"""Plugin integration tests.

These tests verify we can load plugins and that plugin behavior works against a real Postgres backend.
"""

import collections
import importlib

import psycopg2
import pytest

import plugins.tableau_hll as hll


@pytest.fixture()
def plugin_context(postgres_settings, monkeypatch):
    # plugin's internal psycopg2 connection does not pass password, so provide it via libpq env var
    monkeypatch.setenv("PGPASSWORD", postgres_settings["password"])

    with psycopg2.connect(sslmode="disable", **postgres_settings) as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute('CREATE SCHEMA IF NOT EXISTS "crm_dim";')
            cur.execute('DROP TABLE IF EXISTS "crm_dim"."crm_data_source";')
            cur.execute(
                """
                DO $$
                BEGIN
                    IF EXISTS (SELECT 1 FROM pg_type WHERE typname = 'hll' AND typtype = 'd') THEN
                        DROP DOMAIN hll;
                    END IF;
                    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'hll') THEN
                        CREATE TYPE hll AS (v text);
                    END IF;
                END $$;
                """
            )
            cur.execute(
                'CREATE TABLE "crm_dim"."crm_data_source" ('
                '"Set of Customers" hll, '
                '"Campaign Name" text);'
            )

    InstanceConfig = collections.namedtuple("InstanceConfig", "redirect")
    Redirect = collections.namedtuple("Redirect", "name host port")
    return {
        "instance_config": InstanceConfig(
            redirect=Redirect(
                name="postgres",
                host=postgres_settings["host"],
                port=postgres_settings["port"],
            )
        ),
        "connect_params": {
            "user": postgres_settings["user"],
            "database": postgres_settings["dbname"],
        },
    }


def test_rewrite_query_for_hll_column(plugin_context):
    src = (
        'SELECT COUNT(DISTINCT "crm_data_source"."Set of Customers") AS "ctd:Set of Customers:ok"\n'
        'FROM "crm_dim"."crm_data_source" "crm_data_source"\n'
        "HAVING (COUNT(1) > 0);"
    )

    res = hll.rewrite_query(src, plugin_context)
    assert "hll_cardinality(hll_union_agg" in res


def test_plugin_module_loads_and_exposes_rewriter():
    module = importlib.import_module("plugins.tableau_hll")
    assert hasattr(module, "rewrite_query")
    assert callable(module.rewrite_query)


def test_does_not_rewrite_non_hll_column(plugin_context):
    src = (
        'SELECT COUNT(DISTINCT "crm_data_source"."Campaign Name") AS "ctd:Campaign Name:ok"\n'
        'FROM "crm_dim"."crm_data_source" "crm_data_source"\n'
        "HAVING (COUNT(1) > 0);"
    )

    res = hll.rewrite_query(src, plugin_context)
    assert "hll_cardinality(hll_union_agg" not in res
