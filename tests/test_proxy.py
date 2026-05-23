from collections.abc import Generator
import contextlib
import os
import shutil
import socket
import ssl
import subprocess
import tempfile
import threading
import time

import psycopg
import psycopg2
import pytest

from postgresql_proxy import config_schema as cfg
from postgresql_proxy.proxy import Proxy


class _QuerySpy:
    """Minimal query interceptor that records every query it sees."""

    def __init__(self):
        self.captured: list[str] = []

    def capture(self, query: str, context) -> str:
        self.captured.append(query)
        return query


def _get_free_tcp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_for_listen_port(host: str, port: int, timeout: float = 5.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.2)
            if sock.connect_ex((host, port)) == 0:
                return
        time.sleep(0.05)
    raise TimeoutError(f"Proxy did not start listening on {host}:{port} in {timeout}s")


def _build_dump_like_sql(table_count: int = 12, rows_per_table: int = 100) -> str:
    chunks = ["BEGIN;"]
    for table_idx in range(table_count):
        table_name = f"e2e_batch_{table_idx}"
        chunks.append(f"DROP TABLE IF EXISTS {table_name};")
        chunks.append(f"CREATE TABLE {table_name} (id INTEGER, payload TEXT);")
        chunks.append(f"COPY {table_name} (id, payload) FROM STDIN;")
        for row_idx in range(rows_per_table):
            chunks.append(f"{row_idx}\trow_{table_idx}_{row_idx}")
        chunks.append("\\.")
        chunks.append(f"SELECT COUNT(*) FROM {table_name};")

    chunks.append("SELECT 'BATCH_OK';")
    chunks.append("COMMIT;")
    return "\n".join(chunks) + "\n"


def _run_psql_file(
    postgres_settings, port: int, sql_file_path: str, timeout_sec: int = 60
):
    cmd = [
        "psql",
        "-X",
        "-q",
        "-tA",
        "-v",
        "ON_ERROR_STOP=1",
        "-h",
        "127.0.0.1",
        "-p",
        str(port),
        "-U",
        postgres_settings["user"],
        "-d",
        postgres_settings["dbname"],
        "-f",
        sql_file_path,
    ]
    env = {
        **os.environ,
        "PGPASSWORD": postgres_settings["password"],
        "PGSSLMODE": "require",
    }
    return subprocess.run(
        cmd,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout_sec,
        check=False,
    )


@contextlib.contextmanager
def _temporary_server_cert_pair():
    if shutil.which("openssl") is None:
        pytest.fail("openssl is required for SSL E2E tests but was not found in PATH")

    with tempfile.TemporaryDirectory(prefix="proxy-e2e-cert-") as tmp_dir:
        cert_path = os.path.join(tmp_dir, "server.crt")
        key_path = os.path.join(tmp_dir, "server.key")
        result = subprocess.run(
            [
                "openssl",
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-sha256",
                "-days",
                "1",
                "-nodes",
                "-subj",
                "/CN=localhost",
                "-keyout",
                key_path,
                "-out",
                cert_path,
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            err_tail = "\n".join((result.stderr or "").splitlines()[-20:])
            pytest.fail(
                f"Failed to generate temporary TLS cert/key for E2E tests (rc={result.returncode}): {err_tail}"
            )

        yield cert_path, key_path


@contextlib.contextmanager
def _run_proxy(
    postgres_settings,
    ssl_context: ssl.SSLContext | None = None,
    *,
    plugins: dict | None = None,
    query_interceptors: list[dict] | None = None,
) -> Generator[int, None, None]:
    """Start a proxy in a background thread and yield its listening port."""
    proxy_port = _get_free_tcp_port()
    commands_config: dict = {}
    if query_interceptors:
        commands_config["queries"] = query_interceptors
    instance = cfg.InstanceSettings(
        {
            "listen": {"name": "proxy", "host": "127.0.0.1", "port": proxy_port},
            "redirect": {
                "name": "postgres",
                "host": postgres_settings["host"],
                "port": postgres_settings["port"],
            },
            "intercept": {"commands": commands_config, "responses": {}},
        }
    )
    if not hasattr(instance.intercept.responses, "parameter_status"):
        instance.intercept.responses.parameter_status = []

    proxy = Proxy(instance, plugins=plugins or {}, debug=True, ssl_context=ssl_context)
    thread = threading.Thread(
        target=proxy.listen, kwargs={"max_connections": 32}, daemon=True
    )
    thread.start()

    _wait_for_listen_port("127.0.0.1", proxy_port)

    try:
        yield proxy_port
    finally:
        proxy.stop()
        # Wake selector.select(timeout=1) so shutdown is immediate.
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as wake_sock:
            wake_sock.settimeout(0.2)
            wake_sock.connect_ex(("127.0.0.1", proxy_port))
        thread.join(timeout=4)
        assert not thread.is_alive(), "Proxy thread did not stop cleanly"


@pytest.fixture()
def plain_proxy_port(postgres_settings):
    with _run_proxy(postgres_settings) as proxy_port:
        yield proxy_port


@pytest.fixture()
def ssl_proxy_port(postgres_settings):
    with _temporary_server_cert_pair() as (cert_path, key_path):
        ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ssl_context.load_cert_chain(certfile=cert_path, keyfile=key_path)
        with _run_proxy(postgres_settings, ssl_context=ssl_context) as proxy_port:
            yield proxy_port


@pytest.mark.timeout(20)
def test_connect_query_without_ssl(postgres_settings, plain_proxy_port):
    with psycopg2.connect(
        host="127.0.0.1",
        port=plain_proxy_port,
        user=postgres_settings["user"],
        password=postgres_settings["password"],
        dbname=postgres_settings["dbname"],
        sslmode="disable",
        connect_timeout=3,
    ) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
            assert cur.fetchone() == (1,)


@pytest.mark.timeout(20)
def test_connect_query_with_ssl(postgres_settings, ssl_proxy_port):
    with psycopg2.connect(
        host="127.0.0.1",
        port=ssl_proxy_port,
        user=postgres_settings["user"],
        password=postgres_settings["password"],
        dbname=postgres_settings["dbname"],
        sslmode="require",
        connect_timeout=3,
    ) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
            assert cur.fetchone() == (1,)


@pytest.mark.timeout(60)
def test_repeated_connect_query_smoke_no_hang(postgres_settings, plain_proxy_port):
    for i in range(20):
        with psycopg2.connect(
            host="127.0.0.1",
            port=plain_proxy_port,
            user=postgres_settings["user"],
            password=postgres_settings["password"],
            dbname=postgres_settings["dbname"],
            sslmode="disable",
            connect_timeout=3,
        ) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT %s", (i,))
                assert cur.fetchone() == (i,)


@pytest.mark.timeout(60)
@pytest.mark.parametrize("sslmode", ["disable", "require"])
@pytest.mark.parametrize(
    ["sql", "expected"],
    [
        pytest.param(
            "SELECT 1",
            [(1,)],
            id="tiny-1B",
        ),
        pytest.param(
            "SELECT repeat('x', 1024)",
            [("x" * 1024,)],
            id="small-1KB",
        ),
        pytest.param(
            "SELECT repeat('x', 102400)",
            [("x" * 102400,)],
            id="medium-100KB",
        ),
        pytest.param(
            "SELECT repeat('x', 1048576)",
            [("x" * 1048576,)],
            id="large-1MB",
        ),
        pytest.param(
            "SELECT repeat('x', 10485760)",
            [("x" * 10485760,)],
            id="xlarge-10MB",
        ),
        pytest.param(
            "SELECT i FROM generate_series(1, 10000) AS t(i)",
            [(i,) for i in range(1, 10001)],
            id="rows-10k",
        ),
        pytest.param(
            "SELECT i FROM generate_series(1, 100000) AS t(i)",
            [(i,) for i in range(1, 100001)],
            id="rows-100k",
        ),
    ],
)
def test_various_payload_sizes(
    postgres_settings,
    plain_proxy_port,
    ssl_proxy_port,
    sslmode,
    sql,
    expected,
):
    with psycopg2.connect(
        host="127.0.0.1",
        port=plain_proxy_port if sslmode == "disable" else ssl_proxy_port,
        user=postgres_settings["user"],
        password=postgres_settings["password"],
        dbname=postgres_settings["dbname"],
        sslmode=sslmode,
        connect_timeout=3,
    ) as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
            assert cur.fetchall() == expected


@pytest.mark.timeout(60)
def test_psql_ssl_file_batch_stress_no_hang(postgres_settings, ssl_proxy_port):
    if shutil.which("psql") is None:
        pytest.fail("psql is required for this test but was not found in PATH")

    with tempfile.NamedTemporaryFile("w", suffix=".sql", delete=True) as tmp_file:
        sql_content = _build_dump_like_sql(table_count=24, rows_per_table=300)
        tmp_file.write(sql_content)
        tmp_file.flush()
        sql_file_path = tmp_file.name

        for run_idx in range(3):
            started = time.time()
            try:
                result = _run_psql_file(
                    postgres_settings,
                    port=ssl_proxy_port,
                    sql_file_path=sql_file_path,
                    timeout_sec=60,
                )
            except subprocess.TimeoutExpired as err:
                pytest.fail(
                    "psql -f batch timed out over SSL via proxy "
                    f"(run={run_idx + 1}, timeout={err.timeout}s)"
                )

            elapsed = time.time() - started
            if result.returncode != 0:
                out_tail = "\n".join((result.stdout or "").splitlines()[-20:])
                err_tail = "\n".join((result.stderr or "").splitlines()[-20:])
                pytest.fail(
                    "psql -f batch failed over SSL via proxy "
                    f"(run={run_idx + 1}, rc={result.returncode}, {elapsed=:.2f}s) "
                    f"stdout_tail={out_tail} stderr_tail={err_tail}"
                )

            if "BATCH_OK" not in (result.stdout or ""):
                out_tail = "\n".join((result.stdout or "").splitlines()[-20:])
                pytest.fail(
                    "psql -f batch succeeded but expected marker missing "
                    f"(run={run_idx + 1}, {elapsed=:.2f}s) stdout_tail={out_tail}"
                )


def test_extended_query_protocol_parse_packet_with_high_oid_params_passes_through_proxy(
    postgres_settings,
):
    """Regression: proxy must not corrupt Extended Query Protocol Parse packets.

    psycopg v3 sends Parse → Bind → Execute for parameterized queries.  The Parse body
    ends with binary uint32 OIDs; jsonb OID 3802 (0x00000EDA) contains 0xDA which is
    not valid UTF-8.  The old interceptor sliced the body incorrectly and crashed on
    decode, causing the connection to hang or drop.

    A _QuerySpy is wired into the proxy to verify the interceptor receives the correct
    SQL text — not corrupted bytes from the binary OID suffix.
    """
    spy = _QuerySpy()
    with _run_proxy(
        postgres_settings,
        plugins={"spy": spy},
        query_interceptors=[{"plugin": "spy", "function": "capture"}],
    ) as proxy_port:
        with psycopg.connect(
            host="127.0.0.1",
            port=proxy_port,
            user=postgres_settings["user"],
            password=postgres_settings["password"],
            dbname=postgres_settings["dbname"],
            sslmode="disable",
        ) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DROP TABLE IF EXISTS _test_jsonb_proxy_params;"
                    "CREATE TABLE _test_jsonb_proxy_params "
                    "(id serial PRIMARY KEY, data jsonb, label text);"
                )

                cur.execute(
                    "INSERT INTO _test_jsonb_proxy_params (data, label) "
                    "VALUES (%s, %s) RETURNING id",
                    (psycopg.types.json.Jsonb({"key": "value"}), "hello"),
                )
                row = cur.fetchone()

    assert row is not None and row[0] >= 1

    # Verify the interceptor received clean SQL — no binary OID bytes leaked in.
    insert_queries = [
        q for q in spy.captured if "INSERT INTO _test_jsonb_proxy_params" in q
    ]
    assert insert_queries
    assert all("\x00" not in q for q in insert_queries), (
        "null byte leaked into intercepted query"
    )


def test_extended_query_protocol_named_prepared_statement_passes_through_proxy(
    postgres_settings, plain_proxy_port
):
    """Parse packets with a non-empty statement name must also be relayed correctly.

    The statement_name field precedes the query text in the Parse body.  The fix uses
    find(b'\\x00') to locate boundaries, so named statements work the same as anonymous
    ones (empty name).
    """
    with psycopg.connect(
        host="127.0.0.1",
        port=plain_proxy_port,
        user=postgres_settings["user"],
        password=postgres_settings["password"],
        dbname=postgres_settings["dbname"],
        sslmode="disable",
        # Prepare after the first execution of the same query (i.e. on 2nd run).
        prepare_threshold=1,
    ) as conn:
        with conn.cursor() as cur:
            # Execute twice so psycopg can promote the query to a named statement.
            for val in (1, 2):
                cur.execute("SELECT %s::int + 1", (val,))
                result = cur.fetchone()
                assert result == (val + 1,)

            # Verify psycopg created a named prepared statement in this session.
            cur.execute(
                "SELECT count(*) FROM pg_prepared_statements WHERE name LIKE '_pg3_%'"
            )
            prepared_count = cur.fetchone()
            assert prepared_count is not None and prepared_count[0] >= 1


def test_non_utf8_client_encoding_query_text_through_proxy(
    postgres_settings, plain_proxy_port
):
    """Proxy query interception should honor client_encoding when decoding query text."""
    with psycopg2.connect(
        host="127.0.0.1",
        port=plain_proxy_port,
        user=postgres_settings["user"],
        password=postgres_settings["password"],
        dbname=postgres_settings["dbname"],
        sslmode="disable",
        connect_timeout=3,
        client_encoding="LATIN1",
    ) as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("SHOW client_encoding")
            assert cur.fetchone() == ("LATIN1",)

            cur.execute("SELECT 'olá'::text")
            assert cur.fetchone() == ("olá",)
