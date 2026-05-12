# Testing Guide

All tests in this repo require a real PostgreSQL server and are organized at the top level:

- `test_proxy.py`: proxy behavior tests (connection, SSL, hang regressions)
- `test_plugins.py`: plugin integration tests (HLL rewrite behavior)

## Prerequisites

- Python `3.13` (same version as CI)
- Docker (for local disposable PostgreSQL)
- `psql` (`postgresql-client`)
- `openssl` (SSL tests generate a temporary self-signed cert/key at runtime)

Install Python deps in the project virtualenv:

```bash
make install-test
```

## Which command should I use?

- Fastest full local run with disposable Postgres: `make test`
- Run only proxy tests (using your own Postgres): `python -m pytest tests/test_proxy.py -vv`
- Run only plugin tests: `python -m pytest tests/test_plugins.py -vv`

## 1) Full local suite (recommended)

`make test` starts a temporary PostgreSQL container, waits for readiness, sets DB env vars, then runs:

```bash
python -m pytest -vv
```

Use it when you want one command that matches normal contributor workflow.

```bash
make test
```

## 2) DB-backed proxy tests against an existing PostgreSQL

If you already have PostgreSQL running, set connection env vars and run only proxy tests:

```bash
export E2E_PG_HOST=127.0.0.1
export E2E_PG_PORT=5432
export E2E_PG_USER=postgres
export E2E_PG_PASSWORD=postgres
export E2E_PG_DB=postgres
python -m pytest tests/test_proxy.py -vv
```

If PostgreSQL is not reachable, tests fail fast at startup.

## 3) Plugin integration tests

```bash
python -m pytest tests/test_plugins.py -vv
```

Requires PostgreSQL to be running with the `E2E_PG_*` env vars set (see section 2).
