"""Default templates for CLI initialization."""

from __future__ import annotations

import json
from typing import Final


# How long a worker gets to finish in-flight work after SIGTERM.
#
# An agent run is not a typical web request: the LLM client alone allows up to
# 600s, and tools add more on top. Gunicorn's default graceful timeout is 30s, so
# every rolling deploy was hard-killing runs that were still in progress, ~30s in.
# Matching the LLM budget means a run in flight gets to finish rather than being
# truncated. Kubernetes needs a slightly larger window than the app, so the pod is
# not killed while the app is still draining.
GRACEFUL_TIMEOUT_SECONDS: Final[int] = 600

# Gunicorn kills a worker it considers hung. Long LLM calls must not look hung.
WORKER_TIMEOUT_SECONDS: Final[int] = 660

# Uvicorn stops waiting for open responses after this many seconds and cancels them, so the
# lifespan shutdown (graph.aclose: the three SHUTDOWN_*_SECONDS settings, 50 s by default) runs
# before gunicorn's --graceful-timeout SIGKILLs the worker. Open NDJSON streams get a final
# "interrupted" chunk just before it.
# Keep in step with ``uvicorn_worker.DEFAULT_GRACEFUL_SHUTDOWN_SECONDS``.
UVICORN_GRACEFUL_SHUTDOWN_SECONDS: Final[int] = GRACEFUL_TIMEOUT_SECONDS - 60

# The container runtime's own kill timer must be longer than gunicorn's, or SIGKILL lands
# while workers are still draining and the lifespan shutdown never runs.
SHUTDOWN_KILL_MARGIN_SECONDS: Final[int] = 60

# `docker compose down` waits this long before SIGKILL.
COMPOSE_STOP_GRACE_SECONDS: Final[int] = GRACEFUL_TIMEOUT_SECONDS + SHUTDOWN_KILL_MARGIN_SECONDS

# terminationGracePeriodSeconds must exceed the app's graceful timeout, plus the
# preStop sleep that lets the load balancer stop sending new traffic first (it runs
# inside the same period).
K8S_PRESTOP_SLEEP_SECONDS: Final[int] = 15
K8S_TERMINATION_GRACE_SECONDS: Final[int] = (
    GRACEFUL_TIMEOUT_SECONDS + K8S_PRESTOP_SLEEP_SECONDS + SHUTDOWN_KILL_MARGIN_SECONDS
)

# Gunicorn worker class: uvicorn worker with timeout_graceful_shutdown, a draining flag for
# /health/ready and a final chunk for open streams.
GUNICORN_WORKER_CLASS: Final[str] = "tenxgraph_api.src.app.uvicorn_worker.TenxGraphUvicornWorker"

# Fixed, numeric uid/gid for the image user. Kubernetes can only verify
# ``runAsNonRoot`` against a numeric USER, and a fixed id keeps volume ownership
# (``fsGroup``, named volumes) stable across rebuilds.
APP_UID: Final[int] = 10001


# Default configuration template
DEFAULT_CONFIG_JSON: Final[str] = json.dumps(
    {
        "agent": "graph.react:app",
        "env": ".env",
        "auth": None,
        "checkpointer": None,
        "injectq": None,
        "store": None,
        "remote_tools": [],
        "thread_name_generator": None,
        "observability": None,
    },
    indent=2,
)

# Production templates (mirroring root repo tooling for convenience)
DEFAULT_PRE_COMMIT: Final[str] = """repos:
    - repo: https://github.com/pre-commit/pre-commit-hooks
        rev: v6.0.0
        hooks:
            - id: check-yaml
                exclude: ^(tests|docs|examples)/
            - id: trailing-whitespace
                exclude: ^(tests|docs|examples)/
            - id: check-added-large-files
                args: [--maxkb=100]
                exclude: ^(tests|docs|examples)/
            - id: check-ast
                exclude: ^(tests|docs|examples)/
            - id: check-builtin-literals
                exclude: ^(tests|docs|examples)/
            - id: check-case-conflict
                exclude: ^(tests|docs|examples)/
            - id: check-docstring-first
                exclude: ^(tests|docs|examples)/
            - id: check-merge-conflict
                exclude: ^(tests|docs|examples)/
            - id: debug-statements
                exclude: ^(tests|docs|examples)/
            - id: detect-private-key
                exclude: ^(tests|docs|examples)/

    - repo: https://github.com/asottile/pyupgrade
        rev: v3.17.0
        hooks:
            - id: pyupgrade
                args: [--py310-plus]
                exclude: ^(tests|docs|examples)/

    - repo: https://github.com/astral-sh/ruff-pre-commit
        rev: v0.5.7
        hooks:
            - id: ruff-format
                exclude: ^(tests|docs|examples)/

    - repo: https://github.com/astral-sh/ruff-pre-commit
        rev: v0.5.7
        hooks:
            - id: ruff
                args: [--fix, --exit-non-zero-on-fix]
                exclude: ^(tests|docs|examples)/

    - repo: https://github.com/PyCQA/bandit
        rev: 1.7.9
        hooks:
            - id: bandit
                args: [-c, pyproject.toml]
                additional_dependencies: ["bandit[toml]"]
                exclude: ^(tests|docs|examples)/
"""

DEFAULT_PYPROJECT: Final[str] = """[build-system]
requires = ["setuptools>=61.0", "wheel"]
build-backend = "setuptools.build_meta"

[project]
name = "10xgraph-api-app"
version = "0.1.0"
description = "10xGraph API application"
readme = "README.md"
license = {text = "MIT"}
requires-python = ">=3.12"
authors = [
        {name = "Your Name", email = "you@example.com"},
]
maintainers = [
        {name = "Your Name", email = "you@example.com"},
]
keywords = ["10xgraph", "api", "fastapi", "cli"]
classifiers = [
        "Development Status :: 4 - Beta",
        "Intended Audience :: Developers",
        "License :: OSI Approved :: MIT License",
        "Operating System :: OS Independent",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
        "Programming Language :: Python :: 3.12",
        "Programming Language :: Python :: 3.13",
]
dependencies = [
        "10xgraph-api",
]

[project.scripts]
10xgraph = "tenxgraph_api.cli.main:main"

[tool.ruff]
line-length = 100
target-version = "py312"
lint.fixable = ["ALL"]
lint.select = [
    "E", "W", "F", "PL", "I", "B", "A", "S", "ISC", "ICN", "PIE", "Q",
    "RET", "SIM", "TID", "RUF", "YTT", "UP", "C4", "PTH", "G", "INP", "T20",
]
lint.ignore = [
    "UP006", "UP007", "RUF012", "G004", "B904", "B008", "ISC001",
]
lint.dummy-variable-rgx = "^(_+|(_+[a-zA-Z0-9_]*[a-zA-Z0-9]+?))$"
exclude = [
    "venv/*",
]

[tool.ruff.lint.mccabe]
max-complexity = 10

[tool.ruff.lint.per-file-ignores]
"bin/*.py" = ["E402", "S603", "T201", "S101"]
"*/tests/*.py" = ["E402", "S603", "T201", "S101"]
"*/test/*.py" = ["E402", "S603", "T201", "S101"]
"scripts/*.py" = ["E402", "S603", "T201", "S101", "INP001"]
"*/__init__.py" = ["E402", "S603", "T201", "S101"]
"*/migrations/*.py" = ["E402", "S603", "T201", "S101"]

[tool.ruff.lint.isort]
lines-after-imports = 2

[tool.ruff.lint.pylint]
max-args = 10

[tool.ruff.format]
quote-style = "double"
indent-style = "space"
skip-magic-trailing-comma = false
line-ending = "auto"
docstring-code-format = true

[tool.ruff.lint.pydocstyle]
convention = "google"

[tool.bandit]
exclude_dirs = ["*/tests/*", "*/tenxgraph_api/tests/*"]
skips = ["B101", "B611", "B601", "B608"]

[tool.pytest.ini_options]
env = ["ENVIRONMENT=pytest"]
testpaths = ["tests"]
pythonpath = ["."]
filterwarnings = ["ignore::DeprecationWarning"]
addopts = [
    "--cov=tenxgraph_api", "--cov-report=html", "--cov-report=term-missing",
    "--cov-report=xml", "--cov-fail-under=0", "--strict-markers", "-v"
]

[tool.coverage.run]
source = ["tenxgraph_api"]
branch = true
omit = [
    "*/__init__.py", "*/tests/*", "*/migrations/*", "*/scripts/*", "*/venv/*", "*/.venv/*",
]

[tool.coverage.report]
exclude_lines = [
    "if __name__ == '__main__':", "pragma: no cover", "@abc.abstractmethod", "@abstractmethod",
    "raise NotImplementedError",
]
show_missing = true

[tool.coverage.paths]
source = ["tenxgraph_api", "*/site-packages/tenxgraph_api"]

[tool.pytest-env]
ENVIRONMENT = "pytest"
"""


# Docker templates
# The published name on PyPI (pyproject.toml `name`).
CLI_PACKAGE = "10xgraph-api"


# Directories under ``tenxgraph_api/cli`` that only the CLI uses on a developer machine.
# The server imports nothing from them (it needs ``cli/constants.py`` only).
CLI_ONLY_ASSET_DIRS: Final[tuple[str, ...]] = ("templates", "config_editor")

# One-liner run in the image after install. ``find_spec`` locates the package without
# importing it, and a missing package is skipped rather than failing the build. Run with
# ``python -I`` so only the installed copy in site-packages can match, never a checkout on
# the working directory or PYTHONPATH.
_STRIP_CLI_ASSETS = (
    "import importlib.util, pathlib, shutil; "
    "spec = importlib.util.find_spec('tenxgraph_api'); "
    "cli = pathlib.Path(spec.origin).parent / 'cli' if spec and spec.origin else None; "
    f"[shutil.rmtree(cli / d, ignore_errors=True) for d in {CLI_ONLY_ASSET_DIRS!r} if cli]"
)

# Removes the CLI-only libraries (typer, rich, questionary, ...) unless something else needs
# them; see ``tenxgraph_api/_slim_image.py``. Skipped when the installed 10xgraph-api is older
# than that module, so a pinned older version still builds.
_SLIM_IMAGE = (
    "import importlib.util, runpy; "
    "importlib.util.find_spec('tenxgraph_api._slim_image') "
    "and runpy.run_module('tenxgraph_api._slim_image', run_name='__main__')"
)


def generate_dockerfile_content(
    python_version: str,
    port: int,
    requirements_file: str,
    has_requirements: bool,
    omit_cmd: bool = False,
    has_pyproject: bool = False,
) -> str:
    """Generate the content for the Dockerfile.

    Dependencies come from ``requirements_file`` when there is one, otherwise from the
    ``[project].dependencies`` of ``pyproject.toml`` (what ``10xgraph init`` scaffolds), and
    only as a last resort from the published CLI package alone.

    The image is built in two stages. Dependencies are installed into a virtualenv in a
    throwaway builder stage, and only that virtualenv reaches the final image, so pip,
    compilers and install leftovers never ship.
    """
    dockerfile_lines = [
        "# Dockerfile for 10xGraph API",
        "# Generated by 10xgraph-api CLI",
        "",
        "# ---- Stage 1: install dependencies into a virtualenv ----",
        "# For reproducible production builds, pin both FROM lines by digest",
        f"# (python:{python_version}-slim@sha256:...) and install from a hash-locked",
        "# requirements file (pip install --require-hashes -r requirements.txt).",
        f"FROM python:{python_version}-slim AS builder",
        "",
        "ENV PIP_NO_CACHE_DIR=1",
        "ENV PIP_DISABLE_PIP_VERSION_CHECK=1",
        "WORKDIR /build",
        "RUN python -m venv /opt/venv",
        'ENV PATH="/opt/venv/bin:$PATH"',
        "",
        "# Every 10xGraph dependency ships prebuilt wheels, so no compiler is needed. If one",
        "# of your own dependencies has to build from source, uncomment this. It only lives",
        "# in the builder stage and never reaches the final image.",
        "# RUN apt-get update \\",
        "#     && apt-get install -y --no-install-recommends build-essential \\",
        "#     && rm -rf /var/lib/apt/lists/*",
        "",
    ]

    if has_requirements:
        dockerfile_lines.extend(
            [
                "# Install exactly the dependencies listed in your requirements file",
                f"COPY {requirements_file} requirements.txt",
                "RUN pip install -r requirements.txt",
                "",
            ]
        )
    elif has_pyproject:
        dockerfile_lines.extend(
            [
                "# Install exactly the [project].dependencies listed in pyproject.toml.",
                "# pip cannot read that list on its own without installing the project as a",
                "# package, so it is printed to a temporary file inside this builder stage.",
                "# The file is never in your project folder and never reaches the final image.",
                "COPY pyproject.toml .",
                "RUN python -c \"import tomllib; deps = tomllib.load(open('pyproject.toml', 'rb'))"
                "['project'].get('dependencies', []); print('\\\\n'.join(deps))\" "
                "> /tmp/requirements.txt \\",
                "    && pip install -r /tmp/requirements.txt",
                "",
            ]
        )
    else:
        dockerfile_lines.extend(
            [
                "# No requirements.txt or pyproject.toml: install the 10xGraph API server only",
                "# (it brings uvicorn with it)",
                f"RUN pip install {CLI_PACKAGE}",
                "",
            ]
        )

    dockerfile_lines.extend(
        [
            "# Production process manager: gunicorn supervising uvicorn workers. These are",
            "# not dependencies of 10xgraph-api, so the image installs them here.",
            'RUN pip install "gunicorn>=23,<27" "uvicorn-worker>=0.4,<1"',
            "",
            "# Strip CLI-only assets the server never loads: project scaffolds, the bundled",
            "# agent skill, and the local config editor (HTML/JS). Only the server code and",
            "# its dependencies stay in the image.",
            f'RUN python -I -c "{_STRIP_CLI_ASSETS}"',
            "",
            "# Remove the CLI's own libraries (typer, rich, questionary and what only they pull",
            "# in). A package that one of your dependencies still needs is kept.",
            f'RUN python -I -c "{_SLIM_IMAGE}"',
            "",
            "# The server never runs pip, so drop it from the virtualenv the runtime copies",
            "# (about 13 MB). The base image keeps its own pip for debugging.",
            "RUN pip uninstall -y pip",
            "",
            "# ---- Stage 2: runtime image ----",
            f"FROM python:{python_version}-slim",
            "",
            "# Set environment variables",
            "ENV PYTHONDONTWRITEBYTECODE=1",
            "ENV PYTHONUNBUFFERED=1",
            "ENV PYTHONPATH=/app",
            'ENV PATH="/opt/venv/bin:$PATH"',
            "# Default to production; overridable via .env or runtime env.",
            "# In production the local in-memory telemetry store stays disabled",
            "# (use OTEL / a publisher for observability instead).",
            "ENV MODE=production",
            "ENV IS_DEBUG=false",
            "# Gunicorn worker count. Gunicorn reads WEB_CONCURRENCY natively; a sane default",
            "# here beats gunicorn's built-in default of 1 worker. Tune to your CPU/memory",
            "# at deploy time, e.g. `docker run -e WEB_CONCURRENCY=8 ...`.",
            "ENV WEB_CONCURRENCY=2",
            "",
            "# Create a non-root user with a fixed uid that owns the work directory.",
            "# uploads/ exists up front so a volume mounted there inherits that owner.",
            f"RUN groupadd -r -g {APP_UID} appuser \\",
            f"    && useradd -r -u {APP_UID} -g appuser -d /app -s /usr/sbin/nologin appuser \\",
            "    && mkdir -p /app/uploads && chown appuser:appuser /app /app/uploads",
            "WORKDIR /app",
            "",
            "# Only the installed virtualenv comes over from the builder",
            "COPY --from=builder /opt/venv /opt/venv",
            "",
            "# Copy application code. --chown sets ownership while copying; a separate",
            "# `chown -R` afterwards would write a second copy of every file into a new layer.",
            "COPY --chown=appuser:appuser . .",
            "# Numeric, so Kubernetes runAsNonRoot can verify it",
            f"USER {APP_UID}:{APP_UID}",
            "",
            "# Expose port",
            f"EXPOSE {port}",
            "",
            "# Health check (Python stdlib, so the image needs no curl)",
            "HEALTHCHECK --interval=30s --timeout=30s --start-period=5s --retries=3 \\",
            '    CMD python -c "import urllib.request; '
            f"urllib.request.urlopen('http://127.0.0.1:{port}/ping', timeout=5)\" || exit 1",
            "",
        ]
    )

    if not omit_cmd:
        dockerfile_lines.extend(
            [
                "# Run the application (production)",
                "# Use Gunicorn with Uvicorn workers for better performance and multi-core",
                "# utilization",
                "#",
                "# --graceful-timeout is critical: an agent run can legitimately take",
                "# minutes (the LLM client alone allows up to 600s). Gunicorn's default",
                "# graceful timeout is 30s, so on every rolling deploy SIGTERM would",
                "# hard-kill runs that were still mid-flight, truncating them. Give",
                "# in-flight runs time to finish draining instead.",
                (
                    f'CMD ["gunicorn", "-k", "{GUNICORN_WORKER_CLASS}", '
                    f'"-b", "0.0.0.0:{port}", '
                    f'"--graceful-timeout", "{GRACEFUL_TIMEOUT_SECONDS}", '
                    f'"--timeout", "{WORKER_TIMEOUT_SECONDS}", '
                    '"tenxgraph_api.src.app.main:app"]'
                ),
                "",
            ]
        )

    return "\n".join(dockerfile_lines)


def generate_k8s_manifest_content(service_name: str, port: int) -> str:
    """Generate a Kubernetes Deployment + Service manifest.

    Exists because rolling deploys were the main way in-flight agent runs got
    truncated, and no manifest was generated at all -- so every user hand-rolled
    one, typically with the default 30s grace period that kills a run mid-LLM-call.

    The three settings that matter here:

    - ``terminationGracePeriodSeconds`` must exceed the app's own graceful timeout,
      or the kubelet SIGKILLs the pod while it is still draining.
    - The ``preStop`` sleep gives the load balancer time to notice the pod is
      terminating and stop routing NEW requests to it. Without it, Kubernetes sends
      SIGTERM and removes the endpoint concurrently, so requests can still arrive
      at a pod that has already begun shutting down.
    - The readiness probe is what takes the pod out of rotation; the liveness probe
      must be slack enough that a busy worker is not restarted mid-run.

    The pod is locked down by default: non-root, read-only root filesystem, no
    capabilities, no service account token, and secrets come from a Secret object
    rather than the manifest or the image.
    """
    secret_name = f"{service_name}-secrets"
    return "\n".join(
        [
            "# Generated by 10xgraph-api CLI. Before applying:",
            "#   1. Push the image to your registry and set `image` below to a pinned",
            "#      version tag or digest (never :latest).",
            "#   2. Create the Secret the pod reads (API keys, JWT_SECRET_KEY, DB URLs),",
            "#      one --from-literal per value (--from-env-file keeps quotes as part",
            "#      of the value), or sync it from your secret manager:",
            f"#        kubectl create secret generic {secret_name} \\",
            "#          --from-literal=JWT_SECRET_KEY=... --from-literal=GOOGLE_API_KEY=... \\",
            "#          --from-literal=ORIGINS=https://your-frontend.example.com \\",
            "#          --from-literal=ALLOWED_HOST=your-app.example.com",
            "#   3. ORIGINS (CORS origins, never *) and ALLOWED_HOST (your public hostname) are",
            "#      required in production and are read from the same Secret.",
            "#   4. Expose it through an Ingress or Gateway that terminates TLS.",
            "# The root filesystem is read-only: only /tmp and /app/uploads are writable.",
            "# If your graph writes anywhere else (a SQLite checkpointer under",
            "# ~/.10xgraph, a local Qdrant folder), mount a volume at that path.",
            "apiVersion: apps/v1",
            "kind: Deployment",
            "metadata:",
            f"  name: {service_name}",
            "spec:",
            "  replicas: 2",
            "  selector:",
            "    matchLabels:",
            f"      app: {service_name}",
            "  template:",
            "    metadata:",
            "      labels:",
            f"        app: {service_name}",
            "    spec:",
            # Must be > the app's graceful timeout, else the kubelet SIGKILLs a
            # pod that is still finishing a run.
            f"      terminationGracePeriodSeconds: {K8S_TERMINATION_GRACE_SECONDS}",
            "      # The app never calls the Kubernetes API; do not hand it a token.",
            "      automountServiceAccountToken: false",
            "      securityContext:",
            "        runAsNonRoot: true",
            f"        runAsUser: {APP_UID}",
            f"        runAsGroup: {APP_UID}",
            f"        fsGroup: {APP_UID}",
            "        seccompProfile:",
            "          type: RuntimeDefault",
            "      containers:",
            f"        - name: {service_name}",
            f"          image: registry.example.com/{service_name}:1.0.0",
            "          imagePullPolicy: IfNotPresent",
            "          ports:",
            f"            - containerPort: {port}",
            "          securityContext:",
            "            allowPrivilegeEscalation: false",
            "            readOnlyRootFilesystem: true",
            "            capabilities:",
            "              drop:",
            '                - "ALL"',
            "          volumeMounts:",
            "            # Gunicorn worker heartbeats and Python temp files",
            "            - name: tmp",
            "              mountPath: /tmp",
            "            # Local media uploads (MEDIA_STORAGE_TYPE=local, the default)",
            "            - name: uploads",
            "              mountPath: /app/uploads",
            "          envFrom:",
            "            - secretRef:",
            f"                name: {secret_name}",
            "          env:",
            "            - name: MODE",
            '              value: "production"',
            "            - name: IS_DEBUG",
            '              value: "false"',
            # ORIGINS and ALLOWED_HOST come from the Secret (an env entry here would
            # override it). Production refuses to start while either is missing or a wildcard.
            "          lifecycle:",
            "            preStop:",
            "              exec:",
            "                # Let the load balancer stop sending new traffic before",
            "                # the app starts shutting down.",
            "                command:",
            '                  - "/bin/sh"',
            '                  - "-c"',
            f'                  - "sleep {K8S_PRESTOP_SLEEP_SECONDS}"',
            "          readinessProbe:",
            "            httpGet:",
            "              path: /health/ready",
            f"              port: {port}",
            "            initialDelaySeconds: 5",
            "            periodSeconds: 10",
            "          livenessProbe:",
            "            httpGet:",
            "              path: /ping",
            f"              port: {port}",
            "            # Deliberately slack: a worker busy with a long agent run",
            "            # must not be mistaken for a hung one and restarted.",
            "            initialDelaySeconds: 30",
            "            periodSeconds: 30",
            "            failureThreshold: 5",
            "          resources:",
            "            requests:",
            '              cpu: "500m"',
            '              memory: "512Mi"',
            "            limits:",
            '              cpu: "2"',
            '              memory: "2Gi"',
            "      volumes:",
            "        - name: tmp",
            "          emptyDir: {}",
            "        # Per pod and lost on restart. With more than one replica a file",
            "        # uploaded to one pod is missing on the others: set",
            "        # MEDIA_STORAGE_TYPE=cloud (S3/GCS) for production uploads.",
            "        - name: uploads",
            "          emptyDir: {}",
            "---",
            "apiVersion: v1",
            "kind: Service",
            "metadata:",
            f"  name: {service_name}",
            "spec:",
            "  selector:",
            f"    app: {service_name}",
            "  ports:",
            "    - protocol: TCP",
            "      port: 80",
            f"      targetPort: {port}",
            "",
        ]
    )


def generate_docker_compose_content(service_name: str, port: int) -> str:
    """Generate a docker-compose.yml for the API service.

    Published on 127.0.0.1 only, so the plain-HTTP port is reachable from this host and
    not from the network: a TLS reverse proxy on the same host is what faces the internet.
    Secrets come from ``.env`` at run time and never enter the image.
    """
    return "\n".join(
        [
            "# Generated by 10xgraph-api CLI.",
            "# Secrets (API keys, JWT_SECRET_KEY, DB URLs) are read from .env at run time;",
            "# .dockerignore keeps .env out of the image. Copy .env.example to .env first.",
            "services:",
            f"  {service_name}:",
            "    build: .",
            "    image: 10xgraph-api:latest",
            "    env_file:",
            "      - .env",
            "    environment:",
            "      - PYTHONUNBUFFERED=1",
            "      - PYTHONDONTWRITEBYTECODE=1",
            # Default to production; local in-memory telemetry stays disabled.
            "      - MODE=production",
            "      - IS_DEBUG=false",
            "      # ORIGINS and ALLOWED_HOST come from .env (a value here would override it).",
            "      # Production refuses to start while either is missing or a wildcard.",
            "    # Loopback only: put a TLS reverse proxy (Caddy, nginx, a cloud load",
            "    # balancer) in front. Drop the 127.0.0.1 prefix only if that proxy runs",
            "    # on another host, and firewall the port to it.",
            "    ports:",
            f"      - '127.0.0.1:{port}:{port}'",
            (
                f"    command: [ 'gunicorn', '-k', '{GUNICORN_WORKER_CLASS}', "
                f"'-b', '0.0.0.0:{port}', "
                f"'--graceful-timeout', '{GRACEFUL_TIMEOUT_SECONDS}', "
                f"'--timeout', '{WORKER_TIMEOUT_SECONDS}', "
                "'tenxgraph_api.src.app.main:app' ]"
            ),
            # Give in-flight agent runs time to drain on `docker compose down`
            # instead of being killed after the default 10s.
            # Longer than gunicorn's --graceful-timeout, or SIGKILL lands mid-drain.
            f"    stop_grace_period: {COMPOSE_STOP_GRACE_SECONDS}s",
            "    restart: unless-stopped",
            "    # Locked down: read-only filesystem, no Linux capabilities, no setuid",
            "    # escalation. Only /tmp and the uploads folder are writable.",
            "    read_only: true",
            "    tmpfs:",
            "      - /tmp",
            "    cap_drop:",
            "      - ALL",
            "    security_opt:",
            "      - no-new-privileges:true",
            "    volumes:",
            "      - uploads:/app/uploads",
            "    # Consider adding resource limits and deploy configurations in a swarm/stack",
            "    # deploy:",
            "    #   replicas: 2",
            "    #   resources:",
            "    #     limits:",
            "    #       cpus: '1.0'",
            "    #       memory: 512M",
            "",
            "volumes:",
            "  uploads:",
            "",
        ]
    )


# Files that hold credentials. The Dockerfile runs ``COPY . .``, so anything not
# ignored here ships inside the image; the same list keeps them out of git.
SECRET_FILE_PATTERNS: Final[tuple[str, ...]] = (
    ".env",
    ".env.*",
    "*.env",
    "secrets/",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "id_rsa*",
    "id_ed25519*",
    "service_account*.json",
    "service-account*.json",
    "credentials*.json",
    ".aws/",
    ".ssh/",
    ".netrc",
    ".pypirc",
    ".npmrc",
)

# The scaffolded .env.example is documentation, not a secret, and belongs in git.
GITIGNORE_SECRET_PATTERNS: Final[tuple[str, ...]] = (*SECRET_FILE_PATTERNS, "!.env.example")


def merge_ignore_patterns(existing: str, patterns: tuple[str, ...]) -> str | None:
    """Return ``existing`` with the patterns it lacks appended, or None if none are missing.

    Lets ``build`` and ``init`` add the secret patterns to an ignore file the developer
    already has, instead of skipping it and leaving the gaps. Negations (``!.env.example``)
    are re-appended after anything added, since a later ``.env.*`` would otherwise
    override an earlier exception.
    """
    present = {line.strip() for line in existing.splitlines()}
    missing = [pattern for pattern in patterns if pattern not in present]
    if not missing:
        return None
    negations = [p for p in patterns if p.startswith("!") and p not in missing]
    missing += negations
    prefix = existing if not existing or existing.endswith("\n") else existing + "\n"
    header = "\n# Added by 10xgraph: files that hold credentials\n"
    return prefix + header + "\n".join(missing) + "\n"


def generate_dockerignore_content() -> str:
    """Generate a standard .dockerignore file content."""
    return "\n".join(
        [
            "# Exclude secrets and credentials (COPY . . would ship them in the image)",
            *SECRET_FILE_PATTERNS,
            "configs/",
            "",
            "# Exclude Python artifacts and caches",
            "__pycache__/",
            "*.py[cod]",
            "*$py.class",
            ".pytest_cache/",
            ".coverage",
            "htmlcov/",
            ".mypy_cache/",
            ".ruff_cache/",
            "",
            "# Exclude virtual environments",
            ".venv/",
            "venv/",
            "env/",
            "",
            "# Exclude deployment files the running app never reads",
            "docker-compose*.yml",
            "compose*.yaml",
            "k8s.yaml",
            "",
            "# Exclude version control and IDE files",
            ".git/",
            ".gitignore",
            ".idea/",
            ".vscode/",
            "*.swp",
            "*.swo",
            "",
            "# Exclude built assets and distribution files",
            "build/",
            "dist/",
            "*.egg-info/",
            "",
            "# Exclude local data directories",
            "data/",
            "db.sqlite3",
            "store/",
            "uploads/",
            "eval_reports/",
            "",
            "# Exclude dev-only project folders: tests, evals, and installed agent skills",
            "tests/",
            "evals/",
            ".claude/",
            ".agents/",
            ".github/",
            "",
        ]
    )


def generate_gitignore_content() -> str:
    """Generate the .gitignore that ``10xgraph init`` scaffolds.

    ``init`` tells developers to copy ``.env.example`` to ``.env`` and fill in API keys and
    ``JWT_SECRET_KEY``; this keeps that file, and other credentials, out of commits.
    """
    return "\n".join(
        [
            "# Secrets and credentials: never commit these",
            *GITIGNORE_SECRET_PATTERNS,
            "",
            "# Local data written by the dev server and 10xgraph eval",
            "uploads/",
            "eval_reports/",
            "*.sqlite3",
            "*.db",
            "",
            "# Python",
            "__pycache__/",
            "*.py[cod]",
            ".venv/",
            "venv/",
            "*.egg-info/",
            "build/",
            "dist/",
            "",
            "# Tool caches",
            ".pytest_cache/",
            ".mypy_cache/",
            ".ruff_cache/",
            ".coverage",
            "htmlcov/",
            "",
            "# Editors and OS",
            ".idea/",
            ".vscode/",
            ".DS_Store",
            "",
        ]
    )
