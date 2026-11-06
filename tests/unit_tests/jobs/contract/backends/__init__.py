"""Real backends for the contract suites (one module each).

``INTEGRATION`` is set from the ``--integration`` option (registered in ``tests/conftest.py``)
by ``pytest_configure`` in ``contract/conftest.py``; backends that need a service read it
through ``integration_enabled()`` instead of parsing ``sys.argv``.
"""

from __future__ import annotations


INTEGRATION = False


def integration_enabled() -> bool:
    return INTEGRATION
