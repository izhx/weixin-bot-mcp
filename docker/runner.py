"""Container supervisor and operator entry point; runtime state lives in /data."""
import getpass
import http.server
import json
import os
from pathlib import Path
import re
import signal
import sqlite3
import stat
import subprocess
import sys
import threading
import time
import urllib.request
import warnings

ROOT = Path(os.environ.get("BRIDGE_DATA_DIR", "/data"))
CONFIG = ROOT / "config/bridge.json"
PROFILE = ROOT / "config/tunnel.yaml"
KEY = ROOT / "secrets/runtime.key"
PASSWORD = ROOT / "secrets/http-password.json"
PYTHON = sys.executable
PORT = int(os.environ.get("BRIDGE_HEALTH_PORT", "8788"))


def write_private(path, value):
    raw = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode() if not isinstance(value, bytes) else value
    temporary = path.with_name(path.name + ".new")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def initialize():
    os.umask(0o077)
    from wechat_dot_bridge.private_paths import _private_directory as private_directory
    for suffix in ("", "config", "secrets", "state", "locks", "home"):
        private_directory(ROOT / suffix)
    legacy_profile = ROOT / "config/tunnel.json"
    if legacy_profile.is_file() and not PROFILE.exists():
        # JSON is valid YAML, but the official client requires a .yaml suffix.
        write_private(PROFILE, json.loads(legacy_profile.read_text()))
    if not CONFIG.exists():
        write_private(CONFIG, {"state_dir": str(ROOT / "state"), "lock_dir": str(ROOT / "locks"), "callback_hosts": []})
    from wechat_dot_bridge.store import Store
    store = Store(ROOT / "state")
    store.close()


def cli_command(*args):
    return [PYTHON, "-m", "wechat_dot_bridge", "--config", str(CONFIG), *args]


def tunnel_profile(tunnel_id, proxy=None):
    if not re.fullmatch(r"tunnel_[0-9a-f]{32}", tunnel_id):
        raise ValueError("invalid_tunnel_id")
    profile = {"config_version": 1,
               "control_plane": {"base_url": "https://api.openai.com", "tunnel_id": tunnel_id, "api_key": "file:" + str(KEY)},
               "health": {"listen_addr": "127.0.0.1:0", "url_file": str(ROOT / "state/tunnel-health.url")},
               "admin_ui": {"open_browser": False}, "log": {"level": "warn", "format": "json"},
               "mcp": {"commands": [{"channel": "main", "command": " ".join(cli_command("serve"))}]}}
    if proxy:
        if not re.fullmatch(r"http://127\.0\.0\.1:[0-9]{1,5}", proxy) or not 1 <= int(proxy.rsplit(":", 1)[1]) <= 65535:
            raise ValueError("invalid_proxy")
        profile["control_plane"]["http_proxy"] = proxy
    return profile


def runtime_key(value):
    value = value.strip()
    if value and (not 20 <= len(value) <= 1024 or any(c.isspace() for c in value)):
        raise ValueError("invalid_runtime_key")
    return value


def private_bytes(path, limit=16384):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    except OSError:
        raise ValueError("unsafe_runtime_config") from None
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
            raise ValueError("unsafe_runtime_config")
        raw = stream.read(limit + 1)
        if len(raw) > limit:
            raise ValueError("runtime_config_too_large")
        return raw


def apply_environment():
    """Merge explicit Compose settings; empty values preserve persisted setup."""
    from wechat_dot_bridge.net_transport import NetworkFailure, proxy_port

    # The tunnel child reads the private file, not the literal bootstrap secret.
    secret = runtime_key(os.environ.pop("WEIXIN_TUNNEL_RUNTIME_KEY", ""))
    tunnel_id = os.environ.get("WEIXIN_TUNNEL_ID", "").strip()
    tunnel_proxy = os.environ.get("WEIXIN_TUNNEL_PROXY", "").strip()
    updates = {}
    for name in ("callback_proxy", "callback_dns", "media_proxy", "media_dns"):
        value = os.environ.get("WEIXIN_" + name.upper(), "").strip()
        if not value:
            continue
        if name.endswith("_proxy"):
            value = None if value == "direct" else value
            try:
                proxy_port(value)
            except NetworkFailure:
                raise ValueError("invalid_environment_proxy") from None
        elif value not in ("system", "google"):
            raise ValueError("invalid_environment_dns")
        updates[name] = value
    if not (secret or tunnel_id or tunnel_proxy or updates):
        return

    config = json.loads(private_bytes(CONFIG))
    if not isinstance(config, dict):
        raise ValueError("invalid_environment_config")
    merged = dict(config, **updates)
    writes = []
    if secret or tunnel_id or tunnel_proxy:
        raw = private_bytes(PROFILE)
        previous = json.loads(raw) if raw is not None else {}
        if not isinstance(previous, dict) or not isinstance(previous.get("control_plane", {}), dict):
            raise ValueError("invalid_environment_config")
        control = previous.get("control_plane", {})
        tunnel_id = tunnel_id or control.get("tunnel_id")
        proxy = (None if tunnel_proxy == "direct" else tunnel_proxy) if tunnel_proxy else control.get("http_proxy")
        prepared = tunnel_profile(tunnel_id or "", proxy)
        profile = dict(previous or prepared)
        profile["control_plane"] = dict(control or prepared["control_plane"],
                                        tunnel_id=tunnel_id, api_key="file:" + str(KEY))
        if proxy is None:
            profile["control_plane"].pop("http_proxy", None)
        else:
            profile["control_plane"]["http_proxy"] = proxy
        old_key = private_bytes(KEY, limit=1024)
        if secret and secret.encode() != old_key:
            writes.append((KEY, secret.encode()))
        if profile != previous:
            writes.append((PROFILE, profile))
    if merged != config:
        writes.append((CONFIG, merged))
    # Validate every provided setting and destination before changing any file.
    for path, value in writes:
        write_private(path, value)


def setup_tunnel():
    if not sys.stdin.isatty():
        raise ValueError("interactive_terminal_required")
    existing = json.loads(PROFILE.read_text()) if PROFILE.is_file() else {}
    tunnel_id = existing.get("control_plane", {}).get("tunnel_id") or input("Tunnel ID: ").strip()
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        secret = runtime_key(getpass.getpass("Runtime key (hidden; blank keeps existing): "))
    if secret:
        write_private(KEY, secret.encode())
    if not KEY.is_file():
        raise ValueError("runtime_key_missing")
    proxy = existing.get("control_plane", {}).get("http_proxy")
    write_private(PROFILE, tunnel_profile(tunnel_id, proxy))
    print(json.dumps({"configured": True, "hint": "The running container reloads within 3 seconds."}))


def setup_password():
    if not sys.stdin.isatty():
        raise ValueError("interactive_terminal_required")
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        first = getpass.getpass("Set your HTTPS MCP password (12-256 characters, hidden): ")
        second = getpass.getpass("Repeat password (hidden): ")
    if first != second:
        raise ValueError("passwords_do_not_match")
    from wechat_dot_bridge.owner_auth import password_record
    write_private(PASSWORD, password_record(first))
    print(json.dumps({"password_configured": True, "old_oauth_tokens_invalidated": True}))


def safe_state():
    result = {"bound": False, "enabled": False, "active_subscriptions": 0}
    path = ROOT / "state/bridge.sqlite3"
    try:
        with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=1) as db:
            row = db.execute("SELECT enabled,status FROM binding LIMIT 1").fetchone()
            result.update(bound=bool(row), enabled=bool(row and row[0]), account_status=row[1] if row else "not_bound")
            result["active_subscriptions"] = db.execute("SELECT count(*) FROM subscriptions WHERE status='active' AND (expires_at=0 OR expires_at>?)", (time.time(),)).fetchone()[0]
    except (OSError, sqlite3.Error):
        result["database_ready"] = False
    return result


def stop_child(child):
    if child and child.poll() is None:
        os.killpg(child.pid, signal.SIGTERM)
        try:
            child.wait(timeout=40)
        except subprocess.TimeoutExpired:
            os.killpg(child.pid, signal.SIGKILL)
            child.wait()
    if child and child.stdin:
        child.stdin.close()


def run():
    from wechat_dot_bridge.store import Store
    # Initialize once before the worker owns the database; health is read-only.
    store = Store(ROOT / "state")
    store.close()
    stopping = threading.Event()
    state = {"mode": "starting", "child": None}

    class Health(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            if self.path not in ("/healthz", "/readyz"):
                self.send_error(404)
                return
            child = state["child"]
            live = bool(child and child.poll() is None)
            value = {"process_healthy": live, "mode": state["mode"], **safe_state()}
            tunnel_ready = False
            if state["mode"] == "tunnel" and live:
                try:
                    url = (ROOT / "state/tunnel-health.url").read_text().strip()
                    if re.fullmatch(r"http://127\.0\.0\.1:[0-9]{1,5}", url.rstrip("/")):
                        with urllib.request.urlopen(url.rstrip("/") + "/readyz", timeout=1) as response:
                            tunnel_ready = response.status == 200
                except Exception:
                    pass
            value["tunnel_ready"] = tunnel_ready
            http_ready = False
            try:
                config = json.loads(CONFIG.read_text())
                http = config.get("http")
                if http and live:
                    url = "http://" + http["listen_host"] + ":" + str(http["listen_port"]) + "/healthz"
                    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                    with opener.open(url, timeout=1) as response:
                        http_ready = response.status == 200
            except Exception:
                pass
            value["http_ready"] = http_ready
            transport_ready = http_ready if state["mode"] == "direct" else tunnel_ready
            value["ready"] = live and transport_ready and value["bound"] and value["enabled"] and value.get("account_status") == "ready" and value["active_subscriptions"] == 1
            body = json.dumps(value).encode()
            self.send_response(200 if (live if self.path == "/healthz" else value["ready"]) else 503)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = http.server.ThreadingHTTPServer(("127.0.0.1", PORT), Health)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    signal.signal(signal.SIGTERM, lambda *_: stopping.set())
    signal.signal(signal.SIGINT, lambda *_: stopping.set())
    fingerprint = None
    try:
        while not stopping.is_set():
            current = tuple((p.stat().st_mtime_ns, p.stat().st_size) if p.is_file() else None for p in (PROFILE, KEY, CONFIG))
            child = state["child"]
            if current != fingerprint or child is None or child.poll() is not None:
                stop_child(child)
                config = json.loads(CONFIG.read_text())
                direct = config.get("mcp_transport") == "direct" and config.get("http") and PASSWORD.is_file()
                configured = PROFILE.is_file() and KEY.is_file() and not direct
                mode = "direct" if direct else "tunnel" if configured else "waiting_for_tunnel"
                command = ["/opt/tunnel/tunnel-client", "run", "--profile-file", str(PROFILE)] if configured else cli_command("serve")
                # Keep stdio open in provisioning mode so the persistent receive
                # worker runs before Dot is connected. The tunnel replaces it.
                child = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
                state.update(child=child, mode=mode)
                fingerprint = current
                print(json.dumps({"event": "worker_started", "mode": mode}), flush=True)
            stopping.wait(3)
    finally:
        stop_child(state["child"])
        server.shutdown()
        server.server_close()


def main():
    args = sys.argv[1:]
    if args == ["healthcheck"]:
        with urllib.request.urlopen("http://127.0.0.1:" + str(PORT) + "/healthz", timeout=3) as response:
            return 0 if response.status == 200 else 1
    if args == ["test"]:
        return subprocess.call([PYTHON, "-m", "unittest", "discover", "-s", "/app/tests", "-v"], cwd="/app")
    initialize()
    if args and args[0] == "prepare-tunnel":
        if not 2 <= len(args) <= 3:
            raise ValueError("tunnel_arguments_required")
        write_private(PROFILE, tunnel_profile(args[1], args[2] if len(args) == 3 else None))
        print(json.dumps({"profile_prepared": True, "key_present": KEY.is_file()}))
    elif args == ["setup-tunnel"]:
        setup_tunnel()
    elif args == ["setup-password"]:
        setup_password()
    elif args and args[0] == "prepare-url":
        if len(args) not in (2, 3) or not PASSWORD.is_file():
            raise ValueError("password_and_public_url_required")
        from wechat_dot_bridge.http_server import validate_settings
        config = json.loads(CONFIG.read_text())
        config["http"] = validate_settings({"public_url": args[1], "listen_host": args[2] if len(args) == 3 else "172.17.0.1",
                    "listen_port": 8789, "password_file": str(PASSWORD)})
        write_private(CONFIG, config)
        print(json.dumps({"http_configured": True, "mcp_url": args[1].rstrip("/") + "/mcp"}))
    elif args and args[0] == "transport":
        if len(args) != 2 or args[1] not in ("tunnel", "direct"):
            raise ValueError("invalid_transport")
        config = json.loads(CONFIG.read_text())
        if args[1] == "direct" and (not config.get("http") or not PASSWORD.is_file()):
            raise ValueError("https_configuration_required")
        config["mcp_transport"] = args[1]
        write_private(CONFIG, config)
        print(json.dumps({"mcp_transport": args[1]}))
    elif args and args[0] == "set-callback-host":
        hosts = args[1:]
        if not 1 <= len(hosts) <= 3 or any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?", host) or ".." in host for host in hosts):
            raise ValueError("invalid_callback_hosts")
        config = json.loads(CONFIG.read_text())
        config["callback_hosts"] = hosts
        write_private(CONFIG, config)
        print(json.dumps({"callback_hosts_configured": True}))
    elif args == ["login"]:
        result = subprocess.call(cli_command("login-start"))
        if result:
            return result
        image = ROOT / "state/weixin-login-qr.png"
        image.unlink(missing_ok=True)
        return subprocess.call([PYTHON, "/app/scripts/render_login_qr.py", "--state-dir", str(ROOT / "state"), "--output", str(image)])
    elif args:
        os.execv(PYTHON, cli_command(*args))
    else:
        apply_environment()
        run()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        print(json.dumps({"error": "container_command_failed", "hint": "Check provisioning, private directory permissions and the selected command."}), file=sys.stderr)
        sys.exit(1)
