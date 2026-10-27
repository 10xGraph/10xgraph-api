import logging

from fastapi import Depends

from tenxgraph_api.src.app.core import Settings, get_settings, logger


# Sentry's default logging integration turns every ERROR log into an event. Handled client
# errors (4xx) are logged at WARNING, so only server faults reach this level. Warnings and
# above stay as breadcrumbs.
SENTRY_BREADCRUMB_LEVEL = logging.INFO
SENTRY_EVENT_LEVEL = logging.ERROR

# Only server faults are Sentry events. A 403 is authorization doing its job; reporting
# every one lets any client flood the project and bury real errors.
SENTRY_FAILED_STATUS_CODES = frozenset(range(500, 600))


def init_sentry(settings: Settings = Depends(get_settings)) -> None:
    """Initialize Sentry for error tracking and performance monitoring.

    The initialization is best-effort: if ``sentry_sdk`` isn't installed or any
    unexpected error occurs, the application continues to run and a warning is
    logged instead of failing hard.
    """
    environment = settings.MODE.upper() if settings.MODE else ""

    if not settings.SENTRY_DSN:
        logger.warning(
            "Sentry is not configured. Sentry DSN is not set or running in local environment."
        )
        return

    allowed_environments = ["PRODUCTION", "STAGING", "DEVELOPMENT"]
    if environment not in allowed_environments:
        logger.warning(
            f"Sentry is not configured for this environment: {environment}. "
            f"Allowed environments are: {allowed_environments}"
        )
        return

    logger.info(f"Sentry is configured for environment: {environment}")

    try:
        import sentry_sdk
        from sentry_sdk.integrations.fastapi import FastApiIntegration
        from sentry_sdk.integrations.logging import LoggingIntegration
        from sentry_sdk.integrations.starlette import StarletteIntegration

        sentry_sdk.init(
            dsn=settings.SENTRY_DSN,
            integrations=[
                LoggingIntegration(level=SENTRY_BREADCRUMB_LEVEL, event_level=SENTRY_EVENT_LEVEL),
                FastApiIntegration(
                    transaction_style="endpoint",
                    failed_request_status_codes=SENTRY_FAILED_STATUS_CODES,
                ),
                StarletteIntegration(
                    transaction_style="endpoint",
                    failed_request_status_codes=SENTRY_FAILED_STATUS_CODES,
                ),
            ],
            traces_sample_rate=settings.SENTRY_TRACES_SAMPLE_RATE,
            profiles_sample_rate=settings.SENTRY_PROFILES_SAMPLE_RATE,
            # Request bodies hold prompts and user data, and local variables hold the same.
            max_request_body_size="never",
            include_local_variables=False,
            send_default_pii=False,
        )
        logger.debug("Sentry initialized")
    except ImportError:
        logger.warning("sentry_sdk not installed; install '10xgraph-api[sentry]' to enable Sentry")
    except Exception as exc:  # intentionally broad: init must not crash app
        logger.warning("Error initializing Sentry: %s", exc)
