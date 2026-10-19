"""Single-owner password gate behind the official SDK's OAuth/PKCE routes.

The SDK owns registration, client authentication, metadata and PKCE validation.
This provider supplies owner consent and private durable grants. Access/refresh
tokens and authorization codes are indexed by digest, never stored verbatim.
"""
import hashlib
import hmac
import html
import json
import os
from pathlib import Path
import secrets
import sqlite3
import stat
import threading
import time
from urllib.parse import urlsplit

import anyio
from mcp.server.auth.provider import (
    AccessToken, AuthorizationCode, AuthorizationParams, AuthorizeError,
    RefreshToken, RegistrationError, TokenError, construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse

from .errors import BridgeError
from .private_paths import _private_database_file, _private_directory

SCOPE = "weixin"
CALLBACK_HOSTS = frozenset({"chatgpt.com", "chat.openai.com", "connectors.api.openai.com"})
COOKIE = "__Host-weixin-mcp-csrf"
HEADERS = {"Cache-Control": "no-store", "Pragma": "no-cache", "Referrer-Policy": "no-referrer",
           "X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY",
           "Content-Security-Policy": "default-src 'none'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'"}


def digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def password_record(password):
    if not isinstance(password, str) or not 12 <= len(password) <= 256:
        raise BridgeError("password_length_12_to_256_required")
    salt = secrets.token_bytes(16)
    derived = hashlib.scrypt(password.encode(), salt=salt, n=32768, r=8, p=1, maxmem=67108864)
    return {"version": 1, "salt": salt.hex(), "hash": derived.hex()}


def read_password(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1 or info.st_size > 1024):
            raise BridgeError("unsafe_password_file")
        value = json.load(stream)
    if (set(value) != {"version", "salt", "hash"} or value["version"] != 1
            or len(bytes.fromhex(value["salt"])) != 16 or len(bytes.fromhex(value["hash"])) != 64):
        raise BridgeError("invalid_password_file")
    return value


class OwnerAuthorization:
    def __init__(self, directory, password_file, issuer, clock=time.time):
        self.directory = _private_directory(directory)
        self.password_file = Path(password_file)
        self.issuer = issuer.rstrip("/")
        self.resource = self.issuer + "/mcp"
        self.clock, self.lock = clock, threading.RLock()
        read_password(self.password_file)  # Fail closed before exposing HTTP.
        self.db = sqlite3.connect(_private_database_file(self.directory), isolation_level=None, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA secure_delete=ON")
        self.db.execute("CREATE TABLE IF NOT EXISTS records(kind TEXT,key TEXT,value TEXT,expires REAL,PRIMARY KEY(kind,key))")
        self.db.execute("CREATE TABLE IF NOT EXISTS login_attempts(at REAL)")

    def close(self):
        self.db.close()

    def _version(self):
        value = read_password(self.password_file)
        return digest(value["salt"] + value["hash"])

    def _put(self, kind, key, value, ttl):
        with self.lock:
            self.db.execute("DELETE FROM records WHERE expires<=?", (self.clock(),))
            limit = 32 if kind == "client" else 128
            if self.db.execute("SELECT count(*) FROM records WHERE kind=?", (kind,)).fetchone()[0] >= limit:
                raise BridgeError("authorization_capacity_reached")
            self.db.execute("INSERT INTO records VALUES(?,?,?,?)", (kind, key, json.dumps(value), self.clock() + ttl))

    def _get(self, kind, key):
        with self.lock:
            row = self.db.execute("SELECT value FROM records WHERE kind=? AND key=? AND expires>?", (kind, key, self.clock())).fetchone()
        return json.loads(row[0]) if row else None

    def _take(self, kind, key):
        with self.lock:
            value = self._get(kind, key)
            if value is not None:
                self.db.execute("DELETE FROM records WHERE kind=? AND key=?", (kind, key))
            return value

    async def get_client(self, client_id):
        value = self._get("client", client_id)
        return OAuthClientInformationFull.model_validate(value) if value else None

    async def register_client(self, client_info):
        for uri in client_info.redirect_uris:
            parts = urlsplit(str(uri))
            if (parts.scheme != "https" or parts.hostname not in CALLBACK_HOSTS or parts.port not in (None, 443)
                    or parts.username or parts.password or parts.fragment):
                raise RegistrationError("invalid_redirect_uri", "Only the OpenAI callback hosts are allowed")
        if not client_info.redirect_uris:
            raise RegistrationError("invalid_redirect_uri", "A callback is required")
        try:
            self._put("client", client_info.client_id, client_info.model_dump(mode="json"), 365 * 86400)
        except BridgeError:
            raise RegistrationError("invalid_client_metadata", "Registration capacity reached") from None

    async def authorize(self, client, params):
        if params.resource and params.resource.rstrip("/") != self.resource:
            raise AuthorizeError("invalid_target", "Unknown resource")
        if set(params.scopes or [SCOPE]) != {SCOPE}:
            raise AuthorizeError("invalid_scope", "Only the owner weixin scope is available")
        request_id = secrets.token_urlsafe(32)
        self._put("pending", digest(request_id), {"client_id": client.client_id,
                  "params": params.model_dump(mode="json")}, 300)
        return self.issuer + "/owner-login?request=" + request_id

    async def login(self, request):
        if request.method == "GET":
            request_id = request.query_params.get("request", "")
            pending = self._get("pending", digest(request_id))
            if not pending:
                return JSONResponse({"error": "authorization_expired"}, status_code=400, headers=HEADERS)
            csrf = secrets.token_urlsafe(32)
            self._put("csrf", digest(request_id + csrf), {}, 300)
            page = ("<!doctype html><html lang='zh-CN'><meta charset='utf-8'><meta name='viewport' content='width=device-width'>"
                    "<title>微信 MCP 授权</title><h1>微信 MCP 授权</h1>"
                    "<p>输入你在服务器设置的密码，允许你的 Dot 读取微信来信、回复并向本人发送通知。</p>"
                    "<form method='post' action='/owner-login'>"
                    "<input type='hidden' name='request' value='" + html.escape(request_id, quote=True) + "'>"
                    "<input type='hidden' name='csrf' value='" + csrf + "'>"
                    "<label>密码 <input type='password' name='password' autocomplete='current-password' required maxlength='256'></label>"
                    "<button type='submit'>授权我的 Dot</button></form></html>")
            response = HTMLResponse(page, headers=HEADERS)
            response.set_cookie(COOKIE, csrf, max_age=300, secure=True, httponly=True, samesite="lax", path="/")
            return response
        if request.headers.get("origin") != self.issuer:
            return JSONResponse({"error": "invalid_origin"}, status_code=403, headers=HEADERS)
        form = await request.form(max_fields=3, max_part_size=4096)
        request_id, csrf, password = (form.get(k, "") for k in ("request", "csrf", "password"))
        if any(not isinstance(v, str) for v in (request_id, csrf, password)) or len(password) > 256:
            return JSONResponse({"error": "invalid_request"}, status_code=400, headers=HEADERS)
        if (not csrf or not hmac.compare_digest(csrf, request.cookies.get(COOKIE, ""))
                or self._get("csrf", digest(request_id + csrf)) is None):
            return JSONResponse({"error": "invalid_csrf"}, status_code=403, headers=HEADERS)
        with self.lock:
            self.db.execute("DELETE FROM login_attempts WHERE at<?", (self.clock() - 60,))
            if self.db.execute("SELECT count(*) FROM login_attempts").fetchone()[0] >= 5:
                return JSONResponse({"error": "try_again_in_one_minute"}, status_code=429, headers=HEADERS)
            self.db.execute("INSERT INTO login_attempts VALUES(?)", (self.clock(),))
        value = read_password(self.password_file)
        actual = await anyio.to_thread.run_sync(lambda: hashlib.scrypt(password.encode(), salt=bytes.fromhex(value["salt"]),
                                       n=32768, r=8, p=1, maxmem=67108864))
        if not hmac.compare_digest(actual.hex(), value["hash"]):
            return JSONResponse({"error": "incorrect_password"}, status_code=403, headers=HEADERS)
        with self.lock:
            self._take("csrf", digest(request_id + csrf))
            pending = self._take("pending", digest(request_id))
        if not pending:
            return JSONResponse({"error": "authorization_expired"}, status_code=400, headers=HEADERS)
        params = AuthorizationParams.model_validate(pending["params"])
        code = secrets.token_urlsafe(32)
        record = AuthorizationCode(code=code, client_id=pending["client_id"], scopes=[SCOPE],
                    expires_at=self.clock() + 60, code_challenge=params.code_challenge,
                    redirect_uri=params.redirect_uri, redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
                    resource=self.resource, subject="owner").model_dump(mode="json", exclude={"code"})
        record["owner_version"] = self._version()
        self._put("code", digest(code), record, 60)
        response = RedirectResponse(construct_redirect_uri(str(params.redirect_uri), code=code, state=params.state),
                                    status_code=302, headers=HEADERS)
        response.delete_cookie(COOKIE, secure=True, httponly=True, samesite="lax", path="/")
        return response

    async def load_authorization_code(self, client, authorization_code):
        value = self._get("code", digest(authorization_code))
        if not value or value["client_id"] != client.client_id or value["owner_version"] != self._version():
            return None
        return AuthorizationCode(code=authorization_code, **{k: v for k, v in value.items() if k != "owner_version"})

    def _issue(self, client, scopes):
        access, refresh, group = (secrets.token_urlsafe(32) for _ in range(3))
        common = {"client_id": client.client_id, "scopes": scopes, "resource": self.resource,
                  "subject": "owner", "owner_version": self._version(), "group": group}
        self._put("access", digest(access), dict(common, expires_at=int(self.clock() + 3600)), 3600)
        self._put("refresh", digest(refresh), dict(common, expires_at=int(self.clock() + 30 * 86400)), 30 * 86400)
        return OAuthToken(access_token=access, token_type="Bearer", expires_in=3600, refresh_token=refresh, scope=" ".join(scopes))

    async def exchange_authorization_code(self, client, authorization_code):
        with self.lock:
            value = self._take("code", digest(authorization_code.code))
            if not value or value["client_id"] != client.client_id or value["owner_version"] != self._version():
                raise TokenError("invalid_grant", "Authorization code is no longer valid")
            return self._issue(client, authorization_code.scopes)

    async def load_refresh_token(self, client, refresh_token):
        value = self._get("refresh", digest(refresh_token))
        if not value or value["client_id"] != client.client_id or value["owner_version"] != self._version():
            return None
        return RefreshToken(token=refresh_token, **{k: v for k, v in value.items() if k not in {"owner_version", "group"}})

    async def exchange_refresh_token(self, client, refresh_token, scopes):
        with self.lock:
            value = self._take("refresh", digest(refresh_token.token))
            if not value or value["client_id"] != client.client_id or value["owner_version"] != self._version():
                raise TokenError("invalid_grant", "Refresh token is no longer valid")
            self._revoke_group(value["group"])
            return self._issue(client, scopes)

    def _revoke_group(self, group):
        for kind, key, raw in self.db.execute("SELECT kind,key,value FROM records WHERE kind IN ('access','refresh')").fetchall():
            if json.loads(raw)["group"] == group:
                self.db.execute("DELETE FROM records WHERE kind=? AND key=?", (kind, key))

    async def load_access_token(self, token):
        value = self._get("access", digest(token))
        if not value or value["owner_version"] != self._version():
            return None
        return AccessToken(token=token, claims={"iss": self.issuer},
                    **{k: v for k, v in value.items() if k not in {"owner_version", "group"}})

    async def revoke_token(self, token):
        with self.lock:
            value = self._get("access", digest(token.token)) or self._get("refresh", digest(token.token))
            if value:
                self._revoke_group(value["group"])
