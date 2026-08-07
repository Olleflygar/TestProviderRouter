"""Gating and provisioning helpers for the real-PostgreSQL tests.

Every PostgreSQL test skips -- never fails -- unless a test database is
configured, so `pytest` stays green and offline on a machine with no server,
mirroring how the DuckDB tests skip when that package is absent.

The connection string comes from the environment, falling back to the
repository's root `.env` through python-dotenv exactly as `WorkflowTests`
already does. Nothing here reaches a provider or the network beyond the
configured database.
"""

from __future__ import annotations

import importlib.util
import os
import time
from pathlib import Path

TEST_URL_ENV = "NYGEN_ROUTER_TEST_POSTGRES_URL"
TEST_TRANSACTION_URL_ENV = "NYGEN_ROUTER_TEST_POSTGRES_TRANSACTION_URL"

_PROJECT_ROOT = Path(__file__).resolve().parents[2]

PSYCOPG_AVAILABLE = importlib.util.find_spec("psycopg") is not None


def _from_dotenv(name: str) -> str | None:
    try:
        from dotenv import dotenv_values
    except ImportError:  # pragma: no cover - dotenv is a dev dependency
        return None
    env_file = _PROJECT_ROOT / ".env"
    if not env_file.is_file():
        return None
    try:
        value = dotenv_values(env_file).get(name)
    except OSError:  # pragma: no cover - unreadable .env is simply "unset"
        return None
    return value or None


def postgres_url(name: str = TEST_URL_ENV) -> str | None:
    """Return the configured test connection string, or None when unset."""
    return os.environ.get(name) or _from_dotenv(name)


def postgres_available(name: str = TEST_URL_ENV) -> bool:
    return PSYCOPG_AVAILABLE and postgres_url(name) is not None


def skip_reason(name: str = TEST_URL_ENV) -> str:
    if not PSYCOPG_AVAILABLE:
        return "psycopg is not installed"
    return f"{name} is not configured"


# The completeness-first preset the documentation recommends for a distant
# database. The shipped defaults are latency-first and assume a nearby server;
# over the internet a 2-second checkout bound makes unrelated assertions flake
# on transient contention. Tests about the defaults construct their own store.
REMOTE_TEST_CONFIG = {
    "connect_timeout_seconds": 10.0,
    "statement_timeout_seconds": 5.0,
    "checkout_timeout_seconds": 5.0,
}


def config_for_url(url: str) -> dict[str, object]:
    """Test settings for whichever database is configured.

    A CI service container speaks plaintext on localhost, so an unencrypted URL
    there is deliberate and must be confirmed rather than silently permitted.
    """
    config = dict(REMOTE_TEST_CONFIG)
    if "sslmode=disable" in url:
        config["allow_unencrypted"] = True
    return config


_shared_store: object | None = None


def shared_store() -> object:
    """One process-wide store for every PostgreSQL test.

    A store per test would build a pool per test, and a managed pooler has a
    bounded client-connection budget: the churn both exhausts it and dominates
    the runtime. Tests get a clean table from `clear_events` instead.
    """
    global _shared_store
    if _shared_store is None:
        from nygen_router import PostgresMetricsStore

        url = postgres_url()
        assert url is not None
        # Closed by the session fixture in conftest, not atexit: closing during
        # interpreter shutdown races with the pool's worker threads.
        _shared_store = PostgresMetricsStore(url, config=config_for_url(url))
    return _shared_store


def forget_shared_store() -> None:
    """Drop the cached store after a test deliberately changes the schema."""
    global _shared_store, _schema_ready
    if _shared_store is not None:
        _shared_store.close()  # type: ignore[attr-defined]
        _shared_store = None
    _schema_ready = False


_schema_ready = False


def ensure_schema(url: str) -> None:
    """Provision through the supported route only when the schema is absent.

    Inspecting opens its own short-lived pool, so the result is cached: doing
    it per test dominated the runtime against a managed database.
    """
    global _schema_ready
    if _schema_ready:
        return
    from nygen_router.storage.admin import create_postgres_database, inspect_postgres_database
    from nygen_router.storage.schema import SchemaState

    if inspect_postgres_database(url).schema.state is SchemaState.MISSING:
        create_postgres_database(url)
    _schema_ready = True


def clear_events(url: str) -> None:
    """Remove recorded attempts between tests without touching the schema."""
    store = shared_store()
    with store._connection(validate=False) as connection:  # type: ignore[attr-defined]
        connection.execute("DELETE FROM provider_attempts")


def restore_schema(url: str) -> None:
    """Return the database to a clean current schema, doing the least work.

    Recreating unconditionally would build a fresh pool per test, and a managed
    pooler has a bounded client-connection budget that such churn exhausts.

    Teardown is bookkeeping, not an assertion, so one transient connection
    failure against a managed database over the internet is retried rather than
    reported as a test error. Nothing under test is retried: the store's own
    failure behavior is asserted deterministically elsewhere.
    """
    from nygen_router.storage.admin import inspect_postgres_database
    from nygen_router.storage.schema import SchemaState

    for attempt in (1, 2):
        try:
            state = inspect_postgres_database(url).schema.state
            if state is SchemaState.CURRENT:
                clear_events(url)
            else:
                reset_schema(url)
            return
        except Exception:
            forget_shared_store()
            if attempt == 2:
                raise
            time.sleep(2.0)


def reset_schema(url: str) -> None:
    """Drop and re-provision the metrics schema through the supported route.

    Teardown uses raw DDL because no production code may ever drop a remote
    table; provisioning deliberately goes through the public administration
    surface, so every test starts from a schema created the way an operator
    creates one.
    """
    import psycopg

    from nygen_router.storage.admin import create_postgres_database
    from nygen_router.storage.schema import SCHEMA_VERSIONS_TABLE

    with psycopg.connect(url, connect_timeout=15) as connection:
        connection.execute("DROP TABLE IF EXISTS provider_attempts")
        connection.execute(f"DROP TABLE IF EXISTS {SCHEMA_VERSIONS_TABLE}")
        connection.commit()
    create_postgres_database(url)
    forget_shared_store()


def drop_schema(url: str) -> None:
    """Remove the metrics schema entirely, leaving an un-provisioned database."""
    import psycopg

    from nygen_router.storage.schema import SCHEMA_VERSIONS_TABLE

    with psycopg.connect(url, connect_timeout=15) as connection:
        connection.execute("DROP TABLE IF EXISTS provider_attempts")
        connection.execute(f"DROP TABLE IF EXISTS {SCHEMA_VERSIONS_TABLE}")
        connection.commit()
