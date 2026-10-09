# src/agent/web/server.py
from agent.web.settings import load_web_settings
from agent.runtime import open_runtime
from agent.errors import ConfigError
from agent.web.app import create_app
from agent.config import Settings

import uvicorn


def run_server(settings: Settings, host: str | None, port: int | None) -> None:
    """Start the web application with uvicorn and block until it stops.

    The access log and the server header are disabled, and forwarded headers are
    trusted only from the configured proxies.

    Args:
        settings: the application settings.
        host: the interface to bind, or None to use the web setting.
        port: the port to listen on, or None to use the web setting.

    Raises:
        ConfigError: when the host is not a loopback address and cookies are not
            marked secure.
    """
    web = load_web_settings()
    bind_host = host or web.host
    # Never expose the app beyond loopback unless cookies are secure, since sessions would otherwise
    # travel in clear text.
    if bind_host not in web.loopback_hosts and not web.cookie_secure:
        raise ConfigError(
            "Refusing to listen beyond the loopback interface with insecure cookies; "
            "serve behind TLS and keep WEB_COOKIE_SECURE=true."
        )
    app = create_app(
        settings, web, lambda: open_runtime(app.state.ctx.settings, require_research=False)
    )
    app.state.ctx.base_url = f"http://{bind_host}:{port or web.port}"
    uvicorn.run(
        app,
        host=bind_host,
        port=port or web.port,
        server_header=False,
        proxy_headers=bool(web.proxies),
        # Only configured proxies may set forwarded headers, so clients cannot spoof their address.
        forwarded_allow_ips=",".join(web.proxies) or None,
        access_log=False,
        log_config=None,
    )
