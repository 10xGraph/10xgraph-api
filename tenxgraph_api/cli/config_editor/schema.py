"""Declarative description of every ``10xgraph.json`` option the editor shows.

The browser page renders itself from :func:`build_schema`, so adding a new
configuration key only needs a new entry here (plus a rule in ``validation``).

Section shape::

    {
        "id": "rate_limit",  # stable id, used by validation issues
        "title": "Rate limiting",
        "description": "...",
        "key": "rate_limit",  # top-level key, or None for root-level fields
        "optional": True,  # renders an on/off toggle for the whole key
        "off_summary": "...",  # what happens while the key is absent
        "default": {...},  # value written when the toggle is switched on
        "widget": None,  # custom renderer: "auth", "authorization", "remote_tools"
        "fields": [...],  # generic fields, paths are relative to the root
        "notes": ["..."],  # extra hints (secrets that belong in .env, ...)
        "env": [{"name": "REDIS_URL", "purpose": "..."}],  # related variables, read-only
    }

Field shape::

    {
        "path": ["rate_limit", "requests"],
        "label": "Requests per window",
        "type": "text" | "number" | "bool" | "select" | "list",
        "help": "...",
        "placeholder": "...",
        "options": [...],  # select only
        "min": 0,
        "step": 1,  # number only
        "required": False,
        "default": ...,  # value the server assumes when the key is absent
        "unset_label": "...",  # offer a choice that removes the key; says what the server does
        "allow_true": False,  # bool only: render just Default / Off, never On
        "unlimited_value": 0,  # number only: an "Unlimited" checkbox that writes this value
        "null_label": "No limit",  # number only: a choice that writes JSON null
        "exclusive_min": 0,  # number only: the value must be greater than this
        "group": "Limits",  # fields sharing a group render together
        "show_if": {"path": [...], "equals": value},
    }

``build_schema()`` also returns ``environment``: server environment variable groups that are
not tied to one section, as ``[{"title": ..., "vars": [{"name": ..., "purpose": ...}]}]``.
A group may also carry an optional ``note`` (a short string shown under the group title).
The editor only lists names; it never reads a ``.env`` file.
"""

from __future__ import annotations

import copy
from typing import Any


DOCS_URL = "https://10xgraph.com/docs/reference/api-cli/configuration"

# Mirrors authorization.all_scopes(); kept in sync by a unit test so the
# editor never imports the core framework just to render suggestions.
AUTHORIZATION_SCOPES: tuple[str, ...] = (
    "checkpointer:delete",
    "checkpointer:import",
    "checkpointer:read",
    "checkpointer:write",
    "config:read",
    "files:delete",
    "files:read",
    "files:upload",
    "graph:fix",
    "graph:invoke",
    "graph:read",
    "graph:stop",
    "graph:stream",
    "store:delete",
    "store:read",
    "store:write",
)

BUILTIN_AUTHORIZATION: tuple[str, ...] = ("ownership", "allow_all", "default", "none")

# Keys read by the CLI or the API server. Anything else is preserved but flagged.
KNOWN_TOP_LEVEL_KEYS: frozenset[str] = frozenset(
    {
        "agent",
        "env",
        "auth",
        "authorization",
        "token_revocation",
        "graph_introspection",
        "cache_store",
        "rate_limit",
        "websocket",
        "runs",
        "ag_ui",
        "media",
        "observability",
        "injectq",
        "store",
        "checkpointer",
        "thread_name_generator",
        "redis",
        "remote_tools",
        "test",
        "evaluation",
    }
)

_SECTIONS: list[dict[str, Any]] = [
    {
        "id": "core",
        "title": "Core",
        "description": "The compiled graph the server runs and the dotenv file it loads.",
        "key": None,
        "optional": False,
        "fields": [
            {
                "path": ["agent"],
                "label": "Agent graph",
                "type": "text",
                "required": True,
                "placeholder": "graph.react:app",
                "help": "Import path of the compiled graph, as module:attribute.",
            },
            {
                "path": ["env"],
                "label": "Env file",
                "type": "text",
                "placeholder": ".env",
                "help": "Dotenv file loaded before the graph starts. Leave empty to skip.",
            },
        ],
    },
    {
        "id": "auth",
        "title": "Authentication",
        "description": "Who is calling the API. Leave off for local development.",
        "key": "auth",
        "optional": True,
        "off_summary": "Every request is accepted without authentication.",
        "default": "jwt",
        "widget": "auth",
        "fields": [],
        "notes": [
            "JWT needs JWT_SECRET_KEY and JWT_ALGORITHM in your .env file.",
            "Custom auth points at a BaseAuth subclass, as module:attribute.",
        ],
        "env": [
            {
                "name": "JWT_SECRET_KEY",
                "purpose": "Signing secret for JWT tokens. Required when auth is jwt.",
            },
            {
                "name": "JWT_ALGORITHM",
                "purpose": "Signing algorithm for JWT tokens. Defaults to HS256.",
            },
            {
                "name": "JWT_ISSUER",
                "purpose": "When set, tokens must carry a matching iss claim.",
            },
            {
                "name": "JWT_AUDIENCE",
                "purpose": "When set, tokens must carry a matching aud claim.",
            },
        ],
    },
    {
        "id": "authorization",
        "title": "Authorization",
        "description": (
            "What an authenticated user may do. When off, ownership is enforced whenever "
            "auth is on (and always in production)."
        ),
        "key": "authorization",
        "optional": True,
        "off_summary": (
            "Ownership checks are enforced when auth is on or in production; with auth off in "
            "development there is no identity to check."
        ),
        "default": "ownership",
        "widget": "authorization",
        "fields": [],
        "scopes": list(AUTHORIZATION_SCOPES),
    },
    {
        "id": "token_revocation",
        "title": "Token revocation",
        "description": (
            "Reject revoked tokens (10xgraph revoke) before they expire. Revocations live in "
            "the API's cache store."
        ),
        "key": "token_revocation",
        "optional": True,
        "off_summary": "Tokens are accepted until they expire.",
        "default": True,
        "fields": [
            {
                "path": ["token_revocation", "enabled"],
                "default": True,
                "label": "Enabled",
                "type": "bool",
                "help": "Turn off to keep the settings without checking revocations.",
            },
            {
                "path": ["token_revocation", "token_lifetime"],
                "label": "Token lifetime (seconds)",
                "type": "number",
                "exclusive_min": 0,
                "step": 1,
                "placeholder": "3600",
                "help": (
                    "The longest lifetime of the tokens you issue. Dates a token that has no "
                    "iat claim from its exp, for user revocations."
                ),
            },
            {
                "path": ["token_revocation", "user_ttl"],
                "default": 2592000,
                "label": "User revocation kept (seconds)",
                "type": "number",
                "exclusive_min": 0,
                "step": 1,
                "null_label": "Until deleted",
                "help": "How long a revoke --user entry is kept by default.",
            },
        ],
        "notes": [
            "Fails closed: while the cache store is unreachable, authenticated requests get "
            "503, and the server refuses to start.",
            "Share revocations between workers with a shared cache store (redis, REDIS_URL or "
            "cache_store under Plugins). With the in-memory store a revocation reaches "
            "one process only, and 10xgraph revoke cannot reach it.",
        ],
        "env": [
            {
                "name": "REDIS_URL",
                "purpose": "Redis for the cache store when neither redis nor cache_store is set.",
            },
        ],
    },
    {
        "id": "graph_introspection",
        "title": "Graph introspection",
        "description": "Who may read the graph structure, tool schemas and state schema.",
        "key": None,
        "optional": False,
        "fields": [
            {
                "path": ["graph_introspection"],
                "label": "Graph introspection",
                "type": "select",
                "options": ["open", "scoped"],
                "unset_label": "Automatic: scoped in production, open in development",
                "help": (
                    "Who may read the graph structure, tool schemas and state schema. scoped "
                    "requires the graph:read scope."
                ),
            },
        ],
    },
    {
        "id": "rate_limit",
        "title": "Rate limiting",
        "description": "Sliding-window request limits applied to every REST call.",
        "key": "rate_limit",
        "optional": True,
        "off_summary": "Requests are not rate limited.",
        "default": {
            "enabled": True,
            "requests": 100,
            "window": 60,
            "by": "ip",
        },
        "fields": [
            {
                "path": ["rate_limit", "enabled"],
                "group": "Limits",
                "default": True,
                "label": "Enabled",
                "type": "bool",
                "help": "Turn off to keep the settings without enforcing them.",
            },
            {
                "path": ["rate_limit", "requests"],
                "group": "Limits",
                "label": "Requests per window",
                "type": "number",
                "min": 1,
                "step": 1,
                "placeholder": "100",
            },
            {
                "path": ["rate_limit", "window"],
                "group": "Limits",
                "label": "Window (seconds)",
                "type": "number",
                "min": 1,
                "step": 1,
                "placeholder": "60",
            },
            {
                "path": ["rate_limit", "by"],
                "group": "Limits",
                "default": "ip",
                "label": "Limit by",
                "type": "select",
                "options": ["ip", "user", "global"],
                "help": "Count requests per client IP, per authenticated user, or all together.",
            },
            {
                "path": ["rate_limit", "fail_open"],
                "group": "Limits",
                "default": True,
                "label": "Fail open",
                "type": "bool",
                "help": (
                    "Allow requests while the cache store errors. Off means deny them with 503 "
                    "RATE_LIMIT_UNAVAILABLE, and the server refuses to start without it."
                ),
            },
            {
                "path": ["rate_limit", "trusted_proxy_headers"],
                "group": "Proxy",
                "default": False,
                "label": "Trust X-Forwarded-For",
                "type": "bool",
                "help": "Only when the app sits behind a proxy you control.",
            },
            {
                "path": ["rate_limit", "trusted_proxy_hops"],
                "group": "Proxy",
                "label": "Trusted proxy hops",
                "type": "number",
                "min": 1,
                "step": 1,
                "placeholder": "1",
                "show_if": {"path": ["rate_limit", "trusted_proxy_headers"], "equals": True},
            },
            {
                "path": ["rate_limit", "trusted_proxies"],
                "group": "Proxy",
                "label": "Trusted proxy networks",
                "type": "list",
                "placeholder": "10.0.0.0/8",
                "help": (
                    "IPs or CIDR ranges your proxy connects from. X-Forwarded-For is ignored "
                    "for anyone else, so clients that reach the app directly cannot spoof it. "
                    "Empty means loopback and private networks only."
                ),
                "show_if": {"path": ["rate_limit", "trusted_proxy_headers"], "equals": True},
            },
            {
                "path": ["rate_limit", "exclude_paths"],
                "group": "Excluded paths",
                "label": "Extra paths to skip",
                "type": "list",
                "placeholder": "/docs",
                "help": (
                    "Paths that are never rate limited. /ping and /health/ready are always "
                    "skipped. API docs paths (/docs, /redocs, /openapi.json) are limited unless "
                    "listed here. The server default is an empty list."
                ),
            },
        ],
        "notes": [
            "Counters live in the API's cache store: shared between workers with redis, "
            "REDIS_URL or cache_store (see Plugins), per process otherwise.",
        ],
        "env": [
            {
                "name": "REDIS_URL",
                "purpose": "Redis for the cache store when neither redis nor cache_store is set.",
            },
        ],
    },
    {
        "id": "websocket",
        "title": "WebSocket",
        "description": (
            "WebSocket streaming and realtime audio, and caps on concurrent connections "
            "(shared between workers with a shared cache store, per process otherwise)."
        ),
        "key": "websocket",
        "optional": True,
        "off_summary": (
            "/v1/graph/ws and /v1/graph/live stay mounted with the default caps "
            "(1000 connections, 10 per user)."
        ),
        "default": {"max_connections": 1000, "max_connections_per_user": 10},
        "fields": [
            {
                "path": ["websocket", "enabled"],
                "label": "Enabled",
                "type": "bool",
                "unset_label": (
                    "Default: routes stay mounted; only a warning if the websockets package "
                    "is missing"
                ),
                "help": (
                    "On mounts /v1/graph/ws and /v1/graph/live and refuses to start without "
                    'the websockets package (pip install "10xgraph-api[websocket]"). Off: '
                    "neither route exists."
                ),
            },
            {
                "path": ["websocket", "max_connections"],
                "label": "Max connections",
                "type": "number",
                "min": 0,
                "step": 1,
                "placeholder": "1000",
                "unlimited_value": 0,
                "help": (
                    "Per worker process unless the cache store is shared. Defaults to 1000; 0 "
                    "or null means unlimited."
                ),
            },
            {
                "path": ["websocket", "max_connections_per_user"],
                "label": "Max connections per user",
                "type": "number",
                "min": 0,
                "step": 1,
                "placeholder": "10",
                "unlimited_value": 0,
                "help": (
                    "Stops one account holding every slot. Defaults to 10; 0 or null means "
                    "unlimited."
                ),
            },
            {
                "path": ["websocket", "realtime_models"],
                "label": "Realtime models clients may pick",
                "type": "list",
                "placeholder": "gemini-3.8-live",
                "help": (
                    "Models a /v1/graph/live client may request. Any other request uses the "
                    "agent's own model. Empty means clients cannot choose."
                ),
            },
            {
                "path": ["websocket", "realtime_client_overrides"],
                "label": "Realtime options clients may set",
                "type": "list",
                "placeholder": "voice",
                "default": ["voice", "modalities", "vad"],
                "help": (
                    "Init-frame options a /v1/graph/live client may set: voice, modalities, vad, "
                    "system_prompt. Defaults to voice, modalities and vad. Add system_prompt "
                    "only if every caller may rewrite the agent's instructions."
                ),
            },
        ],
    },
    {
        "id": "runs",
        "title": "Runs",
        "description": "Who names threads, which client input reaches the graph, and run limits.",
        "key": "runs",
        "optional": True,
        "off_summary": (
            "The server picks new thread ids, any non-reserved input key reaches the graph, "
            "each user may hold 10 HTTP runs at once, and runs have no time limit."
        ),
        "default": {"thread_ids": "server", "max_concurrent_per_user": 10},
        "fields": [
            {
                "path": ["runs", "thread_ids"],
                "label": "New thread ids",
                "type": "select",
                "options": ["client", "server"],
                "default": "server",
                "help": (
                    "server (default): a request may only name a thread that exists, so nobody "
                    "can claim an id before its owner. New threads are started without an id. "
                    "AG-UI and realtime clients still pick their own ids. client: any request "
                    "may start a thread under an id it picks."
                ),
            },
            {
                "path": ["runs", "config_keys"],
                "label": "Allowed config keys",
                "type": "list",
                "placeholder": "locale",
                "help": (
                    "When set, a request config with any other key is rejected. thread_id is "
                    "always allowed. Empty means any non-reserved key."
                ),
            },
            {
                "path": ["runs", "state_keys"],
                "label": "Allowed initial_state keys",
                "type": "list",
                "placeholder": "preferences",
                "help": "When set, initial_state with any other key is rejected.",
            },
            {
                "path": ["runs", "max_concurrent_per_user"],
                "label": "Max concurrent runs per user",
                "type": "number",
                "min": 0,
                "step": 1,
                "placeholder": "10",
                "default": 10,
                "unlimited_value": 0,
                "help": (
                    "HTTP runs in flight per user (per process unless the cache store is "
                    "shared). 0 or null means unlimited."
                ),
            },
            {
                "path": ["runs", "timeout"],
                "label": "Run timeout (seconds)",
                "type": "number",
                "exclusive_min": 0,
                "null_label": "No limit",
                "step": 1,
                "help": (
                    "A run that takes longer is cancelled. Must be greater than 0; decimals "
                    "are allowed. Empty uses the default: 300 in production (MODE=production), "
                    "no limit in development. No limit writes null, which also removes the "
                    "production limit."
                ),
            },
            {
                "path": ["runs", "max_parallel_tool_calls"],
                "label": "Max parallel tool calls",
                "type": "number",
                "min": 1,
                "step": 1,
                "placeholder": "10",
                "help": (
                    "Tool calls one model turn may run at once. Empty uses core's default, "
                    "10, or the TENXGRAPH_MAX_PARALLEL_TOOL_CALLS environment variable."
                ),
            },
        ],
        "env": [
            {
                "name": "TENXGRAPH_MAX_PARALLEL_TOOL_CALLS",
                "purpose": (
                    "Tool calls one turn may run at once when max_parallel_tool_calls is "
                    "not set above."
                ),
            },
            {
                "name": "MODE",
                "purpose": (
                    "production gives runs a 300 second timeout when none is set above; "
                    "development sets no limit."
                ),
            },
        ],
    },
    {
        "id": "ag_ui",
        "title": "AG-UI",
        "description": "Serve the graph over the AG-UI protocol, for clients such as CopilotKit.",
        "key": "ag_ui",
        "optional": True,
        "off_summary": "The AG-UI endpoint (POST /v1/ag-ui) is not mounted.",
        "default": {"enabled": True},
        "fields": [
            {
                "path": ["ag_ui", "enabled"],
                "default": False,
                "label": "Enabled",
                "type": "bool",
                "help": "Mounts POST /v1/ag-ui. It uses the same auth as /v1/graph/stream.",
            },
            {
                "path": ["ag_ui", "allow_client_tools"],
                "default": True,
                "label": "Allow client tools",
                "type": "bool",
                "help": (
                    "Offer the tools a client sends (e.g. CopilotKit useFrontendTool) to the "
                    "model for that run. Off: only tools listed in remote_tools."
                ),
            },
        ],
        "notes": [
            'Needs the ag-ui extra: pip install "10xgraph-api[ag-ui]".',
            "Client tools run in the caller's browser, cannot reuse a server tool's name, and are "
            "size-limited.",
        ],
    },
    {
        "id": "media",
        "title": "Uploaded files",
        "description": "What happens to uploaded files when the messages that use them go away.",
        "key": "media",
        "optional": True,
        "off_summary": "Files are deleted together with the last thread that uses them.",
        "default": {"delete_with_thread": True},
        "fields": [
            {
                "path": ["media", "delete_with_thread"],
                "default": True,
                "label": "Delete files with their thread",
                "type": "bool",
                "help": (
                    "When the last message or thread that references an uploaded file is "
                    "deleted, delete the file too. Off: nothing is deleted automatically "
                    "(use DELETE /v1/files/{file_id}). Files uploaded before this setting "
                    "existed are never deleted automatically."
                ),
            },
        ],
        "env": [
            {
                "name": "MEDIA_STORAGE_TYPE",
                "purpose": "Where uploads are stored: memory, local or cloud. Defaults to local.",
            },
            {
                "name": "MEDIA_STORAGE_PATH",
                "purpose": "Upload folder when the storage type is local. Defaults to ./uploads.",
            },
            {
                "name": "MEDIA_MAX_SIZE_MB",
                "purpose": "Largest accepted upload in megabytes. Defaults to 25.",
            },
            {
                "name": "DOCUMENT_HANDLING",
                "purpose": (
                    "How uploaded documents reach the model: extract_text, pass_raw or skip. "
                    "Defaults to extract_text."
                ),
            },
            {
                "name": "MEDIA_ALLOWED_CONTENT_TYPES",
                "purpose": (
                    "Comma-separated upload allowlist such as image/*,application/pdf. "
                    "Empty accepts any type."
                ),
            },
            {
                "name": "MEDIA_REQUIRE_OWNER",
                "purpose": (
                    "Deny files with no recorded owner. Unset means on in production, off in "
                    "development."
                ),
            },
            {
                "name": "MEDIA_CLOUD_PROVIDER",
                "purpose": "Cloud provider, aws or gcp. Used when the storage type is cloud.",
            },
            {
                "name": "MEDIA_CLOUD_BUCKET",
                "purpose": "Bucket name. Required when the storage type is cloud.",
            },
            {
                "name": "MEDIA_CLOUD_REGION",
                "purpose": "Bucket region. Defaults to us-east-1.",
            },
            {
                "name": "MEDIA_CLOUD_PREFIX",
                "purpose": "Key prefix for stored objects. Defaults to 10xgraph-media.",
            },
            {
                "name": "MEDIA_CLOUD_ACCESS_KEY_ID",
                "purpose": "Access key id for cloud storage.",
            },
            {
                "name": "MEDIA_CLOUD_SECRET_ACCESS_KEY",
                "purpose": "Secret access key for cloud storage.",
            },
            {
                "name": "MEDIA_CLOUD_SESSION_TOKEN",
                "purpose": "Temporary session token for cloud storage credentials.",
            },
            {
                "name": "MEDIA_CLOUD_PROJECT_ID",
                "purpose": "Project id when the cloud provider is gcp.",
            },
            {
                "name": "MEDIA_CLOUD_CREDENTIALS_JSON",
                "purpose": "Service account credentials as JSON for cloud storage.",
            },
            {
                "name": "MEDIA_SIGNED_URL_TTL_SECONDS",
                "purpose": "How long a signed download URL stays valid. Defaults to 3600.",
            },
        ],
    },
    {
        "id": "observability",
        "title": "Observability",
        "description": "Trace graph runs to Logfire and/or LangSmith.",
        "key": "observability",
        "optional": True,
        "off_summary": "Graph runs are not traced.",
        "default": {
            "level": "standard",
            "logfire": {"enabled": False},
            "langsmith": {"enabled": False},
        },
        "fields": [
            {
                "path": ["observability", "level"],
                "group": "Tracing",
                "default": "standard",
                "label": "Level",
                "type": "select",
                "options": ["spans", "standard", "full"],
                "help": "full records prompts and tool I/O and may contain PII.",
            },
            {
                "path": ["observability", "user_id_mode"],
                "group": "Tracing",
                "default": "raw",
                "label": "User id in traces",
                "type": "select",
                "options": ["raw", "hash", "mask"],
                "help": (
                    "raw sends the user id as is; hash sends a SHA-256 of it (keyed when "
                    "TENXGRAPH_TRACE_USER_ID_KEY is set); mask hides the middle of it."
                ),
            },
            {
                "path": ["observability", "logfire", "enabled"],
                "group": "Logfire",
                "default": False,
                "label": "Logfire",
                "type": "bool",
            },
            {
                "path": ["observability", "logfire", "service_name"],
                "group": "Logfire",
                "label": "Logfire service name",
                "type": "text",
                "placeholder": "my-agent",
                "show_if": {"path": ["observability", "logfire", "enabled"], "equals": True},
            },
            {
                "path": ["observability", "logfire", "send_to_logfire"],
                "group": "Logfire",
                "default": True,
                "label": "Send to Logfire",
                "type": "bool",
                "show_if": {"path": ["observability", "logfire", "enabled"], "equals": True},
            },
            {
                "path": ["observability", "logfire", "console"],
                "group": "Logfire",
                "label": "Print spans to console",
                "type": "bool",
                "unset_label": "Default: Logfire's own console behavior",
                "allow_true": False,
                "help": (
                    "Only Default or Off work from this file. Logfire takes False or a "
                    "ConsoleOptions object, which JSON cannot express, so true is rejected."
                ),
                "show_if": {"path": ["observability", "logfire", "enabled"], "equals": True},
            },
            {
                "path": ["observability", "langsmith", "enabled"],
                "group": "LangSmith",
                "default": False,
                "label": "LangSmith",
                "type": "bool",
            },
            {
                "path": ["observability", "langsmith", "project"],
                "group": "LangSmith",
                "label": "LangSmith project",
                "type": "text",
                "placeholder": "my-agent",
                "show_if": {"path": ["observability", "langsmith", "enabled"], "equals": True},
            },
            {
                "path": ["observability", "langsmith", "endpoint"],
                "group": "LangSmith",
                "label": "LangSmith endpoint",
                "type": "text",
                "placeholder": "https://api.smith.langchain.com/otel",
                "show_if": {"path": ["observability", "langsmith", "enabled"], "equals": True},
            },
        ],
        "notes": ["Tokens stay in .env: LOGFIRE_TOKEN and LANGSMITH_API_KEY."],
        "env": [
            {
                "name": "LOGFIRE_TOKEN",
                "purpose": "Logfire write token. Needed to send spans to Logfire.",
            },
            {
                "name": "LANGSMITH_API_KEY",
                "purpose": "LangSmith API key. Required when LangSmith is enabled.",
            },
            {
                "name": "TENXGRAPH_TRACE_USER_ID_KEY",
                "purpose": "Secret that keys the hash of user ids in traces when the mode is hash.",
            },
            {
                "name": "OTEL_ENABLED",
                "purpose": "Export OpenTelemetry traces from the API server. Defaults to false.",
            },
            {
                "name": "OTEL_LEVEL",
                "purpose": "Detail of the OpenTelemetry traces: spans, standard or full.",
            },
            {
                "name": "OTEL_SERVICE_NAME",
                "purpose": "Service name on OpenTelemetry traces. Defaults to 10xgraph-api.",
            },
            {
                "name": "OTEL_EXPORTER_OTLP_ENDPOINT",
                "purpose": "OTLP endpoint that receives the traces.",
            },
        ],
    },
    {
        "id": "plugins",
        "title": "Plugins",
        "description": "Optional objects loaded by import path. Leave a field empty to skip it.",
        "key": None,
        "optional": False,
        "fields": [
            {
                "path": ["injectq"],
                "group": "Import paths",
                "label": "InjectQ container",
                "type": "text",
                "placeholder": "graph.dependencies:container",
            },
            {
                "path": ["store"],
                "group": "Import paths",
                "label": "Memory store",
                "type": "text",
                "placeholder": "graph.dependencies:store",
                "help": "A BaseStore instance for long-term memory.",
            },
            {
                "path": ["checkpointer"],
                "group": "Import paths",
                "label": "Checkpointer",
                "type": "text",
                "placeholder": "graph.dependencies:checkpointer",
                "help": (
                    "Not applied by the API server yet. Pass the checkpointer to "
                    "graph.compile() instead."
                ),
            },
            {
                "path": ["thread_name_generator"],
                "group": "Import paths",
                "label": "Thread name generator",
                "type": "text",
                "placeholder": "graph.thread_name_generator:MyNameGenerator",
            },
            {
                "path": ["redis"],
                "group": "Cache store",
                "label": "Redis URL",
                "type": "text",
                "placeholder": "${REDIS_URL}",
                "help": (
                    "The API's cache store (run locks and caps, idempotency, rate limits, "
                    "ownership cache, token revocation) on Redis, Valkey, Dragonfly or KeyDB. "
                    "Supports $VAR and ${VAR}. Empty falls back to REDIS_URL, then to an "
                    "in-memory store per process."
                ),
            },
            {
                "path": ["cache_store"],
                "group": "Cache store",
                "label": "Custom cache store",
                "type": "text",
                "placeholder": "graph.dependencies:cache_store",
                "help": (
                    "module:attr of a BaseCacheStore instance or zero-argument factory. Wins "
                    "over the Redis URL."
                ),
            },
        ],
        "env": [
            {
                "name": "REDIS_URL",
                "purpose": "Redis for the cache store when neither redis nor cache_store is set.",
            },
        ],
    },
    {
        "id": "remote_tools",
        "title": "Remote tools",
        "description": "Tools the model can call that run in the client, not on the server.",
        "key": "remote_tools",
        "optional": True,
        "off_summary": "The model can only call tools that run on the server.",
        "default": [],
        "widget": "remote_tools",
        "fields": [],
    },
    {
        "id": "test",
        "title": "Testing",
        "description": "Defaults for 10xgraph test.",
        "key": "test",
        "optional": True,
        "off_summary": "10xgraph test uses its built-in defaults.",
        "default": {"path": "tests", "coverage": False},
        "fields": [
            {"path": ["test", "path"], "label": "Tests path", "type": "text"},
            {"path": ["test", "coverage"], "label": "Coverage", "type": "bool", "default": False},
            {
                "path": ["test", "coverage_threshold"],
                "label": "Coverage threshold (%)",
                "type": "number",
                "min": 0,
                "step": 1,
                "placeholder": "80",
                "show_if": {"path": ["test", "coverage"], "equals": True},
            },
        ],
    },
    {
        "id": "evaluation",
        "title": "Evaluation",
        "description": "Defaults for 10xgraph eval.",
        "key": "evaluation",
        "optional": True,
        "off_summary": "10xgraph eval uses its built-in defaults.",
        "default": {
            "directory": "evals",
            "output_dir": "eval_reports",
            "threshold": 0.75,
            "parallel": False,
            "max_concurrency": 4,
            "open_report": True,
        },
        "fields": [
            {"path": ["evaluation", "directory"], "label": "Eval directory", "type": "text"},
            {"path": ["evaluation", "output_dir"], "label": "Report directory", "type": "text"},
            {
                "path": ["evaluation", "threshold"],
                "label": "Pass threshold",
                "type": "number",
                "min": 0,
                "step": 0.05,
                "help": "Between 0 and 1.",
            },
            {
                "path": ["evaluation", "parallel"],
                "label": "Run in parallel",
                "type": "bool",
                "default": False,
            },
            {
                "path": ["evaluation", "max_concurrency"],
                "label": "Max concurrency",
                "type": "number",
                "min": 1,
                "step": 1,
                "show_if": {"path": ["evaluation", "parallel"], "equals": True},
            },
            {
                "path": ["evaluation", "open_report"],
                "label": "Open HTML report",
                "type": "bool",
                "default": True,
                "help": "Open the HTML report in your browser when 10xgraph eval finishes.",
            },
        ],
    },
]

_ENVIRONMENT: list[dict[str, Any]] = [
    {
        "title": "Mode and logging",
        "vars": [
            {
                "name": "MODE",
                "purpose": (
                    "development or production. Production turns on stricter defaults, "
                    "including the API docs being off."
                ),
            },
            {"name": "LOG_LEVEL", "purpose": "Python log level such as INFO or DEBUG."},
            {
                "name": "STRICT_PRODUCTION_CHECKS",
                "purpose": (
                    "Refuse to start in production with wildcard ORIGINS or ALLOWED_HOST, or "
                    "docs enabled. Unset means on in production; false only warns."
                ),
            },
            {
                "name": "STARTUP_CHECKS",
                "purpose": (
                    "Refuse to start while the checkpointer, the store or a required cache store "
                    "is unreachable (the cache store only for token_revocation or rate_limit "
                    "with fail_open false), in development and production. Defaults to true; "
                    "false skips the check."
                ),
            },
            {
                "name": "STARTUP_CHECK_TIMEOUT_SECONDS",
                "purpose": (
                    "Seconds startup keeps retrying an unreachable backend before it gives up. "
                    "Defaults to 30."
                ),
            },
        ],
    },
    {
        "title": "CORS and hosts",
        "vars": [
            {
                "name": "ORIGINS",
                "purpose": "Allowed CORS origins, comma-separated. Defaults to *.",
            },
            {
                "name": "ALLOWED_HOST",
                "purpose": "Comma-separated host names the server answers to. Defaults to *.",
            },
            {
                "name": "CORS_ALLOW_CREDENTIALS",
                "purpose": (
                    "Let cross-origin requests carry cookies and auth headers. Defaults to "
                    "true; production refuses it together with ORIGINS=*."
                ),
            },
        ],
    },
    {
        "title": "Paths and limits",
        "vars": [
            {
                "name": "ROOT_PATH",
                "purpose": "URL prefix when the app is served behind a proxy path.",
            },
            {
                "name": "DOCS_PATH",
                "purpose": "Swagger docs path. Empty turns them off; off by default in production.",
            },
            {
                "name": "REDOCS_PATH",
                "purpose": "ReDoc docs path. Empty turns them off; off by default in production.",
            },
            {
                "name": "MAX_REQUEST_SIZE",
                "purpose": "Largest request body in bytes. Defaults to 10 MB.",
            },
        ],
    },
    {
        "title": "Security headers",
        "vars": [
            {
                "name": "SECURITY_HEADERS_ENABLED",
                "purpose": "Add security headers to every response. Defaults to true.",
            },
            {
                "name": "HSTS_ENABLED",
                "purpose": "Send Strict-Transport-Security. Defaults to true.",
            },
            {"name": "HSTS_MAX_AGE", "purpose": "HSTS max-age in seconds. Defaults to one year."},
            {
                "name": "HSTS_INCLUDE_SUBDOMAINS",
                "purpose": "Add includeSubDomains to the HSTS header. Defaults to true.",
            },
            {
                "name": "HSTS_PRELOAD",
                "purpose": "Add preload to the HSTS header. Defaults to false.",
            },
            {"name": "FRAME_OPTIONS", "purpose": "X-Frame-Options value. Defaults to DENY."},
            {
                "name": "CONTENT_TYPE_OPTIONS",
                "purpose": "X-Content-Type-Options value. Defaults to nosniff.",
            },
            {
                "name": "REFERRER_POLICY",
                "purpose": "Referrer-Policy value. Defaults to strict-origin-when-cross-origin.",
            },
            {
                "name": "PERMISSIONS_POLICY",
                "purpose": "Permissions-Policy value. Unset uses the built-in policy.",
            },
            {
                "name": "CSP_POLICY",
                "purpose": "Content-Security-Policy value. Unset uses the built-in policy.",
            },
        ],
    },
    {
        "title": "Error tracking",
        "vars": [
            {
                "name": "SENTRY_DSN",
                "purpose": "Sentry project DSN. Error tracking is off without it.",
            },
            {
                "name": "SENTRY_TRACES_SAMPLE_RATE",
                "purpose": "Share of requests traced, 0 to 1. Defaults to 0.1.",
            },
            {
                "name": "SENTRY_PROFILES_SAMPLE_RATE",
                "purpose": "Share of requests profiled, 0 to 1. Defaults to 0.",
            },
        ],
    },
    {
        "title": "Snowflake IDs (optional)",
        "note": (
            "Only needed if you use SnowFlakeIdGenerator with no arguments "
            "(needs the snowflakekit extra). Otherwise leave these unset."
        ),
        "vars": [
            {
                "name": "SNOWFLAKE_EPOCH",
                "purpose": "Epoch in milliseconds for Snowflake ids.",
            },
            {
                "name": "SNOWFLAKE_NODE_ID",
                "purpose": "Node id for Snowflake ids. Give each machine a different one.",
            },
            {
                "name": "SNOWFLAKE_WORKER_ID",
                "purpose": "Worker id for Snowflake ids. Make it unique per process on a node.",
            },
            {
                "name": "SNOWFLAKE_TIME_BITS",
                "purpose": "Bits for the timestamp part of a Snowflake id.",
            },
            {"name": "SNOWFLAKE_NODE_BITS", "purpose": "Bits for the node part of a Snowflake id."},
            {
                "name": "SNOWFLAKE_WORKER_BITS",
                "purpose": "Bits for the worker part of a Snowflake id.",
            },
            {
                "name": "SNOWFLAKE_TOTAL_BITS",
                "purpose": "Total bits in a Snowflake id. Defaults to 64.",
            },
        ],
    },
]


def build_schema() -> dict[str, Any]:
    """Return a fresh, JSON-serialisable copy of the editor schema."""
    return {
        "docs_url": DOCS_URL,
        "sections": copy.deepcopy(_SECTIONS),
        "environment": copy.deepcopy(_ENVIRONMENT),
    }


def section_for_key(key: str) -> str:
    """Map a top-level config key to the id of the section that renders it."""
    for section in _SECTIONS:
        if section["key"] == key:
            return section["id"]
        if section["key"] is None and any(f["path"][0] == key for f in section["fields"]):
            return section["id"]
    return "core"
