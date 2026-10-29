"""Tests for CLI validation utilities."""

import tempfile
from pathlib import Path

import pytest

from tenxgraph_api.cli.core.validation import Validator, validate_cli_options
from tenxgraph_api.cli.exceptions import ValidationError


class TestValidatorPort:
    """Tests for port validation."""

    def test_validate_port_valid(self):
        """Test validating valid port numbers."""
        assert Validator.validate_port(80) == 80
        assert Validator.validate_port(443) == 443
        assert Validator.validate_port(8000) == 8000
        assert Validator.validate_port(65535) == 65535
        assert Validator.validate_port(1) == 1

    def test_validate_port_too_low(self):
        """Test validating port number below minimum."""
        with pytest.raises(ValidationError) as exc_info:
            Validator.validate_port(0)
        assert "between 1 and 65535" in str(exc_info.value)

    def test_validate_port_too_high(self):
        """Test validating port number above maximum."""
        with pytest.raises(ValidationError) as exc_info:
            Validator.validate_port(65536)
        assert "between 1 and 65535" in str(exc_info.value)

    def test_validate_port_negative(self):
        """Test validating negative port number."""
        with pytest.raises(ValidationError):
            Validator.validate_port(-1)

    def test_validate_port_not_integer(self):
        """Test validating non-integer port."""
        with pytest.raises(ValidationError) as exc_info:
            Validator.validate_port("8000")
        assert "must be an integer" in str(exc_info.value)

    def test_validate_port_float(self):
        """Test validating float port."""
        with pytest.raises(ValidationError):
            Validator.validate_port(8000.5)


class TestValidatorHost:
    """Tests for host validation."""

    def test_validate_host_valid(self):
        """Test validating valid host addresses."""
        assert Validator.validate_host("localhost") == "localhost"
        assert Validator.validate_host("127.0.0.1") == "127.0.0.1"
        assert Validator.validate_host("example.com") == "example.com"
        assert Validator.validate_host("sub.example.com") == "sub.example.com"

    def test_validate_host_empty(self):
        """Test validating empty host."""
        with pytest.raises(ValidationError) as exc_info:
            Validator.validate_host("")
        assert "cannot be empty" in str(exc_info.value)

    def test_validate_host_whitespace_only(self):
        """Test validating whitespace-only host."""
        with pytest.raises(ValidationError) as exc_info:
            Validator.validate_host("   ")
        assert "cannot be empty" in str(exc_info.value)

    def test_validate_host_too_long(self):
        """Test validating host that's too long."""
        long_host = "a" * 256
        with pytest.raises(ValidationError) as exc_info:
            Validator.validate_host(long_host)
        assert "too long" in str(exc_info.value)

    def test_validate_host_not_string(self):
        """Test validating non-string host."""
        with pytest.raises(ValidationError) as exc_info:
            Validator.validate_host(123)
        assert "must be a string" in str(exc_info.value)

    def test_validate_host_strips_whitespace(self):
        """Test that validation strips whitespace."""
        result = Validator.validate_host("  localhost  ")
        assert result == "localhost"


class TestValidatorPath:
    """Tests for path validation."""

    def test_validate_path_valid_relative(self):
        """Test validating valid relative paths."""
        result = Validator.validate_path("./config.json")
        assert isinstance(result, Path)

    def test_validate_path_valid_absolute(self):
        """Test validating valid absolute paths."""
        result = Validator.validate_path("/etc/config.json")
        assert isinstance(result, Path)

    def test_validate_path_must_exist_true(self):
        """Test validating path that must exist."""
        with tempfile.NamedTemporaryFile(delete=False) as f:
            temp_path = f.name

        try:
            result = Validator.validate_path(temp_path, must_exist=True)
            assert result.exists()
        finally:
            Path(temp_path).unlink()

    def test_validate_path_must_exist_false(self):
        """Test validating path that doesn't need to exist."""
        result = Validator.validate_path("/nonexistent/path.json", must_exist=False)
        assert isinstance(result, Path)

    def test_validate_path_does_not_exist_error(self):
        """Test validating non-existent path when must_exist=True."""
        with pytest.raises(ValidationError) as exc_info:
            Validator.validate_path("/nonexistent/path.json", must_exist=True)
        assert "does not exist" in str(exc_info.value)

    def test_validate_path_invalid_type(self):
        """Test validating invalid path type."""
        with pytest.raises(ValidationError):
            Validator.validate_path(123)

    def test_validate_path_returns_path_object(self):
        """Test that validation returns Path object."""
        result = Validator.validate_path("config.json")
        assert isinstance(result, Path)


class TestValidatorPythonVersion:
    """Tests for Python version validation."""

    def test_validate_python_version_valid_two_parts(self):
        """Test validating valid Python versions with two parts."""
        assert Validator.validate_python_version("3.12") == "3.12"
        assert Validator.validate_python_version("3.13") == "3.13"

    def test_validate_python_version_valid_three_parts(self):
        """Test validating valid Python versions with three parts."""
        assert Validator.validate_python_version("3.12.0") == "3.12.0"

    def test_validate_python_version_too_old(self):
        """Test validating Python version that's too old."""
        with pytest.raises(ValidationError) as exc_info:
            Validator.validate_python_version("3.11.9")
        assert "3.12 or higher" in str(exc_info.value)

    @pytest.mark.parametrize("version", ["3.8", "3.9", "3.10", "3.11", "3.8.10"])
    def test_validate_python_version_below_the_framework_minimum(self, version):
        """The framework needs Python 3.12; an older image would fail at install time."""
        with pytest.raises(ValidationError):
            Validator.validate_python_version(version)

    def test_validate_python_version_very_old(self):
        """Test validating very old Python version."""
        with pytest.raises(ValidationError) as exc_info:
            Validator.validate_python_version("2.7.18")
        assert "3.12 or higher" in str(exc_info.value)

    def test_validate_python_version_invalid_format(self):
        """Test validating Python version with invalid format."""
        with pytest.raises(ValidationError) as exc_info:
            Validator.validate_python_version("3.12.0.1")
        assert "format" in str(exc_info.value)

    def test_validate_python_version_not_string(self):
        """Test validating non-string Python version."""
        with pytest.raises(ValidationError) as exc_info:
            Validator.validate_python_version(3.8)
        assert "must be a string" in str(exc_info.value)

    def test_validate_python_version_no_numbers(self):
        """Test validating Python version with no numbers."""
        with pytest.raises(ValidationError):
            Validator.validate_python_version("python3")


class TestValidatorServiceName:
    """Tests for service name validation."""

    def test_validate_service_name_valid(self):
        """Test validating valid service names."""
        assert Validator.validate_service_name("myservice") == "myservice"
        assert Validator.validate_service_name("service123") == "service123"
        assert Validator.validate_service_name("my-service") == "my-service"
        assert Validator.validate_service_name("my_service") == "my_service"
        assert Validator.validate_service_name("my.service") == "my.service"

    def test_validate_service_name_starts_with_number(self):
        """Test validating service name starting with number."""
        result = Validator.validate_service_name("123service")
        assert result == "123service"

    def test_validate_service_name_empty(self):
        """Test validating empty service name."""
        with pytest.raises(ValidationError) as exc_info:
            Validator.validate_service_name("")
        assert "cannot be empty" in str(exc_info.value)

    def test_validate_service_name_too_long(self):
        """Test validating service name that's too long."""
        long_name = "a" * 64
        with pytest.raises(ValidationError) as exc_info:
            Validator.validate_service_name(long_name)
        assert "63 characters" in str(exc_info.value)

    def test_validate_service_name_invalid_characters(self):
        """Test validating service name with invalid characters."""
        with pytest.raises(ValidationError) as exc_info:
            Validator.validate_service_name("my@service")
        assert "alphanumeric" in str(exc_info.value)

    def test_validate_service_name_not_string(self):
        """Test validating non-string service name."""
        with pytest.raises(ValidationError) as exc_info:
            Validator.validate_service_name(123)
        assert "must be a string" in str(exc_info.value)

    def test_validate_service_name_whitespace(self):
        """Test validating service name with whitespace."""
        result = Validator.validate_service_name("  myservice  ")
        assert result == "myservice"


class TestValidatorConfigStructure:
    """Tests for configuration structure validation."""

    def test_validate_config_valid(self):
        """Test validating valid configuration."""
        config = {"agent": "test_agent"}
        result = Validator.validate_config_structure(config)
        assert result == config

    def test_validate_config_with_extra_fields(self):
        """Test validating configuration with extra fields."""
        config = {"agent": "test_agent", "extra": "value"}
        result = Validator.validate_config_structure(config)
        assert result == config

    def test_validate_config_missing_agent(self):
        """Test validating configuration without agent field."""
        config = {"other": "value"}
        with pytest.raises(ValidationError) as exc_info:
            Validator.validate_config_structure(config)
        assert "agent" in str(exc_info.value)

    def test_validate_config_agent_not_string(self):
        """Test validating configuration with non-string agent."""
        config = {"agent": 123}
        with pytest.raises(ValidationError) as exc_info:
            Validator.validate_config_structure(config)
        assert "must be a string" in str(exc_info.value)

    def test_validate_config_not_dict(self):
        """Test validating non-dict configuration."""
        with pytest.raises(ValidationError) as exc_info:
            Validator.validate_config_structure("not a dict")
        assert "must be a dictionary" in str(exc_info.value)

    def test_validate_config_empty_agent(self):
        """Test validating configuration with empty agent."""
        config = {"agent": ""}
        result = Validator.validate_config_structure(config)
        assert result == config  # Empty string is still a string


class TestValidatorEnvironmentFile:
    """Tests for environment file validation."""

    def test_validate_environment_file_valid(self):
        """Test validating valid environment file."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".env", delete=False) as f:
            f.write("KEY=value\n")
            f.write("DEBUG=true\n")
            temp_path = f.name

        try:
            result = Validator.validate_environment_file(temp_path)
            assert result.is_file()
        finally:
            Path(temp_path).unlink()

    def test_validate_environment_file_with_comments(self):
        """Test validating environment file with comments."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".env", delete=False) as f:
            f.write("# This is a comment\n")
            f.write("KEY=value\n")
            temp_path = f.name

        try:
            result = Validator.validate_environment_file(temp_path)
            assert result.is_file()
        finally:
            Path(temp_path).unlink()

    def test_validate_environment_file_not_found(self):
        """Test validating non-existent environment file."""
        with pytest.raises(ValidationError) as exc_info:
            Validator.validate_environment_file("/nonexistent/.env")
        assert "does not exist" in str(exc_info.value)

    def test_validate_environment_file_invalid_format(self):
        """Test validating environment file with invalid format."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".env", delete=False) as f:
            f.write("INVALID_LINE_WITHOUT_EQUALS\n")
            temp_path = f.name

        try:
            with pytest.raises(ValidationError) as exc_info:
                Validator.validate_environment_file(temp_path)
            assert "Invalid" in str(exc_info.value)
        finally:
            Path(temp_path).unlink()

    def test_validate_environment_file_is_directory(self):
        """Test validating when path is a directory."""
        with tempfile.TemporaryDirectory() as temp_dir:
            with pytest.raises(ValidationError) as exc_info:
                Validator.validate_environment_file(temp_dir)
            assert "is not a file" in str(exc_info.value)


class TestValidateCliOptions:
    """Tests for validate_cli_options convenience function."""

    def test_validate_cli_options_required_only(self):
        """Test validating CLI options with only required fields."""
        result = validate_cli_options(host="localhost", port=8000)
        assert result["host"] == "localhost"
        assert result["port"] == 8000

    def test_validate_cli_options_with_config(self):
        """Test validating CLI options with config file."""
        with tempfile.NamedTemporaryFile(suffix=".json") as f:
            result = validate_cli_options(host="localhost", port=8000, config=f.name)
            assert "config" in result

    def test_validate_cli_options_with_python_version(self):
        """Test validating CLI options with Python version."""
        result = validate_cli_options(host="localhost", port=8000, python_version="3.13")
        assert result["python_version"] == "3.13"

    def test_validate_cli_options_all_options(self):
        """Test validating CLI options with all options."""
        with tempfile.NamedTemporaryFile(suffix=".json") as f:
            result = validate_cli_options(
                host="localhost", port=8000, config=f.name, python_version="3.12"
            )
            assert result["host"] == "localhost"
            assert result["port"] == 8000
            assert "config" in result
            assert result["python_version"] == "3.12"

    def test_validate_cli_options_invalid_host(self):
        """Test validating CLI options with invalid host."""
        with pytest.raises(ValidationError):
            validate_cli_options(host="", port=8000)

    def test_validate_cli_options_invalid_port(self):
        """Test validating CLI options with invalid port."""
        with pytest.raises(ValidationError):
            validate_cli_options(host="localhost", port=70000)
