"""Generated thread names, made in the background after a new thread's first run."""

import asyncio
from typing import Any

from injectq import InjectQ

from tenxgraph_api.src.app.core import logger
from tenxgraph_api.src.app.core.utils.log_sanitizer import log_safe
from tenxgraph_api.src.app.core.validation.request_config import (
    owned_thread_info,
)
from tenxgraph_api.src.app.routers.graph.late_module import LateModule


# The names tests patch on ``graph_service`` (``pending_interrupt``, the limits) are read
# through this, at call time.
service_module = LateModule("tenxgraph_api.src.app.routers.graph.services.graph_service")


class ThreadNamingMixin:
    """``GraphService`` methods that name new threads."""

    @property
    def names_threads(self) -> bool:
        """Whether new threads get a generated name (``thread_name_generator`` is not none)."""
        return self.thread_name_generator is not None

    @staticmethod
    def naming_texts(user_messages: list[Any], replies: list[Any] | None = None) -> list[str]:
        """The texts a thread name is made from: the user's messages first, then the replies."""
        texts = [m.text() for m in user_messages if getattr(m, "role", None) == "user"]
        texts.extend(m.text() for m in replies or [] if getattr(m, "role", None) != "user")
        return texts

    async def _save_thread_name(
        self,
        config: dict[str, Any],
        thread_id: int,
        messages: list[str],
    ) -> str | None:
        """Generate the thread's name and save it on the thread; None when naming is off."""
        if self.thread_name_generator is None:
            return None

        thread_name = await self.thread_name_generator.generate_name(messages)

        res = await self.checkpointer.aput_thread(
            config,
            owned_thread_info(config, thread_id, thread_name=thread_name),
        )
        if res:
            logger.info(
                "Generated thread name: %s for thread_id: %s",
                log_safe(thread_name),
                log_safe(thread_id),
            )

        return thread_name

    def _name_thread_in_background(
        self, config: dict[str, Any], thread_id: Any, messages: list[str]
    ) -> None:
        """Generate and save a new thread's name without holding up the response.

        Runs on the core ``BackgroundTaskManager`` when one is bound, so shutdown drains it;
        otherwise on a task this service keeps a reference to. A failure is logged, never
        raised: the run itself already succeeded.
        """

        async def name_thread() -> None:
            try:
                await self._save_thread_name(config, thread_id, messages)
            except Exception as exc:
                logger.warning("Thread naming failed for thread %s: %s", log_safe(thread_id), exc)

        from tenxgraph.utils.background_task_manager import BackgroundTaskManager

        # Only a bound manager (the graph binds its own): try_get on an unbound concrete class
        # would build a fresh, untracked one that shutdown never drains.
        container = InjectQ.get_instance()
        manager = (
            container.try_get(BackgroundTaskManager) if BackgroundTaskManager in container else None
        )
        if isinstance(manager, BackgroundTaskManager):
            manager.create_task(
                name_thread(),
                name="thread_name",
                timeout=service_module.THREAD_NAME_TIMEOUT_SECONDS,
            )
            return
        tasks: set[asyncio.Task[None]] = self.__dict__.setdefault("_naming_tasks", set())
        if len(tasks) >= service_module.MAX_PENDING_THREAD_NAMES:
            logger.warning(
                "Too many thread names pending (%d); leaving thread %s unnamed",
                len(tasks),
                log_safe(thread_id),
            )
            return
        task = asyncio.create_task(name_thread())
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    async def drain_naming_tasks(self) -> None:
        """Wait for the naming tasks this service holds itself (call at shutdown).

        Tasks on the core ``BackgroundTaskManager`` are drained with that manager. There is
        no timeout: a naming model call is the developer's own.
        """
        tasks = list(self.__dict__.get("_naming_tasks", ()))
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
