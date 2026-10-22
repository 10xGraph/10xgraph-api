"""The Dockerfile's slim step removes CLI-only packages, never one something else needs."""

from __future__ import annotations

from tenxgraph_api import _slim_image
from tenxgraph_api.cli.templates.defaults import generate_dockerfile_content


class _Dist:
    def __init__(self, name: str, *requires: str) -> None:
        self.metadata = {"Name": name}
        self.requires = list(requires)


def _installed(*dists: _Dist) -> dict[str, _Dist]:
    return {_slim_image._norm(d.metadata["Name"]): d for d in dists}


CLI_STACK = (
    _Dist("10xgraph-api", "fastapi", "typer>=0.17", "rich>=14", "questionary>=2"),
    _Dist("typer", "click>=8", "rich>=13.8", "shellingham>=1.3"),
    _Dist("rich", "Pygments>=2.13", "markdown-it-py>=2.2"),
    _Dist("markdown_it_py", "mdurl~=0.1"),
    _Dist("mdurl"),
    _Dist("Pygments"),
    _Dist("questionary", "prompt_toolkit>=2,<4"),
    _Dist("prompt_toolkit", "wcwidth"),
    _Dist("wcwidth"),
    _Dist("shellingham"),
    _Dist("click"),
    _Dist("uvicorn", "click>=7", "h11"),
    _Dist("fastapi", "starlette"),
)


def test_removes_every_cli_only_package_when_nothing_else_needs_them() -> None:
    assert _slim_image.removable(_installed(*CLI_STACK)) == sorted(_slim_image.CLI_ONLY)


def test_keeps_a_cli_package_another_dependency_needs_and_what_it_needs() -> None:
    dists = _installed(*CLI_STACK, _Dist("my-reporting-lib", "rich>=13"))
    remove = _slim_image.removable(dists)

    for kept in ("rich", "pygments", "markdown-it-py", "mdurl"):
        assert kept not in remove
    for gone in ("typer", "shellingham", "questionary", "prompt-toolkit", "wcwidth"):
        assert gone in remove


def test_requirements_behind_an_extra_do_not_keep_a_package() -> None:
    dists = _installed(*CLI_STACK, _Dist("some-lib", 'rich>=13; extra == "pretty"'))
    assert "rich" in _slim_image.removable(dists)


def test_never_removes_click_which_uvicorn_needs() -> None:
    assert "click" not in _slim_image.removable(_installed(*CLI_STACK))


def test_dockerfile_runs_the_slim_step_in_the_builder_before_dropping_pip() -> None:
    content = generate_dockerfile_content(
        "3.13", 8000, "requirements.txt", has_requirements=False, has_pyproject=True
    )
    builder, _runtime = content.split("# ---- Stage 2: runtime image ----")
    lines = builder.splitlines()
    slim = next(i for i, line in enumerate(lines) if "tenxgraph_api._slim_image" in line)
    drop_pip = lines.index("RUN pip uninstall -y pip")
    assert slim < drop_pip
