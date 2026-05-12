# Postgresql Proxy

Serves as a proper server that Postgresql clients can connect to. Can modify packets that pass through.

Currently used for rewriting queries to force proper use of postgres-hll module by external proprietary software that doesn't know about that functionality

## Installing

Requires Python `3.13+`.

1. Clone the repository:
   ```bash
   git clone git@github.com:localstack/postgresql-proxy.git
   cd postgresql-proxy
   ```
2. Install dependencies into a local virtualenv:
   ```bash
   make install
   ```
3. Copy the example config and edit it for your environment:
   ```bash
   cp config.yml.example config.yml
   ```

## Configuring
In the `config.yml` file you can define the following things
### Plugins
A list of dynamically loaded modules that reside in the [plugins](plugins) directory. These plugins can be used in later configuration, to intercept queries, commands, or responses. View plugin documentation for example plugins for more details on how to do that.
### Settings
General application settings. Currently the following settings are used
* `log-level` - the log level for the general log. See [python logging](https://docs.python.org/3/library/logging.html) for more details about the logging functionality
* `general-log` - the location for the general log. All general messages go in there.
* `intercept-log` - the location for the intercept log. Intercepted messages and return values from various enabled plugins will be written there. This log can be quite verbose as it contains the full binary messages being circulated.

Make sure to manage the logs yourself, as they accumulate and take up disk space.

### Instances
`instances` is a list of instance definitions. Each instance has a listening port and redirects to a different postgresql instance. They have individual configurations for which message interceptors to use. It **requires**, for every instance, a `listen` directive and `redirect` directive.
* `listen` directive, that must contain a `name` (for logging purposes), `host` and `port` for the listening socket. This is the host and port that external tools will connect to, as if it were the actual PostgreSql server.
* `redirect` directive, that must contain the same components as `listen`, is the address of the actual PostgreSql server that this instance redirects to.
* `intercept` - defines message interceptors
  * `commands` - interceptors for commands (messages from the client)
    * `queries` - interceptors for queries.
    * `connects` - interceptors for connection requests. *Not implemented yet*
  * `responses` - interceptors for responses (messages from PostgreSql server). *Not implemented yet*
  
  Each interceptor definition must have a `plugin`, which should also be present in the [plugins](#Plugins) configuration, and a `function`, that is found directly in that module, that will be called each time with the intercepted message as a byte string, and a context variable that is an instance of the `Proxy` class, that contains connection information and other useful stuff.

## Running

Activate the virtualenv and run the proxy directly:

```bash
source .venv/bin/activate
python -m postgresql_proxy
```

Or run it without activating the venv:

```bash
.venv/bin/python -m postgresql_proxy
```

## Changelog
- v0.3.1
  - Fix SSL COPY stalls by draining pending SSL buffer after recv [#11](https://github.com/localstack/postgresql-proxy/pull/11)
  - Fix intermittent `BlockingIOError` on macOS during SSL negotiation
- v0.3.0
  - Add support for SSL connections [#9](https://github.com/localstack/postgresql-proxy/pull/9)
- v0.2.1
  - Fix partial send of outbound packets [#8](https://github.com/localstack/postgresql-proxy/pull/8)
- v0.2.0
  - Add support for intercepting [ParameterStatus](https://www.postgresql.org/docs/current/protocol-message-formats.html#PROTOCOL-MESSAGE-FORMATS-PARAMETERSTATUS) responses from Postgres [#7](https://github.com/localstack/postgresql-proxy/pull/7)
- v0.1.2
  - Fix error in process_inbound_packet [#6](https://github.com/localstack/postgresql-proxy/pull/6)
- v0.1.1
  - Fix connection termination in [#5](https://github.com/localstack/postgresql-proxy/pull/5)
- v0.1.0
  - Fix connection management in [#4](https://github.com/localstack/postgresql-proxy/pull/4)  
  Improve the connection management of the proxy, connections lifecycle, and improves CPU usage, fix migration done with Prisma
- v0.0.5
  - add support to modify and ignore incoming connection parameters in [#2](https://github.com/localstack/postgresql-proxy/pull/2)  
    Fixes an issue with Redshift python connector using forbidden PostgreSQL connection parameters
  - switch to using twine for package upload in [#3](https://github.com/localstack/postgresql-proxy/pull/3)
- v0.0.4
  - Correctly map postgresql charsets to python charsets in [#1](https://github.com/localstack/postgresql-proxy/pull/1)
- v0.0.3
  - add stop() method to proxy; refactor logging
- v0.0.2
  - fix socket file descriptors under Linux

## Testing

All tests require a real PostgreSQL server and are organized at the top level:

- `test_proxy.py`: proxy behavior tests (connection, SSL, hang regressions)
- `test_plugins.py`: plugin integration tests (HLL rewrite behavior)

### Prerequisites

- Python `3.13` (same version as CI)
- Docker (for local disposable PostgreSQL)
- `psql` (`postgresql-client`)
- `openssl` (SSL tests generate a temporary self-signed cert/key at runtime)

Install Python deps in the project virtualenv:

```bash
make install-test
```

### Which command should I use?

- One-command full local run with disposable Postgres: `make start-pg-and-test`
- Run full suite against an already running Postgres: `make test`
- Run only proxy tests (using your own Postgres): `python -m pytest tests/test_proxy.py -vv`
- Run only plugin tests: `python -m pytest tests/test_plugins.py -vv`

#### 1) Full local suite (recommended)

`make start-pg-and-test` starts a temporary PostgreSQL container, waits for readiness, sets DB env vars, then runs:

```bash
make test
```

Use it when you want one command that matches normal contributor workflow.

```bash
make start-pg-and-test
```

#### 2) Run against an existing PostgreSQL

If you already have PostgreSQL running, set connection env vars and run the tests you need:

```bash
export E2E_PG_HOST=127.0.0.1
export E2E_PG_PORT=5432
export E2E_PG_USER=postgres
export E2E_PG_PASSWORD=postgres
export E2E_PG_DB=postgres

# Proxy tests only
python -m pytest tests/test_proxy.py -vv

# Plugin tests only
python -m pytest tests/test_plugins.py -vv
```

If PostgreSQL is not reachable, tests fail fast at startup.

#### 3) Run CI locally with `act`

Run the GitHub Actions test workflow locally with [`act`](https://github.com/nektos/act):

On macOS, install `act` with Homebrew:

```bash
brew install act
```

```bash
make test-act
```

Useful overrides for local runs:

```bash
# Refresh images explicitly when needed
make test-act ACT_PULL=true

# Match GitHub runner architecture on Apple Silicon (slower)
make test-act ACT_CONTAINER_ARCH=linux/amd64
```
