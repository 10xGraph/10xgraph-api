"""Look up optional services in the InjectQ container serving the current request."""

from __future__ import annotations

import logging

from injectq import InjectQ


logger = logging.getLogger("tenxgraph_api")


def lookup[T](service_type: type[T]) -> T | None:
    """An optional service from the current container (``InjectQ.get_instance()``), else None.

    Inside a request the container is the app's (``setup_fastapi`` makes it current), outside
    one the global container. For services the app may not have (media, telemetry, auth): a
    lookup that fails is the same as none bound.
    """
    try:
        return InjectQ.get_instance().try_get(service_type)
    except Exception as exc:
        logger.debug("No %s available: %s", service_type.__name__, exc)
        return None
