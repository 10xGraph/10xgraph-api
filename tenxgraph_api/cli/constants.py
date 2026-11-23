"""CLI constants and configuration values."""

import tomllib
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version
from pathlib import Path
from typing import Final

from tenxgraph_api.config_names import CONFIG_FILE, LEGACY_CONFIG_FILE


_PACKAGE_NAME: Final[str] = "10xgraph-api"


def _resolve_version() -> str:
    """Resolve the package version from installed metadata, falling back to pyproject.toml.

    Single source of truth: the installed distribution's version. When running from a
    source checkout that is not installed, read ``pyproject.toml`` instead.
    """
    try:
        return _pkg_version(_PACKAGE_NAME)
    except PackageNotFoundError:
        pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
        try:
            with pyproject.open("rb") as f:
                return tomllib.load(f).get("project", {}).get("version", "unknown")
        except (OSError, tomllib.TOMLDecodeError):
            return "unknown"


# Version information (single-sourced from package metadata / pyproject.toml)
CLI_VERSION: Final[str] = _resolve_version()

# Default configuration values
DEFAULT_HOST: Final[str] = "127.0.0.1"
DEFAULT_PORT: Final[int] = 8000
# The names live with the server (it cannot import the CLI); the CLI reuses them.
DEFAULT_CONFIG_FILE: Final[str] = CONFIG_FILE
DEFAULT_PLAYGROUND_URL: Final[str] = "https://play.10xgraph.com"
DEFAULT_PYTHON_VERSION: Final[str] = "3.13"
DEFAULT_SERVICE_NAME: Final[str] = "10xgraph-api"

# File paths and names
# Discovery order within one directory: the first match wins.
CONFIG_FILENAMES: Final[list[str]] = [
    DEFAULT_CONFIG_FILE,
    LEGACY_CONFIG_FILE,
    ".agentflow.json",
    "agentflow.config.json",
]

REQUIREMENTS_PATHS: Final[list[str]] = [
    "requirements.txt",
    "requirements/requirements.txt",
    "requirements/base.txt",
    "requirements/production.txt",
]

# Logging configuration
LOG_FORMAT: Final[str] = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
LOG_DATE_FORMAT: Final[str] = "%Y-%m-%d %H:%M:%S"

# Project structure
PROJECT_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
