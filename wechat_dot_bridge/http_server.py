"""Official SDK Streamable HTTP and OAuth beside the existing single worker."""
from contextlib import asynccontextmanager, contextmanager
import ipaddress
import logging
from pathlib import Path
from urllib.parse import urlsplit

import anyio
from mcp.server.auth.middleware.bearer_auth import BearerAuthBackend, RequireAuthMiddleware
from mcp.server.auth.provider import ProviderTokenVerifier
from mcp.server.auth.routes import build_resource_metadata_url, create_auth_routes, create_protected_resource_routes
from mcp.server.auth.settings import ClientRegistrationOptions, RevocationOptions
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import RequestBodyLimitMiddleware, TransportSecuritySettings
from pydantic import AnyHttpUrl
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.authentication import AuthenticationMiddleware
from starlette.responses import JSONResponse
from starlette.routing import Route
import uvicorn

from .errors import BridgeError
from .owner_auth import OwnerAuthorization, SCOPE
from .sdk_adapter import MAX_RPC_BYTES, SDKAdapter, _private_sdk_logging


def validate_settings(value):
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {"public_url", "listen_host", "listen_port", "password_file"}:
        raise BridgeError("http_config_invalid")
    try:
        url = urlsplit(value["public_url"])
        address = ipaddress.ip_address(value["listen_host"])
        if (url.scheme != "https" or not url.hostname or url.username or url.password or url.query or url.fragment
                or url.path not in ("", "/") or url.port not in (None, 443)
                or not address.is_private or address.is_unspecified or address.is_multicast
                or type(value["listen_port"]) is not int or not 1024 <= value["listen_port"] <= 65535
                or not Path(value["password_file"]).is_absolute()):
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise BridgeError("http_config_invalid") from None
    return dict(value, public_url=value["public_url"].rstrip("/"))


def create_app(bridge, settings, auth=None):
    settings = validate_settings(settings)
    issuer = settings["public_url"]
    resource = AnyHttpUrl(issuer + "/mcp")
    auth = auth or OwnerAuthorization(bridge.store.directory / "http-auth", settings["password_file"], issuer)
    adapter = SDKAdapter(bridge)
    manager = StreamableHTTPSessionManager(adapter.server, json_response=True, stateless=True,
        max_request_body_size=MAX_RPC_BYTES,
        security_settings=TransportSecuritySettings(allowed_hosts=[urlsplit(issuer).netloc, urlsplit(issuer).netloc + ":443"],
                allowed_origins=[issuer, "https://chatgpt.com", "https://chat.openai.com"]))

    @asynccontextmanager
    async def lifespan(app):
        try:
            with _private_sdk_logging():
                async with manager.run():
                    yield
        finally:
            auth.close()

    async def health(request):
        # The gateway never publishes this local transport readiness route.
        return JSONResponse({"ready": True})

    protected = RequireAuthMiddleware(manager.handle_request, [SCOPE], build_resource_metadata_url(resource))
    routes = create_auth_routes(auth, AnyHttpUrl(issuer),
                client_registration_options=ClientRegistrationOptions(enabled=True, valid_scopes=[SCOPE], default_scopes=[SCOPE]),
                revocation_options=RevocationOptions(enabled=True))
    routes += create_protected_resource_routes(resource, [AnyHttpUrl(issuer)], scopes_supported=[SCOPE], resource_name="weixin-bot-mcp")
    routes += [Route("/owner-login", auth.login, methods=["GET", "POST"]),
               Route("/mcp", protected, methods=["GET", "POST", "DELETE"]),
               Route("/healthz", health, methods=["GET"])]
    app = Starlette(routes=routes, lifespan=lifespan,
            middleware=[Middleware(AuthenticationMiddleware, backend=BearerAuthBackend(ProviderTokenVerifier(auth), resource_server_url=resource))])
    app.state.auth_provider = auth
    return RequestBodyLimitMiddleware(app, MAX_RPC_BYTES)


async def run_http(bridge, settings):
    class LocalServer(uvicorn.Server):
        @contextmanager
        def capture_signals(self):
            # The existing AnyIO runtime owns SIGTERM and graceful send safety.
            yield

    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logging.getLogger(name).disabled = True
    app = create_app(bridge, settings)
    server = LocalServer(uvicorn.Config(app, host=settings["listen_host"], port=settings["listen_port"],
                log_config=None, access_log=False, proxy_headers=False, server_header=False,
                limit_concurrency=32, timeout_keep_alive=5, timeout_graceful_shutdown=10))
    # Uvicorn's lifespan task is outside the bridge task group. Let its own
    # shutdown close HTTP sessions before the bridge closes its shared store.
    finished = anyio.Event()

    async def serve_until_exit():
        try:
            with anyio.CancelScope(shield=True):
                await server.serve()
        finally:
            finished.set()

    async with anyio.create_task_group() as tasks:
        tasks.start_soon(serve_until_exit)
        try:
            await finished.wait()
        finally:
            server.should_exit = True
