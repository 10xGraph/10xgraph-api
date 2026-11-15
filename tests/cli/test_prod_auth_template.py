"""The prod scaffold's custom auth denies with 401 until implemented (L12).

It used to raise NotImplementedError, so every request was a 500 and the quickest "fix" was
to return a hard-coded user.
"""

# ruff: noqa: S101

import importlib.util
from pathlib import Path

import pytest
from fastapi import HTTPException, Response
from fastapi.security import HTTPAuthorizationCredentials

import tenxgraph_api


TEMPLATE = Path(tenxgraph_api.__file__).parent / "cli/templates/prod/auth/agent_auth.py"


def _agent_auth():
    spec = importlib.util.spec_from_file_location("scaffold_agent_auth", TEMPLATE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.AgentAuth()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "credential",
    [None, HTTPAuthorizationCredentials(scheme="Bearer", credentials="anything")],
)
async def test_unimplemented_auth_denies_with_401(credential):
    with pytest.raises(HTTPException) as exc:
        await _agent_auth().authenticate(None, Response(), credential)
    assert exc.value.status_code == 401
