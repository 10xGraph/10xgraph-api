"""Find the InjectQ container serving the current request."""

from __future__ import annotations

import logging

from injectq import InjectQ


logger = logging.getLogger("tenxgraph_api")


def current_container() -> InjectQ:
    """The app's container: the one ``setup_fastapi`` attached to this request, else global.

    ``InjectQ.get_instance()`` alone returns the global container, which is not the app's
    container when the app was set up with its own (tests, embedded apps). Inside a request
    the FastAPI integration knows the right one.
    """
    from injectq.integrations.fastapi import get_container_fastapi
    from injectq.utils import InjectionError

    try:
        return get_container_fastapi()
    except InjectionError:  # outside a request
        return InjectQ.get_instance()


def lookup[T](service_type: type[T]) -> T | None:
    """An optional service from the app's container (:func:`current_container`), else None.

    For services the app may not have (media, telemetry, auth): a lookup that fails is the
    same as none bound.
    """
    try:
        return current_container().try_get(service_type)
    except Exception as exc:
        logger.debug("No %s available: %s", service_type.__name__, exc)
        return None
