"""Find the InjectQ container serving the current request."""

from __future__ import annotations

from injectq import InjectQ


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
