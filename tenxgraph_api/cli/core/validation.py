"""Input validation utilities for the CLI."""

import re
from pathlib import Path
from typing import Any

from tenxgraph_api.cli.exceptions import ValidationError


class Validator:
    """Input validation utilities."""

    @staticmethod
    def validate_port(port: int) -> int:
        """Validate port number.

        Args:
            port: Port number to validate

        Returns:
            Validated port number

        Raises:
            ValidationError: If port is invalid
        """
        if not isinstance(port, int):
            raise ValidationError("Port must be an integer", field="port")

        if port < 1 or port > 65535:  # noqa: PLR2004
            raise ValidationError("Port must be between 1 and 65535", field="port")

        return port

    @staticmethod
    def validate_host(host: str) -> str:
        """Validate host address.

        Args:
            host: Host address to validate

        Returns:
            Validated host address

        Raises:
            ValidationError: If host is invalid
        """
        if not isinstance(host, str):
            raise ValidationError("Host must be a string", field="host")

        if not host.strip():
            raise ValidationError("Host cannot be empty", field="host")

        # Basic validation - could be enhanced with more sophisticated checks
        if len(host) > 255:  # noqa: PLR2004
            raise ValidationError("Host address too long", field="host")

        return host.strip()

    @staticmethod
    def validate_path(path: str | Path, must_exist: bool = False) -> Path:
        """Validate file path.

        Args:
            path: Path to validate
            must_exist: Whether the path must exist

        Returns:
            Validated Path object

        Raises:
            ValidationError: If path is invalid
        """
        try:
            path_obj = Path(path)
        except (TypeError, ValueError) as e:
            raise ValidationError(f"Invalid path: {e}", field="path") from e

        if must_exist and not path_obj.exists():
            raise ValidationError(f"Path does not exist: {path_obj}", field="path")

        return path_obj

    @staticmethod
    def validate_python_version(version: str) -> str:
        """Validate Python version string.

        Args:
            version: Python version to validate

        Returns:
            Validated version string

        Raises:
            ValidationError: If version is invalid
        """
        if not isinstance(version, str):
            raise ValidationError("Python version must be a string", field="python_version")

        # Pattern for semantic versioning (major.minor or major.minor.patch)
        version_pattern = r"^(\d+)\.(\d+)(?:\.(\d+))?$"

        if not re.match(version_pattern, version):
            raise ValidationError(
                "Python version must be in format 'X.Y' or 'X.Y.Z'", field="python_version"
            )

        # Extract major and minor versions
        parts = version.split(".")
        major, minor = int(parts[0]), int(parts[1])

        # The framework requires Python 3.12 (pyproject ``requires-python``); an older image
        # would build and then fail at pip install.
        if (major, minor) < (3, 12):
            raise ValidationError("Python version must be 3.12 or higher", field="python_version")

        return version

    @staticmethod
    def validate_service_name(name: str) -> str:
        """Validate service name for Docker.

        Args:
            name: Service name to validate

        Returns:
            Validated service name

        Raises:
            ValidationError: If name is invalid
        """
        if not isinstance(name, str):
            raise ValidationError("Service name must be a string", field="service_name")

        name = name.strip()
        if not name:
            raise ValidationError("Service name cannot be empty", field="service_name")

        # Docker service name validation
        if not re.match(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]*$", name):
            raise ValidationError(
                "Service name must start with alphanumeric character and "
                "contain only alphanumeric, underscore, period, or hyphen",
                field="service_name",
            )

        if len(name) > 63:  # noqa: PLR2004
            raise ValidationError(
                "Service name must be 63 characters or less", field="service_name"
            )

        return name

    @staticmethod
    def validate_config_structure(config: dict[str, Any]) -> dict[str, Any]:
        """Validate configuration structure.

        Args:
            config: Configuration dictionary to validate

        Returns:
            Validated configuration

        Raises:
            ValidationError: If configuration is invalid
        """
        if not isinstance(config, dict):
            raise ValidationError("Configuration must be a dictionary")

        # Required fields
        required_fields = ["agent"]
        for field in required_fields:
            if field not in config:
                raise ValidationError(f"Missing required field: {field}")

        # Validate agent field
        agent = config["agent"]
        if not isinstance(agent, str):
            raise ValidationError("Field 'agent' must be a string")

        return config

    @staticmethod
    def validate_environment_file(env_file: str | Path) -> Path:
        """Validate environment file.

        Args:
            env_file: Path to environment file

        Returns:
            Validated Path object

        Raises:
            ValidationError: If environment file is invalid
        """
        env_path = Validator.validate_path(env_file, must_exist=True)

        if not env_path.is_file():
            raise ValidationError(f"Environment file is not a file: {env_path}", field="env_file")

        # Basic validation of .env file format
        try:
            with env_path.open("r", encoding="utf-8") as f:
                for line_num, line in enumerate(f, 1):
                    up_line = line.strip()
                    if up_line and not up_line.startswith("#") and "=" not in up_line:
                        raise ValidationError(
                            f"Invalid environment file format at line {line_num}: {up_line}",
                            field="env_file",
                        )
        except UnicodeDecodeError as e:
            raise ValidationError(
                f"Environment file contains invalid characters: {e}", field="env_file"
            ) from e
        except OSError as e:
            raise ValidationError(f"Cannot read environment file: {e}", field="env_file") from e

        return env_path


# Convenience functions for common validations
def validate_cli_options(
    host: str,
    port: int,
    config: str | None = None,
    python_version: str | None = None,
) -> dict[str, Any]:
    """Validate common CLI options.

    Args:
        host: Host address
        port: Port number
        config: Optional config file path
        python_version: Optional Python version

    Returns:
        Dictionary of validated options

    Raises:
        ValidationError: If any option is invalid
    """
    validated = {
        "host": Validator.validate_host(host),
        "port": Validator.validate_port(port),
    }

    if config:
        validated["config"] = Validator.validate_path(config)

    if python_version:
        validated["python_version"] = Validator.validate_python_version(python_version)

    return validated
