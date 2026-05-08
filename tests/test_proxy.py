import contextlib
import os
import shutil
import socket
import ssl
import subprocess
import tempfile
import threading
import time

import psycopg2
import pytest

from postgresql_proxy import config_schema as cfg
from postgresql_proxy.proxy import Proxy




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
def _run_proxy(postgres_settings, ssl_context: ssl.SSLContext | None = None):
    proxy_port = _get_free_tcp_port()
    instance = cfg.InstanceSettings(
        {
            "listen": {"name": "proxy", "host": "127.0.0.1", "port": proxy_port},
            "redirect": {
                "name": "postgres",
                "host": postgres_settings["host"],
                "port": postgres_settings["port"],
            },
            # Keep interceptors active with default no-op behavior.
            "intercept": {"commands": {}, "responses": {}},
        }
    )
    if not hasattr(instance.intercept.responses, "parameter_status"):
        instance.intercept.responses.parameter_status = []

    proxy = Proxy(instance, plugins={}, debug=True, ssl_context=ssl_context)
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
def test_psql_ssl_file_batch_stress_no_hang(postgres_settings, ssl_proxy_port):
    if shutil.which("psql") is None:
        pytest.fail("psql is required for this test but was not found in PATH")

    sql_file_path = None
    try:
        sql_content = _build_dump_like_sql(table_count=24, rows_per_table=300)
        with tempfile.NamedTemporaryFile("w", suffix=".sql", delete=False) as tmp_file:
            tmp_file.write(sql_content)
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
                    f"(run={run_idx + 1}, rc={result.returncode}, elapsed={elapsed:.2f}s) "
                    f"stdout_tail={out_tail} stderr_tail={err_tail}"
                )

            if "BATCH_OK" not in (result.stdout or ""):
                out_tail = "\n".join((result.stdout or "").splitlines()[-20:])
                pytest.fail(
                    "psql -f batch succeeded but expected marker missing "
                    f"(run={run_idx + 1}, elapsed={elapsed:.2f}s) stdout_tail={out_tail}"
                )
    finally:
        if sql_file_path and os.path.exists(sql_file_path):
            os.unlink(sql_file_path)
