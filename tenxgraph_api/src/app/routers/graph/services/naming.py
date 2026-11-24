"""Generated thread names, made in the background after a new thread's first run."""

from typing import Any

from injectq import InjectQ

from tenxgraph_api.src.app.core import logger
from tenxgraph_api.src.app.core.utils.log_sanitizer import log_safe
from tenxgraph_api.src.app.core.validation.request_config import (
    owned_thread_info,
)


# Longest a background thread-naming call may run (it is a model call).
THREAD_NAME_TIMEOUT_SECONDS = 60.0


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

        Runs on the core ``BackgroundTaskManager`` the graph binds, so shutdown drains it. A
        failure is logged, never raised: the run itself already succeeded.
        """
        from tenxgraph.utils.background_task_manager import BackgroundTaskManager

        # Only a bound manager: try_get on an unbound concrete class would build a fresh,
        # untracked one that shutdown never drains.
        container = InjectQ.get_instance()
        if BackgroundTaskManager not in container:
            logger.warning(
                "No BackgroundTaskManager bound; leaving thread %s unnamed", log_safe(thread_id)
            )
            return

        async def name_thread() -> None:
            try:
                await self._save_thread_name(config, thread_id, messages)
            except Exception as exc:
                logger.warning("Thread naming failed for thread %s: %s", log_safe(thread_id), exc)

        container.get(BackgroundTaskManager).create_task(
            name_thread(),
            name="thread_name",
            timeout=THREAD_NAME_TIMEOUT_SECONDS,
        )
