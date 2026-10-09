# src/agent/web/hosting.py
from agent.web.context import (
    Services,
    WebContext,
)
from contextlib import (
    AbstractAsyncContextManager,
    AsyncExitStack,
)
from agent.web.conversation import Conversation
from agent.errors import InvalidRequest
from agent.web.jobs import WEB_OWNER
from collections.abc import Callable
from agent.config import Settings

import asyncio
import logging

logger = logging.getLogger(__name__)
ServicesFactory = Callable[[], AbstractAsyncContextManager[Services]]
BUSY = "Searches are running. Wait until they finish, then apply the change."


class ServiceHost:
    """Own the running services of the web app and rebuild them when settings change.

    Reloads are serialised by a lock and refused while searches are running. If the new
    settings cannot be opened, the previous settings and services are restored.
    """

    def __init__(self, ctx: WebContext, factory: ServicesFactory):
        """Initialize the host with the web context and a services factory.

        Args:
            ctx: The web context that receives the services and the conversation.
            factory: Callable that returns an async context manager yielding the services.
        """
        self._ctx = ctx
        self._factory = factory
        self._stack: AsyncExitStack | None = None
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        """Open the services for the first time.

        Interrupted research threads from a previous run are recovered.
        """
        await self._open(first=True)

    async def stop(self) -> None:
        """Close the services if they are open.

        Calling it again, or before start, does nothing.
        """
        stack, self._stack = self._stack, None
        if stack is not None:
            await stack.aclose()

    async def _open(self, first: bool = False) -> None:
        """Open the services from the factory and wire them into the context.

        The status cache is cleared, the pipeline is attached to the job manager, and a new
        conversation is built when a chat engine exists. A failure closes what was opened
        and is raised again.

        Args:
            first: True on the first start, which also recovers interrupted threads.
        """
        ctx = self._ctx
        stack = AsyncExitStack()
        try:
            services = await stack.enter_async_context(self._factory())
        except BaseException:
            await stack.aclose()
            raise
        self._stack = stack
        ctx.services = services
        ctx.status_cache.clear()
        if services.pipeline is not None:
            ctx.jobs.attach(services.pipeline)
        if services.chat is not None:
            ctx.conversation = Conversation(
                ctx.store,
                services.chat,
                ctx.jobs,
                lambda: ctx.submit_limiter.allow(WEB_OWNER),
                ctx.settings.voice.spoken_flags,
            )
            ctx.jobs.on_settled = ctx.conversation.settled
            # Recovery runs only on the first start, because a reload is refused while jobs are
            # running.
            if first:
                ctx.conversation.recover()

    async def _close(self) -> None:
        """Detach the services and the conversation from the context and shut them down."""
        ctx = self._ctx
        ctx.services = None
        ctx.conversation = None
        ctx.jobs.on_settled = None
        ctx.jobs.detach()
        await self.stop()

    async def reload(self, settings: Settings) -> None:
        """Replace the services with ones built from new settings.

        New searches are blocked while the services are swapped. If the new services fail to
        open, the old settings are put back and the old services are opened again before the
        error is raised.

        Args:
            settings: The settings to apply.

        Raises:
            InvalidRequest: when searches are still running.
        """
        ctx = self._ctx
        async with self._lock:
            if ctx.jobs.busy():
                raise InvalidRequest(BUSY)
            previous = ctx.settings
            # Freezing blocks new jobs from starting while the services are closed and reopened.
            ctx.jobs.freeze()
            try:
                await self._close()
                ctx.settings = settings
                ctx.mailer.use(settings)
                try:
                    await self._open()
                except BaseException:
                    ctx.settings = previous
                    ctx.mailer.use(previous)
                    await self._open()
                    raise
            finally:
                ctx.jobs.thaw()
