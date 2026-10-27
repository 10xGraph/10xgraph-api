import logging
import os
from functools import lru_cache

from pydantic import ConfigDict, Field, field_validator, model_validator
from pydantic_settings import BaseSettings


IS_PRODUCTION = False
LOGGER_NAME = os.getenv("LOGGER_NAME", "10xgraph-api")

logger = logging.getLogger(LOGGER_NAME)

# Accepted spellings of MODE (after strip and lowercase) mapped to the canonical value that
# the rest of the package compares against.
MODE_ALIASES = {
    "production": "production",
    "prod": "production",
    "development": "development",
    "dev": "development",
    "local": "development",
}


def normalize_mode_value(value: str | None) -> str:
    """Return the canonical MODE for ``value``; unset or blank means ``development``.

    Raises:
        ValueError: If the value is not one of the accepted spellings.
    """
    text = (value or "").strip().lower()
    if not text:
        return "development"
    try:
        return MODE_ALIASES[text]
    except KeyError:
        accepted = ", ".join(sorted(MODE_ALIASES))
        raise ValueError(
            f"Invalid MODE {value!r}. Accepted values (case-insensitive): {accepted}. "
            "Refusing to start: an unknown MODE would silently run with the development "
            "security posture."
        ) from None


def _csv_tokens(value: str) -> list[str]:
    """Split a comma-separated setting into stripped, non-empty tokens."""
    return [token.strip() for token in value.split(",") if token.strip()]


class Settings(BaseSettings):
    """
    This class defines the configuration settings for the application.

    Attributes:
        APP_NAME (str): The name of the application.
        APP_VERSION (str): The version of the application.
        MODE (str): The mode in which the application is running (e.g., development, production).
        LOG_LEVEL (int): The logging level for the application.
        SUMMARY (str): A brief summary of the application. Default is "Backend Base".

        ORIGINS (str): CORS allowed origins.
        ALLOWED_HOST (str): CORS allowed hosts.
        REDIS_URL (str): The URL for the Redis server.

        SENTRY_DSN (str): The DSN for Sentry error tracking.

    Config:
        extra (str): Configuration for handling extra fields. Default is "allow".
    """

    APP_NAME: str = "MyApp"
    APP_VERSION: str = "0.1.0"
    MODE: str = "development"
    # CRITICAL = 50
    # FATAL = CRITICAL
    # ERROR = 40
    # WARNING = 30
    # WARN = WARNING
    # INFO = 20
    # DEBUG = 10
    # NOTSET = 0
    LOG_LEVEL: str = "INFO"
    # Forced off when MODE=production, whatever the environment says.
    IS_DEBUG: bool = True
    # When true, MODE=production refuses to start on the settings it otherwise only warns
    # about: wildcard ALLOWED_HOST, wildcard ORIGINS, and enabled API docs. Unset means on in
    # production and off in development; STRICT_PRODUCTION_CHECKS=false opts out.
    STRICT_PRODUCTION_CHECKS: bool | None = None

    #################################
    ###### Request Limits ###########
    #################################
    MAX_REQUEST_SIZE: int = 10 * 1024 * 1024  # 10MB default

    #################################
    ###### Security Headers #########
    #################################
    SECURITY_HEADERS_ENABLED: bool = True
    HSTS_ENABLED: bool = True
    HSTS_MAX_AGE: int = 31536000  # 1 year in seconds
    HSTS_INCLUDE_SUBDOMAINS: bool = True
    HSTS_PRELOAD: bool = False
    FRAME_OPTIONS: str = "DENY"  # DENY, SAMEORIGIN, or ALLOW-FROM
    CONTENT_TYPE_OPTIONS: str = "nosniff"
    XSS_PROTECTION: str = "1; mode=block"
    REFERRER_POLICY: str = "strict-origin-when-cross-origin"
    PERMISSIONS_POLICY: str | None = None  # Uses default if None
    CSP_POLICY: str | None = None  # Uses default if None

    SUMMARY: str = "10xGraph Backend"

    #################################
    ###### CORS Config ##############
    #################################
    ORIGINS: str = "*"
    ALLOWED_HOST: str = "*"
    # Whether cross-origin requests may carry credentials (cookies / auth headers).
    #
    # Combined with ORIGINS="*" this is the dangerous case: Starlette reflects the
    # request's Origin back with Access-Control-Allow-Credentials: true, which
    # effectively makes EVERY origin a trusted, credentialed origin. In production
    # that combination is refused at startup (see setup_middleware); set explicit
    # ORIGINS, or set CORS_ALLOW_CREDENTIALS=false to serve a public,
    # non-credentialed API from any origin.
    CORS_ALLOW_CREDENTIALS: bool = True

    #################################
    ###### Paths ####################
    #################################
    ROOT_PATH: str = "/"
    DOCS_PATH: str = "/docs"
    REDOCS_PATH: str = "/redocs"

    #################################
    ###### REDIS Config ##########
    #################################
    REDIS_URL: str | None = None

    #################################
    ###### sentry Config ############
    #################################
    SENTRY_DSN: str | None = None
    # Share of requests traced / profiled. 1.0 sends every request, which is costly and
    # rarely wanted in production.
    SENTRY_TRACES_SAMPLE_RATE: float = Field(default=0.1, ge=0.0, le=1.0)
    SENTRY_PROFILES_SAMPLE_RATE: float = Field(default=0.0, ge=0.0, le=1.0)

    #################################
    ###### Auth ############
    #################################
    SNOWFLAKE_EPOCH: int = 1609459200000
    SNOWFLAKE_NODE_ID: int = 1
    SNOWFLAKE_WORKER_ID: int = 2
    SNOWFLAKE_TIME_BITS: int = 39
    SNOWFLAKE_NODE_BITS: int = 5
    SNOWFLAKE_WORKER_BITS: int = 8

    #################################
    ###### JWT Config ###############
    #################################
    JWT_SECRET_KEY: str | None = None
    JWT_ALGORITHM: str = "HS256"
    # When set, tokens must carry a matching ``iss`` / ``aud`` claim, so a token minted for
    # another service that shares the signing key is not accepted here.
    JWT_ISSUER: str | None = None
    JWT_AUDIENCE: str | None = None

    #################################
    ###### OTEL Config ##############
    #################################
    OTEL_ENABLED: bool = False
    OTEL_SERVICE_NAME: str = "10xgraph-api"
    OTEL_EXPORTER_OTLP_ENDPOINT: str | None = None
    OTEL_LEVEL: str = "standard"  # spans | standard | full

    @field_validator("MODE", mode="before")
    @classmethod
    def normalize_mode(cls, v: str | None) -> str:
        """Normalize MODE to ``production`` or ``development``; reject anything else."""
        return normalize_mode_value(v)

    @field_validator("ORIGINS")
    @classmethod
    def warn_cors_wildcard(cls, v: str) -> str:
        """Warn if CORS is set to wildcard in production."""
        try:
            mode = normalize_mode_value(os.environ.get("MODE"))
        except ValueError:
            # The MODE field itself reports the invalid value.
            return v
        if v == "*" and mode == "production":
            logger.warning(
                "⚠️  SECURITY WARNING: CORS ORIGINS='*' in production.\n"
                "   This allows any website to make requests to your API.\n"
                "   Set ORIGINS to specific domains (e.g., https://yourdomain.com)"
            )
        return v

    @model_validator(mode="after")
    def check_production_security(self):
        """Check for insecure configurations in production mode."""
        if self.STRICT_PRODUCTION_CHECKS is None:
            self.STRICT_PRODUCTION_CHECKS = self.MODE == "production"
        if self.MODE == "production":
            # Docs (and the OpenAPI schema behind them) are off in production unless a path
            # was set explicitly.
            for field in ("DOCS_PATH", "REDOCS_PATH"):
                if field not in self.model_fields_set:
                    setattr(self, field, "")

            # Debug output is never allowed in production: enforce it rather than warn.
            if self.IS_DEBUG:
                self.IS_DEBUG = False
                logger.warning("IS_DEBUG=true is ignored in production; debug is forced off.")

            warnings = []

            if self.DOCS_PATH or self.REDOCS_PATH:
                warnings.append(
                    "⚠️  API documentation endpoints are enabled in production. "
                    "Consider disabling DOCS_PATH and REDOCS_PATH."
                )

            if "*" in _csv_tokens(self.ALLOWED_HOST):
                warnings.append(
                    "⚠️  ALLOWED_HOST='*' in production. "
                    "Set to specific hostnames for better security."
                )

            if self.STRICT_PRODUCTION_CHECKS and "*" in _csv_tokens(self.ORIGINS):
                warnings.append("⚠️  ORIGINS='*' in production. Set the exact allowed origins.")

            if warnings and self.STRICT_PRODUCTION_CHECKS:
                raise ValueError(
                    "\n".join(
                        [
                            "Refusing to start: MODE=production with insecure settings "
                            "(STRICT_PRODUCTION_CHECKS is on by default in production):",
                            *warnings,
                            "Fix these settings, or set STRICT_PRODUCTION_CHECKS=false to "
                            "start anyway with only warnings.",
                        ]
                    )
                )
            if warnings:
                logger.warning("\n".join(["\n🔒 PRODUCTION SECURITY WARNINGS:", *warnings]))

        return self

    model_config = ConfigDict(extra="allow")


@lru_cache
def get_settings() -> Settings:
    """
    Retrieve and return the application settings.
    If not in production, load settings from a specific environment file.
    Returns:
        Settings: An instance of the Settings class containing
        application configurations.
    """
    logger.info("Loading settings from environment variables and .env if present")
    return Settings()  # type: ignore
