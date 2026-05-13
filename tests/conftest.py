import os

import psycopg2
import pytest


@pytest.fixture(scope="session")
def postgres_settings():
    """PostgreSQL connection settings from environment or defaults."""
    return {
        "host": os.environ.get("E2E_PG_HOST", "127.0.0.1"),
        "port": int(os.environ.get("E2E_PG_PORT", "5432")),
        "user": os.environ.get("E2E_PG_USER", "postgres"),
        "password": os.environ.get("E2E_PG_PASSWORD", "postgres"),
        "dbname": os.environ.get("E2E_PG_DB", "postgres"),
    }


@pytest.fixture(scope="session", autouse=True)
def ensure_postgres_available(postgres_settings):
    """Ensure PostgreSQL backend is available before running any tests."""
    try:
        with psycopg2.connect(
            connect_timeout=3, sslmode="disable", **postgres_settings
        ) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                assert cur.fetchone() == (1,)
    except Exception as err:  # pragma: no cover - environment dependent
        pytest.fail(
            f"PostgreSQL backend is required for tests but is not reachable: {err}"
        )
