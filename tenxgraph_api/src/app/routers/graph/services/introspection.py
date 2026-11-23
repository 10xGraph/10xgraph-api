"""Read-only views of the graph: details, tools, run observability and the state schema."""

from typing import Any

from pydantic import BaseModel
from tenxgraph.core.authz import Identity

from tenxgraph_api.src.app.core import logger
from tenxgraph_api.src.app.core.config.settings import is_production
from tenxgraph_api.src.app.core.exceptions.client_errors import (
    as_http_error,
)
from tenxgraph_api.src.app.core.threads import require_thread
from tenxgraph_api.src.app.routers.graph.schemas.graph_schemas import (
    GraphSchema,
    GraphToolsSchema,
    ObservabilitySchema,
    ObsRunSchema,
    ToolNodeSchema,
    ToolSchema,
)
from tenxgraph_api.src.app.routers.graph.services.stream_utils import _log_run_error
from tenxgraph_api.src.app.utils.run_trace import (
    reconstruct_run,
)


class IntrospectionMixin:
    """``GraphService`` methods that read the graph, its runs and its state schema."""

    async def graph_details(self) -> GraphSchema:
        try:
            logger.info("Getting graph details")
            res = self._graph.generate_graph()
            # Surface live/realtime capability so clients can route to /v1/graph/live
            # and gate their UI. Not part of core's generate_graph() output.
            info = res.get("info")
            if isinstance(info, dict):
                info["is_realtime"] = self.is_live_agent
                info["realtime_options"] = self._realtime_options() if self.is_live_agent else None
            return GraphSchema(**res)
        except Exception as e:
            _log_run_error(e, "Failed to get graph details")
            raise as_http_error(e, "Failed to get graph details")

    async def get_tools(self) -> GraphToolsSchema:
        """Collect the tools exposed by every ToolNode in the graph.

        Walks the compiled graph's nodes, and for each ``ToolNode`` calls
        ``all_tools()`` (which resolves local functions, MCP tools from any
        connected server, and remote/client-registered tools). Each tool is
        tagged with its ``source`` so the UI can group them.

        A failing MCP server on one node does not break the endpoint: that node's
        tools are collected best-effort and the error is logged.
        """
        node_schemas: list[ToolNodeSchema] = []
        total_tools = 0

        for node_name, tool_node in self.tool_nodes():
            try:
                raw_tools = await tool_node.all_tools()
            except Exception as e:  # - one bad MCP server shouldn't 500 the page
                logger.warning("Failed to list tools for node '%s': %s", node_name, e)
                raw_tools = []

            # After all_tools(), these hold the names by origin.
            local_names = set(getattr(tool_node, "_funcs", {}).keys())
            mcp_names = set(getattr(tool_node, "mcp_tools", []) or [])
            remote_names = set(getattr(tool_node, "remote_tool_names", []) or [])

            tools: list[ToolSchema] = []
            for entry in raw_tools:
                fn = entry.get("function", {}) if isinstance(entry, dict) else {}
                name = fn.get("name", "")
                if name in mcp_names:
                    source = "mcp"
                elif name in remote_names:
                    source = "remote"
                elif name in local_names:
                    source = "local"
                else:
                    # Unknown origin — default to local (a plain function schema).
                    source = "local"

                tools.append(
                    ToolSchema(
                        name=name,
                        description=fn.get("description", "") or "",
                        source=source,
                        parameters=fn.get("parameters", {}) or {},
                    )
                )

            total_tools += len(tools)
            node_schemas.append(
                ToolNodeSchema(
                    node_name=node_name,
                    tool_count=len(tools),
                    tools=tools,
                )
            )

        return GraphToolsSchema(
            node_count=len(node_schemas),
            tool_count=total_tools,
            nodes=node_schemas,
        )

    async def get_observability(
        self,
        thread_id: str,
        user: Identity | None,
        run_id: str | None = None,
    ) -> "ObservabilitySchema":
        """Reconstruct an observability trace for a thread from captured run events.

        Rebuilds spans (root → node → llm/tool), an event list, and aggregated
        token usage from the telemetry records a run emitted. Returns the latest
        run by default, or the requested ``run_id``.

        The telemetry store is keyed by thread only, so the thread is read with the caller's
        reach first: a thread they cannot see is a 404 (``ThreadNotFoundError``).
        """
        store = self.telemetry
        if not store:
            reason = "disabled_in_production" if is_production() else "store_unavailable"
            return ObservabilitySchema(
                thread_id=str(thread_id), enabled=False, reason=reason, run_count=0
            )

        await require_thread(self.checkpointer, user, thread_id)
        runs = store.get_runs(thread_id)
        run_ids = [r.run_id for r in runs]

        trace = None
        if run_id:
            trace = store.get_run(thread_id, run_id)
        elif runs:
            trace = runs[-1]

        if trace is None:
            return ObservabilitySchema(
                thread_id=str(thread_id),
                run_count=len(runs),
                run_ids=run_ids,
                run=None,
                reason="no_runs",
            )

        run_schema = self._reconstruct_run(trace)
        return ObservabilitySchema(
            thread_id=str(thread_id),
            run_count=len(runs),
            run_ids=run_ids,
            run=run_schema,
        )

    def _reconstruct_run(self, trace: Any) -> "ObsRunSchema":
        """Turn a captured RunTrace into spans + events + usage."""
        return reconstruct_run(trace)

    async def get_state_schema(self) -> dict:
        try:
            logger.info("Getting state schema")
            res: BaseModel = self._graph._state
            return res.model_json_schema()
        except Exception as e:
            _log_run_error(e, "Failed to get state schema")
            raise as_http_error(e, "Failed to get state schema")
