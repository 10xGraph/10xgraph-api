from tenxgraph_api.src.app.core.exceptions.server_errors import classify


def _driver_error(name: str, module: str, **attrs):
    cls = type(name, (Exception,), {"__module__": module})
    err = cls("boom")
    for k, v in attrs.items():
        setattr(err, k, v)
    return err


def test_data_errors_are_422():
    for module in ("psycopg.errors", "asyncpg.exceptions"):
        kind = classify(_driver_error("DataError", module))
        assert kind.status_code == 422 and kind.error_code == "INVALID_REQUEST_DATA"
    kind = classify(_driver_error("InternalError_", "asyncpg.exceptions", sqlstate="22003"))
    assert kind.status_code == 422


def test_outage_stays_503():
    assert classify(_driver_error("OperationalError", "psycopg.errors")).status_code == 503
