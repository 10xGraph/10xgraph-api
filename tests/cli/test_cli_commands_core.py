from tenxgraph_api.cli.commands import BaseCommand
from tenxgraph_api.cli.commands.version import VersionCommand
from tenxgraph_api.cli.core.output import OutputFormatter
from tenxgraph_api.cli.exceptions import CLIError


CLI_CUSTOM_EXIT = 5


class DummyOutput(OutputFormatter):
    def __init__(self):  # type: ignore[override]
        super().__init__()
        self.errors: list[str] = []
        self.successes: list[str] = []
        self.infos: list[str] = []

    def error(self, msg: str):  # type: ignore[override]
        self.errors.append(msg)

    def success(self, msg: str):  # type: ignore[override]
        self.successes.append(msg)

    def info(self, msg: str):  # type: ignore[override]
        self.infos.append(msg)

    def print_banner(self, *args, **kwargs):  # type: ignore[override]
        pass


class ErrorCommand(BaseCommand):
    def execute(self, *args, **kwargs):  # pragma: no cover - not used directly
        return 0


def test_basecommand_handle_error_cli_error():
    out = DummyOutput()
    cmd = ErrorCommand(output=out)
    err = CLIError("boom", exit_code=CLI_CUSTOM_EXIT)
    code = cmd.handle_error(err)
    assert code == CLI_CUSTOM_EXIT
    assert out.errors and "boom" in out.errors[0]


def test_basecommand_handle_error_generic():
    out = DummyOutput()
    cmd = ErrorCommand(output=out)
    err = RuntimeError("unexpected")
    code = cmd.handle_error(err)
    assert code == 1
    assert out.errors and "unexpected" in out.errors[0]


def test_version_command_error_branch(monkeypatch):
    out = DummyOutput()
    cmd = VersionCommand(output=out)  # type: ignore[arg-type]

    def boom():  # simulate failure resolving the core package version
        raise ValueError("cannot read")

    monkeypatch.setattr(VersionCommand, "_core_version", staticmethod(boom), raising=True)
    exit_code = cmd.execute()
    assert exit_code == 1
    assert not out.successes
    assert any("Unexpected" in e or "cannot read" in e for e in out.errors)


def test_version_command_reports_core_not_installed(monkeypatch):
    """A missing core package must not crash the command."""
    from importlib.metadata import PackageNotFoundError

    def missing(_name):
        raise PackageNotFoundError(_name)

    monkeypatch.setattr("tenxgraph_api.cli.commands.version._pkg_version", missing)
    out = DummyOutput()
    cmd = VersionCommand(output=out)  # type: ignore[arg-type]

    assert cmd.execute() == 0
    assert any("not installed" in i for i in out.infos)


