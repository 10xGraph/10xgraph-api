import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from tenxgraph_api.src.app.core.config.graph_config import GraphConfig


def test_graph_config_reads_agent(tmp_path: Path):
    cfg_path = tmp_path / "cfg.json"
    data = {
        "agent": "mod:func",
        "checkpointer": "ckpt:fn",
        "store": "store.mod:store",
    }
    cfg_path.write_text(json.dumps(data))

    cfg = GraphConfig(str(cfg_path))
    assert cfg.graph_path == "mod:func"
    assert cfg.checkpointer_path == "ckpt:fn"
    assert cfg.store_path == "store.mod:store"


def test_graph_config_missing_agent_raises(tmp_path: Path):
    cfg_path = tmp_path / "cfg.json"
    data = {}
    cfg_path.write_text(json.dumps(data))

    with pytest.raises(ValueError):
        _ = GraphConfig(str(cfg_path)).graph_path


def test_graph_config_validates_remote_tools(tmp_path: Path):
    cfg_path = tmp_path / "cfg.json"
    cfg_path.write_text(
        json.dumps(
            {
                "agent": "mod:func",
                "remote_tools": [
                    {
                        "node": "tools",
                        "name": "write_report",
                        "description": "Write a report on the client.",
                        "parameters": {"type": "object"},
                    }
                ],
            }
        )
    )

    tool = GraphConfig(str(cfg_path)).remote_tools[0]
    assert tool.node_name == "tools"
    assert tool.parameters == {"type": "object", "properties": {}, "required": []}


def test_graph_config_rejects_remote_tool_key_typos(tmp_path: Path):
    cfg_path = tmp_path / "cfg.json"
    cfg_path.write_text(
        json.dumps(
            {
                "agent": "mod:func",
                "remote_tools": [
                    {
                        "nod": "tools",
                        "name": "write_report",
                        "description": "Write a report on the client.",
                    }
                ],
            }
        )
    )

    with pytest.raises(ValueError, match="Invalid remote_tools configuration") as exc_info:
        _ = GraphConfig(str(cfg_path)).remote_tools
    assert isinstance(exc_info.value.__cause__, ValidationError)


def test_graph_config_rejects_duplicate_remote_tools(tmp_path: Path):
    tool = {
        "node_name": "tools",
        "name": "write_report",
        "description": "Write a report on the client.",
    }
    cfg_path = tmp_path / "cfg.json"
    cfg_path.write_text(json.dumps({"agent": "mod:func", "remote_tools": [tool, tool]}))

    with pytest.raises(ValueError, match="Duplicate remote tool name 'write_report'"):
        _ = GraphConfig(str(cfg_path)).remote_tools


def test_default_config_path_prefers_10xgraph_json(tmp_path: Path, monkeypatch):
    from tenxgraph_api.src.app.core.config.graph_config import default_config_path

    monkeypatch.chdir(tmp_path)
    assert default_config_path() == "10xgraph.json"

    (tmp_path / "agentflow.json").write_text(json.dumps({"agent": "old:app"}))
    assert default_config_path() == "agentflow.json"
    assert GraphConfig().graph_path == "old:app"

    (tmp_path / "10xgraph.json").write_text(json.dumps({"agent": "new:app"}))
    assert default_config_path() == "10xgraph.json"
    assert GraphConfig().graph_path == "new:app"
