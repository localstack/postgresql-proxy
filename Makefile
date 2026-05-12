SHELL := /bin/bash
VENV_DIR ?= .venv
VENV_RUN = . $(VENV_DIR)/bin/activate
PIP_CMD ?= pip
PYTHON_CMD ?= python
TEST_DEPS ?= pytest pytest-timeout
LINT_DEPS ?= ruff

PG_TEST_CONTAINER ?= pg-proxy-local-tests
PG_TEST_IMAGE ?= postgres:16
PG_TEST_PORT ?= 55432
PG_TEST_USER ?= postgres
PG_TEST_PASSWORD ?= postgres
PG_TEST_DB ?= postgres

usage:             ## Show this help
	@fgrep -h "##" $(MAKEFILE_LIST) | fgrep -v fgrep | sed -e 's/\\$$//' | sed -e 's/##//'

install:           ## Install dependencies in local virtualenv folder
	(test `which virtualenv` || $(PIP_CMD) install --user virtualenv) && \
		(test -e $(VENV_DIR) || virtualenv $(VENV_OPTS) $(VENV_DIR)) && \
		($(VENV_RUN) && $(PIP_CMD) install --upgrade pip) && \
		(test ! -e requirements.txt || ($(VENV_RUN); $(PIP_CMD) install -r requirements.txt))

publish:           ## Publish the library to the central PyPi repository
	($(VENV_RUN); pip install twine; python ./setup.py sdist && twine upload dist/*)

install-test: install ## Install test dependencies in local virtualenv
	($(VENV_RUN); $(PIP_CMD) install $(TEST_DEPS))

install-lint: install ## Install lint dependencies in local virtualenv
	($(VENV_RUN); $(PIP_CMD) install $(LINT_DEPS))

lint: install-lint ## Format code with ruff
	$(VENV_DIR)/bin/ruff format postgresql_proxy tests plugins

test: ## Start local PostgreSQL container and run all tests
	@set -euo pipefail; \
	cleanup() { docker rm -f $(PG_TEST_CONTAINER) >/dev/null 2>&1 || true; }; \
	trap cleanup EXIT INT TERM; \
	docker rm -f $(PG_TEST_CONTAINER) >/dev/null 2>&1 || true; \
	docker run --name $(PG_TEST_CONTAINER) \
		-e POSTGRES_USER=$(PG_TEST_USER) \
		-e POSTGRES_PASSWORD=$(PG_TEST_PASSWORD) \
		-e POSTGRES_DB=$(PG_TEST_DB) \
		-p $(PG_TEST_PORT):5432 \
		-d $(PG_TEST_IMAGE) >/dev/null; \
	for i in $$(seq 1 45); do \
		if docker exec $(PG_TEST_CONTAINER) pg_isready -U $(PG_TEST_USER) >/dev/null 2>&1; then \
			echo "PostgreSQL ready on 127.0.0.1:$(PG_TEST_PORT)"; \
			break; \
		fi; \
		sleep 1; \
	done; \
	if ! docker exec $(PG_TEST_CONTAINER) pg_isready -U $(PG_TEST_USER) >/dev/null 2>&1; then \
		echo "PostgreSQL did not become ready in time"; \
		exit 1; \
	fi; \
	E2E_PG_HOST=127.0.0.1 \
	E2E_PG_PORT=$(PG_TEST_PORT) \
	E2E_PG_USER=$(PG_TEST_USER) \
	E2E_PG_PASSWORD=$(PG_TEST_PASSWORD) \
	E2E_PG_DB=$(PG_TEST_DB) \
	$(VENV_DIR)/bin/$(PYTHON_CMD) -m pytest -vv

.PHONY: usage install install-test install-lint clean publish test lint
