"""Drop the CLI's own libraries from a production Docker image.

Why this exists
    ``10xgraph-api`` ships the server and the ``10xgraph`` CLI in one package, so installing it
    also installs the CLI's libraries: typer, rich, questionary and what only they pull in
    (pygments, markdown-it-py, mdurl, prompt-toolkit, wcwidth, shellingham). The server never
    imports any of them. In an image they are about 25 MB (bytecode included) that never runs,
    plus extra packages for vulnerability scanners to flag.

Why not a plain ``pip uninstall`` line in the Dockerfile
    The user's own project may depend on one of these packages, for example a library that
    prints with rich. A fixed uninstall list would delete it, and the app would fail at startup
    with ``ModuleNotFoundError``. This module reads every installed package's requirements and
    removes a CLI library only when nothing else still needs it, directly or through another
    package.

How it runs
    Only inside Docker. The Dockerfile from ``10xgraph build`` (``generate_dockerfile_content``
    in ``cli/templates/defaults.py``) runs it once in the builders tage, before pip itself is
    removed, and skips it when the installed 10xgraph-api is older than this module. To run it
    by hand::

        python -I -m tenxgraph_api._slim_image

    The server and the CLI never import it. It uses only the standard library, so it never
    imports a package it is about to remove.
"""

from __future__ import annotations

import re

# Used only to run pip, which has no supported Python API (pip's docs say to call it as a
# subprocess). See the comment at the call in main().
import subprocess  # nosec B404
import sys
import sysconfig
from importlib import metadata
from pathlib import Path


CLI_PACKAGE = "10xgraph-api"
# The CLI's own dependencies (typer, rich, questionary in pyproject.toml) and everything only
# they pull in. Keep it in sync when the CLI gains or drops a dependency. click is left out on
# purpose: uvicorn needs it, so the server does too.
CLI_ONLY = frozenset(
    {
        "typer",
        "shellingham",
        "rich",
        "pygments",
        "markdown-it-py",
        "mdurl",
        "questionary",
        "prompt-toolkit",
        "wcwidth",
    }
)
CONSOLE_SCRIPTS = ("10xgraph", "agentflow")


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _requirements(dist: metadata.Distribution) -> set[str]:
    """Names this distribution needs at install time (requirements behind an extra skipped)."""
    names = set()
    for req in dist.requires or ():
        _, _, marker = req.partition(";")
        if "extra" in marker:
            continue
        names.add(_norm(re.split(r"[\s;<>=!~\[(]", req.strip(), maxsplit=1)[0]))
    return names


def removable(dists: dict[str, metadata.Distribution]) -> list[str]:
    """CLI-only packages that no other installed package (directly or through another) needs."""
    needed: set[str] = set()
    for name, dist in dists.items():
        if name not in CLI_ONLY and name != CLI_PACKAGE:
            needed |= _requirements(dist)

    kept: set[str] = set()
    while True:
        newly_kept = {name for name in CLI_ONLY & needed if name in dists} - kept
        if not newly_kept:
            break
        kept |= newly_kept
        for name in newly_kept:
            needed |= _requirements(dists[name])

    return sorted(name for name in CLI_ONLY if name in dists and name not in kept)


def main() -> int:
    dists = {_norm(d.metadata["Name"]): d for d in metadata.distributions()}
    remove = removable(dists)
    if not remove:
        sys.stdout.write("slim image: no CLI-only packages to remove\n")
        return 0

    # Safe to run: the program is this interpreter's own pip (sys.executable, no shell) and the
    # arguments are names from CLI_ONLY, never user input. pip must run as a subprocess because
    # it has no supported in-process API.
    subprocess.check_call(  # noqa: S603  # nosec B603
        [sys.executable, "-m", "pip", "uninstall", "-y", "-q", *remove]
    )
    sys.stdout.write(f"slim image: removed {', '.join(remove)}\n")

    if "typer" in remove:
        # The 10xgraph command cannot run without typer; drop its launchers too.
        scripts = Path(sysconfig.get_path("scripts"))
        for name in CONSOLE_SCRIPTS:
            (scripts / name).unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
