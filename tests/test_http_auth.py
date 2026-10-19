"""Real SDK OAuth/PKCE and HTTP contracts with fixture-only credentials."""
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import unittest
from urllib.parse import parse_qs, urlsplit

from starlette.testclient import TestClient
import mcp_types as types

from wechat_dot_bridge.events import Events
from wechat_dot_bridge.http_server import create_app, validate_settings
from wechat_dot_bridge.mcp import MCPServer, PROTOCOL
from wechat_dot_bridge.owner_auth import OwnerAuthorization, password_record
from wechat_dot_bridge.store import Store

ISSUER = "https://203.0.113.10"
REDIRECT = "https://chatgpt.com/connector_platform/oauth/redirect"
PASSWORD = "fixture-password-only-for-tests"
VERIFIER = "fixture-pkce-verifier-that-is-at-least-43-characters-long"


class HTTPAuthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.password = self.root / "password.json"
        self.set_password(PASSWORD)
        self.store = Store(self.root / "state")
        self.settings = {"public_url": ISSUER, "listen_host": "127.0.0.1", "listen_port": 8789,
                         "password_file": str(self.password)}
        self.auth = OwnerAuthorization(self.root / "oauth", self.password, ISSUER)
        app = create_app(MCPServer(self.store, Events(self.store, [])), self.settings, auth=self.auth)
        self.client_context = TestClient(app, base_url=ISSUER, follow_redirects=False)
        self.client = self.client_context.__enter__()
        self.rpc_id = 0

    def tearDown(self):
        self.client_context.__exit__(None, None, None)
        self.store.close()
        self.tmp.cleanup()

    def set_password(self, value):
        self.password.write_text(json.dumps(password_record(value)))
        os.chmod(self.password, 0o600)

    def register(self, callback=REDIRECT):
        return self.client.post("/register", json={"redirect_uris": [callback],
              "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"],
              "token_endpoint_auth_method": "client_secret_post", "scope": "weixin"})

    def begin_login(self):
        response = self.register()
        self.assertEqual(response.status_code, 201, response.text)
        client = response.json()
        challenge = base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest()).decode().rstrip("=")
        response = self.client.get("/authorize", params={"client_id": client["client_id"],
              "redirect_uri": REDIRECT, "response_type": "code", "code_challenge": challenge,
              "code_challenge_method": "S256", "state": "fixture-state", "resource": ISSUER + "/mcp"})
        self.assertEqual(response.status_code, 302, response.text)
        page = self.client.get(response.headers["location"])
        self.assertEqual(page.status_code, 200)
        csrf = re.search("name='csrf' value='([^']+)'", page.text).group(1)
        request_id = parse_qs(urlsplit(response.headers["location"]).query)["request"][0]
        return client, {"request": request_id, "csrf": csrf, "password": PASSWORD}

    def login_code(self):
        client, form = self.begin_login()
        response = self.client.post("/owner-login", data=form, headers={"Origin": ISSUER})
        self.assertEqual(response.status_code, 302, response.text)
        parsed = urlsplit(response.headers["location"])
        self.assertEqual(parsed.hostname, "chatgpt.com")
        values = parse_qs(parsed.query)
        self.assertEqual(values["state"], ["fixture-state"])
        return client, values["code"][0]

    def exchange(self, client, code, verifier=VERIFIER):
        return self.client.post("/token", data={"grant_type": "authorization_code", "code": code,
              "client_id": client["client_id"], "client_secret": client["client_secret"],
              "redirect_uri": REDIRECT, "code_verifier": verifier, "resource": ISSUER + "/mcp"})

    def rpc(self, token, method, arguments=None, host=None):
        self.rpc_id += 1
        headers = {"Authorization": "Bearer " + token, "MCP-Protocol-Version": PROTOCOL,
                   "MCP-Method": method,
                   "Accept": "application/json, text/event-stream"}
        if arguments and "name" in arguments:
            headers["MCP-Name"] = arguments["name"]
        if host:
            headers["Host"] = host
        params = dict(arguments or {})
        params["_meta"] = {types.PROTOCOL_VERSION_META_KEY: PROTOCOL,
                 types.CLIENT_CAPABILITIES_META_KEY: {}, types.CLIENT_INFO_META_KEY: {"name": "fixture-http", "version": "1"}}
        return self.client.post("/mcp", headers=headers,
                   json={"jsonrpc": "2.0", "id": self.rpc_id, "method": method, "params": params})

    def test_public_metadata_but_all_mcp_requests_require_oauth(self):
        response = self.client.post("/mcp", json={})
        self.assertEqual(response.status_code, 401)
        self.assertIn("oauth-protected-resource/mcp", response.headers["www-authenticate"])
        metadata = self.client.get("/.well-known/oauth-authorization-server").json()
        self.assertIn("S256", metadata["code_challenge_methods_supported"])
        self.assertEqual(metadata["registration_endpoint"], ISSUER + "/register")
        resource = self.client.get("/.well-known/oauth-protected-resource/mcp").json()
        self.assertEqual(resource["resource"], ISSUER + "/mcp")
        self.assertEqual(self.rpc("fixture-invalid-token", "tools/list").status_code, 401)
        self.assertNotIn(PASSWORD, json.dumps(resource))

    def test_owner_password_pkce_single_use_code_and_real_modern_mcp(self):
        client, code = self.login_code()
        self.assertEqual(self.exchange(client, code, "incorrect-verifier").status_code, 400)
        response = self.exchange(client, code)
        self.assertEqual(response.status_code, 200, response.text)
        token = response.json()["access_token"]
        self.assertEqual(self.exchange(client, code).status_code, 400)
        discover = self.rpc(token, "server/discover")
        self.assertEqual(discover.status_code, 200, discover.text)
        self.assertIn("events", discover.json()["result"]["capabilities"])
        listed = self.rpc(token, "tools/list")
        self.assertEqual(len(listed.json()["result"]["tools"]), 13)
        status = self.rpc(token, "tools/call", {"name": "get_weixin_status", "arguments": {}})
        self.assertFalse(status.json()["result"]["structuredContent"]["bound"])
        events = self.rpc(token, "events/list").json()["result"]["events"]
        self.assertIn("text", events[0]["payloadSchema"]["properties"])
        self.assertNotEqual(self.rpc(token, "tools/list", host="attacker.invalid").status_code, 200)

    def test_wrong_password_csrf_origin_and_untrusted_callback_never_authorize(self):
        self.assertEqual(self.register("https://attacker.invalid/callback").status_code, 400)
        client, form = self.begin_login()
        self.assertEqual(self.client.post("/owner-login", data=form, headers={"Origin": "https://attacker.invalid"}).status_code, 403)
        self.assertEqual(self.client.post("/owner-login", data=dict(form, csrf="wrong"), headers={"Origin": ISSUER}).status_code, 403)
        self.assertEqual(self.client.post("/owner-login", data=dict(form, password="wrong"), headers={"Origin": ISSUER}).status_code, 403)
        self.assertEqual(self.client.post("/owner-login", data=form, headers={"Origin": ISSUER}).status_code, 302)

    def test_refresh_rotation_password_change_and_hash_only_grant_storage(self):
        client, code = self.login_code()
        original = self.exchange(client, code).json()
        raw = str(self.auth.db.execute("SELECT * FROM records").fetchall())
        for secret in (PASSWORD, code, original["access_token"], original["refresh_token"]):
            self.assertNotIn(secret, raw)
        response = self.client.post("/token", data={"grant_type": "refresh_token",
              "refresh_token": original["refresh_token"], "client_id": client["client_id"],
              "client_secret": client["client_secret"], "resource": ISSUER + "/mcp"})
        self.assertEqual(response.status_code, 200, response.text)
        rotated = response.json()
        self.assertNotEqual(rotated["access_token"], original["access_token"])
        self.assertEqual(self.rpc(original["access_token"], "tools/list").status_code, 401)
        self.assertEqual(self.rpc(rotated["access_token"], "tools/list").status_code, 200)
        self.set_password("different-fixture-password-after-rotation")
        self.assertEqual(self.rpc(rotated["access_token"], "tools/list").status_code, 401)

    def test_password_attempts_are_rate_limited_before_issuing_a_code(self):
        _, form = self.begin_login()
        for _ in range(5):
            response = self.client.post("/owner-login", data=dict(form, password="wrong"), headers={"Origin": ISSUER})
            self.assertEqual(response.status_code, 403)
        response = self.client.post("/owner-login", data=form, headers={"Origin": ISSUER})
        self.assertEqual(response.status_code, 429)
        self.assertEqual(self.auth.db.execute("SELECT count(*) FROM records WHERE kind='code'").fetchone()[0], 0)

    def test_oauth_revocation_invalidates_both_access_and_refresh_tokens(self):
        client, code = self.login_code()
        tokens = self.exchange(client, code).json()
        response = self.client.post("/revoke", data={"token": tokens["access_token"],
                   "client_id": client["client_id"], "client_secret": client["client_secret"]})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.rpc(tokens["access_token"], "tools/list").status_code, 401)
        response = self.client.post("/token", data={"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"],
                   "client_id": client["client_id"], "client_secret": client["client_secret"], "resource": ISSUER + "/mcp"})
        self.assertEqual(response.status_code, 400)

    def test_grants_persist_in_private_database_and_password_is_never_stored(self):
        client, code = self.login_code()
        token = self.exchange(client, code).json()["access_token"]
        peer = OwnerAuthorization(self.root / "oauth", self.password, ISSUER)
        try:
            import anyio
            self.assertIsNotNone(anyio.run(peer.load_access_token, token))
            self.assertIsNotNone(anyio.run(peer.get_client, client["client_id"]))
            self.assertEqual((self.root / "oauth/bridge.sqlite3").stat().st_mode & 0o777, 0o600)
            self.assertNotIn(PASSWORD, self.password.read_text())
        finally:
            peer.close()

    def test_http_cannot_bind_public_plaintext_or_use_non_https_origin(self):
        for changes in ({"listen_host": "0.0.0.0"}, {"public_url": "http://203.0.113.10"},
                        {"public_url": "https://203.0.113.10/?credential=fixture-bad"}):
            with self.assertRaises(Exception):
                validate_settings(dict(self.settings, **changes))
