import asyncio
import inspect
from collections.abc import Callable
from typing import Any


def _is_async_callable(func: Callable[..., Any]) -> bool:
    """Detect if a callable is a coroutine function (async def)."""
    return inspect.iscoroutinefunction(func)


async def call_sync_or_async(func: Callable[..., Any], *args, **kwargs) -> Any:
    """
    Call a function that may be sync or async, returning its result.
    If sync, runs in a thread pool to avoid blocking the event loop.
    Thread pool is shared for efficiency.
    """
    if _is_async_callable(func):
        return await func(*args, **kwargs)

    # Call sync function in a thread pool
    result = await asyncio.to_thread(func, *args, **kwargs)
    # If the result is awaitable, await it
    if inspect.isawaitable(result):
        return await result
    return result
