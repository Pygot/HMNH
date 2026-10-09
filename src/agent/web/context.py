# src/agent/web/context.py
from agent.connections import (
    ConnectionChecker,
    Connections,
)
from agent.web.security import (
    RateLimiter,
    SessionStore,
)
from typing import (
    Protocol,
    TYPE_CHECKING,
)
from agent.web.conversation import Conversation
from agent.passwords import PasswordHasher
from agent.web.settings import WebSettings
from agent.portability import Portability
from agent.candidates import Candidates
from agent.runtime import ServiceStatus
from starlette.requests import Request
from agent.finance import FinanceBook
from agent.messaging import Messenger
from agent.pipeline import Researcher
from agent.web.jobs import JobManager
from agent.voice import VoiceService
from agent.accounts import Accounts
from agent.errors import AgentError
from agent.policy import PolicyBook
from agent.routines import Routines
from agent.web.assets import Asset
from agent.chat import ChatEngine
from agent.config import Settings
from dataclasses import dataclass
from agent.http import TTLCache
from agent.mailer import Mailer
from agent.stats import Stats
from agent.store import Store
from agent.vault import Vault

import ipaddress
import jinja2

if TYPE_CHECKING:
    from agent.web.automation import RoutineRunner
    from agent.web.hosting import ServiceHost


class Services(Protocol):
    """The running services that the web layer relies on.

    Any of the service attributes can be None.
    """

    pipeline: Researcher | None
    voice: VoiceService | None
    chat: ChatEngine | None

    async def status(self) -> ServiceStatus:
        """Report which services are configured and reachable.

        Returns:
            The current service status.
        """
        ...


@dataclass
class WebContext:
    """The shared state of the web application.

    It holds the stores, security components and rate limiters that request handlers
    use. The services field stays None while the service is still starting.
    """

    settings: Settings
    web: WebSettings
    sessions: SessionStore
    jobs: JobManager
    templates: jinja2.Environment
    login_limiter: RateLimiter
    submit_limiter: RateLimiter
    session_limiter: RateLimiter
    api_limiter: RateLimiter
    api_failure_limiter: RateLimiter
    voice_limiter: RateLimiter
    chat_limiter: RateLimiter
    mail_limiter: RateLimiter
    store: Store
    accounts: Accounts
    hasher: PasswordHasher
    vault: Vault
    policy: PolicyBook
    mailer: Mailer
    user_login_limiter: RateLimiter
    user_ceiling_limiter: RateLimiter
    second_factor_limiter: RateLimiter
    link_limiter: RateLimiter
    account_limiter: RateLimiter
    assets: dict[str, Asset]
    status_cache: TTLCache[ServiceStatus]
    candidates: Candidates
    routines: Routines
    finance: FinanceBook
    stats: Stats
    messenger: Messenger
    portability: Portability
    message_limiter: RateLimiter
    runner: RoutineRunner | None
    connections: Connections
    checker: ConnectionChecker
    host: ServiceHost | None = None
    services: Services | None = None
    conversation: Conversation | None = None
    setup_code: str | None = None
    base_url: str = "http://localhost:8000"


def context_of(request: Request) -> WebContext:
    """Return the web context of the application that handles a request.

    Args:
        request: The current request.

    Returns:
        The web context stored on the application state.
    """
    return request.app.state.ctx


def client_key(request: Request) -> str:
    """Return the key that identifies the client of a request for rate limiting.

    The key is the client host, or unknown without one. IPv6 addresses are reduced to
    their network prefix as set in the web settings.

    Args:
        request: The current request.

    Returns:
        The host, or the network address of the IPv6 prefix of the client.
    """
    host = request.client.host if request.client else "unknown"
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return host
    if address.version != 6:
        return host
    prefix = context_of(request).web.ipv6_prefix
    # Clients are grouped by IPv6 prefix so rotating addresses inside one range does not dodge the
    # rate limits.
    return str(ipaddress.IPv6Network((address, prefix), strict=False).network_address)


async def service_status(ctx: WebContext) -> tuple[ServiceStatus | None, str | None]:
    """Return the status of the services, using a short-lived cache.

    An error result is not cached.

    Args:
        ctx: The web context.

    Returns:
        A tuple of the status and an error message. The status is None with a message
        while the services are starting or when the status cannot be read.
    """
    if ctx.services is None:
        return None, "The service is still starting."
    cached = ctx.status_cache.get("status")
    if cached is not None:
        return cached, None
    try:
        status = await ctx.services.status()
    except AgentError as error:
        return None, str(error)
    ctx.status_cache.put("status", status)
    return status, None
