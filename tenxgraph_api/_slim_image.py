"""Remove CLI-only packages from a production image. Run by the generated Dockerfile.

The server never imports the CLI libraries (typer, rich, questionary and what they pull in),
so the image does not need them. A package is removed only when nothing else installed still
requires it, so a project dependency that uses rich (for example) keeps it and everything it
needs. Standard library only: it runs in the Docker builder stage.

    python -I -m tenxgraph_api._slim_image
"""

from __future__ import annotations

import re
import subprocess
import sys
import sysconfig
from importlib import metadata
from pathlib import Path


CLI_PACKAGE = "10xgraph-api"
# The CLI's own dependencies and everything only they pull in.
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

    # Fixed package names from CLI_ONLY, never user input.
    subprocess.check_call([sys.executable, "-m", "pip", "uninstall", "-y", "-q", *remove])  # noqa: S603
    sys.stdout.write(f"slim image: removed {', '.join(remove)}\n")

    if "typer" in remove:
        # The 10xgraph command cannot run without typer; drop its launchers too.
        scripts = Path(sysconfig.get_path("scripts"))
        for name in CONSOLE_SCRIPTS:
            (scripts / name).unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
