"""Configuration management for the 10xGraph CLI."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tenxgraph_api.cli.constants import (
    CONFIG_FILENAMES,
    PROJECT_ROOT,
)
from tenxgraph_api.cli.exceptions import ConfigurationError
from tenxgraph_api.config_names import CONFIG_FILE, LEGACY_CONFIG_FILE
from tenxgraph_api.import_path import is_import_path


def config_names(config_path: str | Path) -> tuple[str, ...]:
    """Names to try for ``config_path``, in priority order.

    The default ``10xgraph.json`` falls back to the pre-rename ``agentflow.json``;
    any other name is used as given.
    """
    name = str(config_path)
    if name == CONFIG_FILE:
        return (CONFIG_FILE, LEGACY_CONFIG_FILE)
    return (name,)


def resolve_default_config(config: str | Path, directory: Path | None = None) -> Path:
    """Find ``config`` in ``directory`` (default: cwd) or any parent, honoring the legacy name.

    Each directory is checked from the nearest upward; within one directory
    ``10xgraph.json`` wins over ``agentflow.json``. An absolute ``config`` is used
    as given. Returns the first existing candidate, or the preferred name in
    ``directory`` when none exists so callers can report or create it.
    """
    base = (directory or Path.cwd()).resolve()
    names = config_names(config)
    for folder in (base, *base.parents):
        for name in names:
            if (folder / name).exists():
                return folder / name
    return base / names[0]


def read_config_json(path: Path) -> dict[str, Any] | None:
    """The config file as a dict, or None when missing, unreadable, bad JSON or not an object."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


class ConfigManager:
    """Manages configuration discovery and validation."""

    def __init__(self, config_path: str | None = None) -> None:
        """Initialize the config manager.

        Args:
            config_path: Optional path to config file
        """
        self.config_path = config_path
        self._config_data: dict[str, Any] | None = None

    def find_config_file(self, config_path: str | Path) -> Path:
        """Find the config file in various locations.

        Args:
            config_path: Path to config file (can be relative or absolute)

        Returns:
            Path to the found config file

        Raises:
            ConfigurationError: If config file is not found
        """
        config_path_obj = Path(config_path)

        # If absolute path is provided, use it directly
        if config_path_obj.is_absolute():
            if not config_path_obj.exists():
                raise ConfigurationError(
                    f"Config file not found at {config_path}",
                    config_path=str(config_path_obj),
                )
            return config_path_obj

        # The shared search: working directory and its parents, nearest first.
        found = resolve_default_config(config_path)
        if found.exists():
            return found

        names = config_names(config_path)
        search_dirs = [*self._working_tree(), Path(__file__).parent.parent, PROJECT_ROOT]
        search_locations = [directory / name for directory in search_dirs for name in names]

        for location in search_locations:
            if location.exists():
                return location

        # If still not found, try package data locations
        package_locations = [PROJECT_ROOT / "tenxgraph_api" / name for name in names]

        for location in package_locations:
            if location.exists():
                return location

        # Generate helpful error message
        searched_paths = search_locations + package_locations
        error_msg = f"Config file '{config_path}' not found in any of these locations:"
        for path in searched_paths:
            error_msg += f"\n  - {path}"

        raise ConfigurationError(error_msg, config_path=str(config_path))

    def auto_discover_config(self) -> Path | None:
        """Automatically discover config file using common names.

        Returns:
            Path to discovered config file or None if not found
        """
        search_dirs = [*self._working_tree(), PROJECT_ROOT]

        for search_dir in search_dirs:
            for config_name in CONFIG_FILENAMES:
                config_path = search_dir / config_name
                if config_path.exists():
                    return config_path

        return None

    @staticmethod
    def _working_tree() -> tuple[Path, ...]:
        """Return cwd followed by its ancestors, nearest first."""
        cwd = Path.cwd().resolve()
        return (cwd, *cwd.parents)

    def load_config(self, config_path: str | None = None) -> dict[str, Any]:
        """Load configuration from file.

        Args:
            config_path: Optional path to config file

        Returns:
            Configuration dictionary

        Raises:
            ConfigurationError: If config loading fails
        """
        if config_path:
            actual_path = self.find_config_file(config_path)
        elif self.config_path:
            actual_path = self.find_config_file(self.config_path)
        else:
            discovered_path = self.auto_discover_config()
            if not discovered_path:
                raise ConfigurationError(
                    "No configuration file found. Please provide a config file path "
                    "or create one of: " + ", ".join(CONFIG_FILENAMES)
                )
            actual_path = discovered_path

        try:
            with actual_path.open("r", encoding="utf-8") as f:
                self._config_data = json.load(f)
        except json.JSONDecodeError as e:
            raise ConfigurationError(
                f"Invalid JSON in config file: {e}",
                config_path=str(actual_path),
            ) from e
        except OSError as e:
            raise ConfigurationError(
                f"Failed to read config file: {e}",
                config_path=str(actual_path),
            ) from e

        # Ensure config data was loaded
        if self._config_data is None:
            raise ConfigurationError(
                "Failed to load configuration data",
                config_path=str(actual_path),
            )

        # Validate configuration
        self._validate_config(self._config_data)

        # Store the resolved path for future use
        self.config_path = str(actual_path)

        return self._config_data

    def _validate_config(self, config_data: dict[str, Any]) -> None:
        """Validate configuration data.

        Args:
            config_data: Configuration to validate

        Raises:
            ConfigurationError: If validation fails
        """
        required_fields = ["agent"]

        for field in required_fields:
            if field not in config_data:
                raise ConfigurationError(
                    f"Missing required field '{field}' in configuration",
                    config_path=self.config_path,
                )

        # Validate agent field
        agent = config_data["agent"]
        if not is_import_path(agent):
            raise ConfigurationError(
                f"Field 'agent' must be 'module:attribute', got {agent!r}",
                config_path=self.config_path,
            )

    def get_config_value(self, key: str, default: Any = None) -> Any:
        """Get a specific configuration value.

        Args:
            key: Configuration key (supports dot notation)
            default: Default value if key not found

        Returns:
            Configuration value or default
        """
        if self._config_data is None:
            return default

        # Support dot notation for nested keys
        keys = key.split(".")
        value = self._config_data

        for k in keys:
            if isinstance(value, dict) and k in value:
                value = value[k]
            else:
                return default

        return value

    def _section(self, key: str) -> dict[str, Any]:
        raw = self.get_config_value(key, default={})
        return raw if isinstance(raw, dict) else {}

    def get_test_config(self) -> dict[str, Any]:
        """Return the optional 'test' section from 10xgraph.json.

        Returns a dict with keys: path, coverage, coverage_threshold.
        All fields are optional; callers should use .get() with their own defaults.
        """
        return self._section("test")

    def get_evaluation_config(self) -> dict[str, Any]:
        """Return the optional 'evaluation' section from 10xgraph.json.

        Returns a dict with keys: directory, output_dir, threshold, timestamp_files.
        All fields are optional; callers should use .get() with their own defaults.
        """
        return self._section("evaluation")

    def resolve_env_file(self) -> Path | None:
        """Resolve environment file path from configuration.

        Returns:
            Path to environment file or None if not configured
        """
        env_file = self.get_config_value("env")
        if not env_file:
            return None

        # If relative path, resolve relative to config file location
        env_path = Path(env_file)
        if not env_path.is_absolute() and self.config_path:
            config_dir = Path(self.config_path).parent
            env_path = config_dir / env_file

        return env_path if env_path.exists() else None
