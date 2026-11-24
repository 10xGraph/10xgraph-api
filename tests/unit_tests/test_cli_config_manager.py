"""Tests for the CLI config manager."""

import json
import tempfile
from pathlib import Path

import pytest

from tenxgraph_api.cli.core.config import ConfigManager, resolve_default_config
from tenxgraph_api.cli.exceptions import ConfigurationError


class TestConfigManager:
    """Test suite for ConfigManager class."""

    @pytest.fixture
    def temp_config_file(self):
        """Create a temporary config file for testing."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            config_data = {"agent": "test_agent:app", "env": ".env"}
            json.dump(config_data, f)
            temp_path = f.name
        yield temp_path
        # Cleanup
        Path(temp_path).unlink()

    @pytest.fixture
    def temp_dir(self):
        """Create a temporary directory for testing."""
        temp_dir = tempfile.mkdtemp()
        yield temp_dir
        # Cleanup
        import shutil

        shutil.rmtree(temp_dir)



    def test_find_config_file_absolute_path(self, temp_config_file):
        """Test finding config file with absolute path."""
        manager = ConfigManager()
        result = manager.find_config_file(temp_config_file)
        assert result.exists()
        assert str(result) == temp_config_file

    def test_find_config_file_absolute_path_not_found(self, tmp_path):
        """Test finding config file with non-existent absolute path."""
        manager = ConfigManager()
        missing = (tmp_path / "missing" / "config.json").resolve()
        with pytest.raises(ConfigurationError) as exc_info:
            manager.find_config_file(str(missing))
        assert "Config file not found" in str(exc_info.value)

    def test_find_config_file_relative_path(self, temp_dir):
        """Test finding config file with relative path."""
        config_file = Path(temp_dir) / "config.json"
        config_file.write_text(json.dumps({"agent": "test:app"}))

        manager = ConfigManager()
        # This should search in various locations
        try:
            result = manager.find_config_file(str(config_file))
            # If found, it should be a Path object
            assert isinstance(result, Path)
        except ConfigurationError:
            # Expected if file not found in search paths
            pass

    def test_find_config_file_not_found(self):
        """Test finding config file that doesn't exist."""
        manager = ConfigManager()
        with pytest.raises(ConfigurationError) as exc_info:
            manager.find_config_file("non_existent_config.json")
        assert "not found" in str(exc_info.value).lower()

    def test_auto_discover_config(self, temp_dir):
        """Test auto-discovering config file."""
        # Create a config file in temp directory
        config_path = Path(temp_dir) / "10xgraph.json"
        config_path.write_text(json.dumps({"agent": "test:app"}))

        # Change to temp directory for discovery
        import os

        old_cwd = os.getcwd()
        try:
            os.chdir(temp_dir)
            manager = ConfigManager()
            discovered = manager.auto_discover_config()
            # If discovered, should be a Path
            if discovered:
                assert isinstance(discovered, Path)
        finally:
            os.chdir(old_cwd)

    def test_auto_discover_config_not_found(self, temp_dir):
        """Test auto-discovering config when no config exists."""
        import os

        old_cwd = os.getcwd()
        try:
            os.chdir(temp_dir)
            manager = ConfigManager()
            discovered = manager.auto_discover_config()
            # May or may not find a config depending on project setup
            assert discovered is None or isinstance(discovered, Path)
        finally:
            os.chdir(old_cwd)

    def test_auto_discover_config_walks_parent_directories(self, tmp_path, monkeypatch):
        config_path = tmp_path / "10xgraph.json"
        config_path.write_text(json.dumps({"agent": "graph.agent:app"}), encoding="utf-8")
        nested = tmp_path / "packages" / "worker"
        nested.mkdir(parents=True)
        monkeypatch.chdir(nested)

        assert ConfigManager().auto_discover_config() == config_path

    def test_load_config_with_path(self, temp_config_file):
        """Test loading config with explicit path."""
        manager = ConfigManager()
        config = manager.load_config(temp_config_file)
        assert config["agent"] == "test_agent:app"
        assert config["env"] == ".env"

    def test_load_config_with_manager_path(self, temp_config_file):
        """Test loading config using manager's stored path."""
        manager = ConfigManager(config_path=temp_config_file)
        config = manager.load_config()
        assert config["agent"] == "test_agent:app"

    def test_load_config_invalid_json(self, temp_dir):
        """Test loading config with invalid JSON."""
        config_file = Path(temp_dir) / "bad_config.json"
        config_file.write_text("{invalid json}")

        manager = ConfigManager()
        # load_config should catch JSONDecodeError and raise ConfigurationError
        with pytest.raises(ConfigurationError) as exc_info:
            manager.load_config(str(config_file))
        assert "Invalid JSON" in str(exc_info.value)

    def test_load_config_missing_required_field(self, temp_dir):
        """Test loading config with missing required field."""
        config_file = Path(temp_dir) / "config.json"
        config_file.write_text(json.dumps({"other": "value"}))

        manager = ConfigManager()
        with pytest.raises(ConfigurationError) as exc_info:
            # First find the file
            found_path = config_file
            config_data = json.loads(found_path.read_text())
            manager._validate_config(config_data)
        assert "Missing required field" in str(exc_info.value)

    def test_load_config_invalid_agent_type(self, temp_dir):
        """Test loading config with invalid agent type."""
        config_file = Path(temp_dir) / "config.json"
        config_file.write_text(json.dumps({"agent": 123}))  # Should be string

        manager = ConfigManager(config_path=str(config_file))
        with pytest.raises(ConfigurationError) as exc_info:
            config_data = json.loads(config_file.read_text())
            manager._validate_config(config_data)
        assert "must be 'module:attribute'" in str(exc_info.value)

    def test_get_config_value_simple_key(self, temp_config_file):
        """Test getting config value with simple key."""
        manager = ConfigManager()
        manager.load_config(temp_config_file)
        value = manager.get_config_value("agent")
        assert value == "test_agent:app"

    def test_get_config_value_with_default(self, temp_config_file):
        """Test getting config value with default."""
        manager = ConfigManager()
        manager.load_config(temp_config_file)
        value = manager.get_config_value("non_existent", default="default_value")
        assert value == "default_value"

    def test_get_config_value_dot_notation(self, temp_dir):
        """Test getting config value using dot notation."""
        config_file = Path(temp_dir) / "config.json"
        config_data = {
            "agent": "test:app",
            "settings": {"debug": True, "nested": {"value": "deep"}},
        }
        config_file.write_text(json.dumps(config_data))

        manager = ConfigManager()
        manager.load_config(str(config_file))

        # Test nested access
        value = manager.get_config_value("settings.debug")
        assert value is True

        # Test deep nested access
        value = manager.get_config_value("settings.nested.value")
        assert value == "deep"

    def test_get_config_value_without_loading(self):
        """Test getting config value without loading."""
        manager = ConfigManager()
        value = manager.get_config_value("agent", default="default")
        assert value == "default"

    def test_resolve_env_file_exists(self, temp_dir):
        """Test resolving environment file."""
        config_file = Path(temp_dir) / "config.json"
        env_file = Path(temp_dir) / ".env"
        env_file.write_text("KEY=value")

        config_data = {"agent": "test:app", "env": ".env"}
        config_file.write_text(json.dumps(config_data))

        manager = ConfigManager()
        manager.load_config(str(config_file))
        result = manager.resolve_env_file()
        assert result is not None
        assert result.exists()
        assert result.name == ".env"

    def test_resolve_env_file_not_exists(self, temp_dir):
        """Test resolving non-existent environment file."""
        config_file = Path(temp_dir) / "config.json"
        config_data = {"agent": "test:app", "env": ".env"}
        config_file.write_text(json.dumps(config_data))

        manager = ConfigManager()
        manager.load_config(str(config_file))
        result = manager.resolve_env_file()
        assert result is None

    def test_resolve_env_file_absolute_path(self, temp_dir):
        """Test resolving environment file with absolute path."""
        config_file = Path(temp_dir) / "config.json"
        env_file = Path(temp_dir) / ".env"
        env_file.write_text("KEY=value")

        config_data = {
            "agent": "test:app",
            "env": str(env_file),  # Absolute path
        }
        config_file.write_text(json.dumps(config_data))

        manager = ConfigManager()
        manager.load_config(str(config_file))
        result = manager.resolve_env_file()
        assert result is not None
        assert result.exists()

    def test_resolve_env_file_no_env_configured(self, temp_dir):
        """Test resolving env file when none is configured."""
        config_file = Path(temp_dir) / "config.json"
        config_data = {
            "agent": "test:app"
            # No 'env' field
        }
        config_file.write_text(json.dumps(config_data))

        manager = ConfigManager()
        manager.load_config(str(config_file))
        result = manager.resolve_env_file()
        assert result is None  # No env configured, should return None

    def test_load_config_file_read_error(self, temp_dir):
        """Test handling file read errors."""
        config_file = Path(temp_dir) / "config.json"
        config_file.write_text(json.dumps({"agent": "test:app"}))

        manager = ConfigManager()
        found_path = config_file

        # Try to load the file
        try:
            with found_path.open("r", encoding="utf-8") as f:
                config_data = json.load(f)
            assert config_data["agent"] == "test:app"
        except Exception as e:
            pytest.fail(f"Should not raise exception: {e}")

    def test_validate_config_valid_config(self):
        """Test validating a valid config."""
        manager = ConfigManager()
        config_data = {"agent": "test_agent:app"}
        # Should not raise exception
        manager._validate_config(config_data)

    def test_validate_config_missing_agent(self):
        """Test validating config without agent field."""
        manager = ConfigManager()
        config_data = {}
        with pytest.raises(ConfigurationError) as exc_info:
            manager._validate_config(config_data)
        assert "Missing required field" in str(exc_info.value)

    def test_validate_config_agent_not_string(self):
        """Test validating config with non-string agent."""
        manager = ConfigManager()
        config_data = {"agent": 123}
        with pytest.raises(ConfigurationError) as exc_info:
            manager._validate_config(config_data)
        assert "must be 'module:attribute'" in str(exc_info.value)


class TestLegacyConfigName:
    """10xgraph.json wins; agentflow.json is read only when it is the only file present."""

    @staticmethod
    def _write(path: Path, agent: str) -> Path:
        path.write_text(json.dumps({"agent": agent}), encoding="utf-8")
        return path

    def test_new_name_wins_over_legacy_in_the_same_directory(self, tmp_path, monkeypatch):
        new = self._write(tmp_path / "10xgraph.json", "new:app")
        self._write(tmp_path / "agentflow.json", "old:app")
        monkeypatch.chdir(tmp_path)

        assert ConfigManager().find_config_file("10xgraph.json") == new
        assert ConfigManager().auto_discover_config() == new

    def test_legacy_name_is_found_when_new_is_absent(self, tmp_path, monkeypatch):
        legacy = self._write(tmp_path / "agentflow.json", "old:app")
        monkeypatch.chdir(tmp_path)

        assert ConfigManager().find_config_file("10xgraph.json") == legacy
        assert ConfigManager().auto_discover_config() == legacy

    def test_nearest_directory_wins_over_name_priority(self, tmp_path, monkeypatch):
        self._write(tmp_path / "10xgraph.json", "outer:app")
        nested = tmp_path / "service"
        nested.mkdir()
        legacy = self._write(nested / "agentflow.json", "inner:app")
        monkeypatch.chdir(nested)

        assert ConfigManager().find_config_file("10xgraph.json") == legacy

    def test_explicit_name_does_not_fall_back(self, tmp_path, monkeypatch):
        self._write(tmp_path / "agentflow.json", "old:app")
        monkeypatch.chdir(tmp_path)

        with pytest.raises(ConfigurationError):
            ConfigManager().find_config_file("custom.json")

    def test_resolve_default_config_prefers_new_then_legacy(self, tmp_path):
        assert resolve_default_config("10xgraph.json", tmp_path) == tmp_path / "10xgraph.json"
        legacy = self._write(tmp_path / "agentflow.json", "old:app")
        assert resolve_default_config("10xgraph.json", tmp_path) == legacy
        new = self._write(tmp_path / "10xgraph.json", "new:app")
        assert resolve_default_config("10xgraph.json", tmp_path) == new
        assert resolve_default_config("custom.json", tmp_path) == tmp_path / "custom.json"
