"""The server and jobs must import without the CLI libraries: `10xgraph build` images
uninstall typer, rich and questionary (see `_slim_image.py`)."""

import subprocess
import sys
import textwrap


SCRIPT = textwrap.dedent(
    """
    import sys

    for name in ("typer", "rich", "questionary"):
        sys.modules[name] = None  # any import of these raises ImportError

    import asyncio
    import importlib
    import pkgutil

    import tenxgraph_api.jobs as jobs_pkg
    import tenxgraph_api.src.app as app_pkg

    names = ["tenxgraph_api.src.app.main", "tenxgraph_api.src.app.loader"]
    for pkg in (app_pkg, jobs_pkg):
        names += [m.name for m in pkgutil.walk_packages(pkg.__path__, pkg.__name__ + ".")]
    failed = []
    for name in sorted(set(names)):
        try:
            importlib.import_module(name)
        except ImportError as exc:
            failed.append(f"{name}: {exc}")
    assert not failed, failed

    from tenxgraph_api.src.app.routers.tasks import boot
    assert boot.is_glob("tasks/*.py") and not boot.is_glob("myapp.tasks")
    """
)


def test_server_and_jobs_import_without_cli_libs():
    result = subprocess.run(
        [sys.executable, "-c", SCRIPT], capture_output=True, text=True, timeout=120, check=False
    )
    assert result.returncode == 0, result.stderr
