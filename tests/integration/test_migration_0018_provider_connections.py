"""Migration 0018 — verify the three new tables + constraints + trigger
apply cleanly against a real Postgres.

Spec: docs/architecture/cluster-deploy.md §Schema additions.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError
from testcontainers.postgres import PostgresContainer


@pytest.fixture(scope="module")
def postgres_url() -> str:
    """Spin up a fresh Postgres + alembic upgrade head. Shares one
    container across all tests in this module to amortize startup."""
    with PostgresContainer("postgres:16") as pg:
        url = pg.get_connection_url().replace(
            "postgresql+psycopg2://", "postgresql+psycopg://",
        )
        os.environ["LOOM_DB_URL"] = url
        repo_root = Path(__file__).resolve().parents[2]
        subprocess.run(
            [sys.executable, "-m", "alembic", "-c",
             "migrations/alembic.ini", "upgrade", "head"],
            cwd=repo_root, check=True,
        )
        yield url


@pytest.fixture()
def team_id(postgres_url: str) -> str:
    """A throwaway team for each test so cross-test rows don't collide
    on the (team_id, display_name) partial unique index."""
    engine = create_engine(postgres_url)
    tid = uuid4()
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO teams (id, name) VALUES (:id, :name)",
        ), {"id": tid, "name": f"test-{tid}"})
        conn.execute(text(
            "INSERT INTO team_quotas (team_id) VALUES (:t)",
        ), {"t": tid})
    return str(tid)


def test_three_new_tables_exist(postgres_url: str) -> None:
    engine = create_engine(postgres_url)
    with engine.connect() as conn:
        names = {row[0] for row in conn.execute(text(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public'",
        ))}
    assert {"secrets", "provider_connections", "provider_models_cache"}.issubset(names)


def test_secrets_table_columns(postgres_url: str) -> None:
    engine = create_engine(postgres_url)
    with engine.connect() as conn:
        cols = {row[0] for row in conn.execute(text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'secrets'",
        ))}
    assert cols == {"ref", "ciphertext", "nonce", "master_key_version", "created_at"}


def test_resolved_egress_ips_is_inet_array(postgres_url: str) -> None:
    """resolved_egress_ips uses native Postgres inet[] (not JSONB).
    Catches a regression to the rev-7 design."""
    engine = create_engine(postgres_url)
    with engine.connect() as conn:
        # information_schema reports ARRAY for ARRAY types; udt_name
        # gives the element type. _inet is the array-of-inet UDT.
        result = conn.execute(text(
            "SELECT data_type, udt_name "
            "FROM information_schema.columns "
            "WHERE table_name = 'provider_connections' "
            "  AND column_name = 'resolved_egress_ips'",
        )).one()
    assert result[0] == "ARRAY"
    assert result[1] == "_inet"


def test_check_constraint_rejects_invalid_status(
    postgres_url: str, team_id: str,
) -> None:
    engine = create_engine(postgres_url)
    with engine.begin() as conn, pytest.raises(IntegrityError):
        conn.execute(text(
            "INSERT INTO provider_connections "
            "(team_id, provider_type, display_name, base_url, "
            " upstream_host, encrypted_api_key_ref, status, created_by) "
            "VALUES (:t, 'openai-compatible', 'test', 'https://x', "
            "        'x', 'loom://ref', 'bogus-status', 'admin:0')",
        ), {"t": team_id})


def test_check_constraint_rejects_invalid_provider_type(
    postgres_url: str, team_id: str,
) -> None:
    engine = create_engine(postgres_url)
    with engine.begin() as conn, pytest.raises(IntegrityError):
        conn.execute(text(
            "INSERT INTO provider_connections "
            "(team_id, provider_type, display_name, base_url, "
            " upstream_host, encrypted_api_key_ref, created_by) "
            "VALUES (:t, 'azure-flavor', 'test', 'https://x', "
            "        'x', 'loom://ref', 'admin:0')",
        ), {"t": team_id})


def test_partial_unique_index_allows_reuse_after_soft_delete(
    postgres_url: str, team_id: str,
) -> None:
    """A team can re-use a display_name after the previous connection
    with that name has been soft-deleted."""
    engine = create_engine(postgres_url)
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO provider_connections "
            "(team_id, provider_type, display_name, base_url, "
            " upstream_host, encrypted_api_key_ref, created_by) "
            "VALUES (:t, 'openai-compatible', 'prod', 'https://x', "
            "        'x', 'loom://1', 'admin:0')",
        ), {"t": team_id})

    # Same name, ACTIVE → must fail.
    with engine.begin() as conn, pytest.raises(IntegrityError):
        conn.execute(text(
            "INSERT INTO provider_connections "
            "(team_id, provider_type, display_name, base_url, "
            " upstream_host, encrypted_api_key_ref, created_by) "
            "VALUES (:t, 'openai-compatible', 'prod', 'https://x', "
            "        'x', 'loom://2', 'admin:0')",
        ), {"t": team_id})

    # Soft-delete the first, then insert with the same name → must succeed.
    with engine.begin() as conn:
        conn.execute(text(
            "UPDATE provider_connections SET deleted_at = now() "
            "WHERE team_id = :t AND display_name = 'prod'",
        ), {"t": team_id})
        conn.execute(text(
            "INSERT INTO provider_connections "
            "(team_id, provider_type, display_name, base_url, "
            " upstream_host, encrypted_api_key_ref, created_by) "
            "VALUES (:t, 'openai-compatible', 'prod', 'https://x', "
            "        'x', 'loom://3', 'admin:0')",
        ), {"t": team_id})


def test_updated_at_trigger_fires_on_update(
    postgres_url: str, team_id: str,
) -> None:
    """Every UPDATE bumps updated_at, even when no application-visible
    column changed. Load-bearing for the gateway's indexed-updated_at
    cache invalidation pattern (cluster-deploy.md §Cache durability)."""
    engine = create_engine(postgres_url)
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO provider_connections "
            "(team_id, provider_type, display_name, base_url, "
            " upstream_host, encrypted_api_key_ref, created_by) "
            "VALUES (:t, 'openai-compatible', 'utest', 'https://x', "
            "        'x', 'loom://ref', 'admin:0')",
        ), {"t": team_id})
        before = conn.execute(text(
            "SELECT updated_at FROM provider_connections "
            "WHERE team_id = :t AND display_name = 'utest'",
        ), {"t": team_id}).scalar_one()

    # An UPDATE that touches NOTHING (sets a column to its current
    # value) still fires the trigger.
    with engine.begin() as conn:
        conn.execute(text(
            "UPDATE provider_connections SET status = status "
            "WHERE team_id = :t AND display_name = 'utest'",
        ), {"t": team_id})
        after = conn.execute(text(
            "SELECT updated_at FROM provider_connections "
            "WHERE team_id = :t AND display_name = 'utest'",
        ), {"t": team_id}).scalar_one()

    assert after > before


def test_provider_connection_delete_cascades_to_models_cache(
    postgres_url: str, team_id: str,
) -> None:
    """ON DELETE CASCADE on provider_models_cache.provider_connection_id
    means hard-deleting the parent (e.g., via the future
    `loom admin providers purge` verb) cleans up the cache rows."""
    engine = create_engine(postgres_url)
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO provider_connections "
            "(team_id, provider_type, display_name, base_url, "
            " upstream_host, encrypted_api_key_ref, created_by) "
            "VALUES (:t, 'openai-compatible', 'casc', 'https://x', "
            "        'x', 'loom://ref', 'admin:0') "
            "RETURNING id",
        ), {"t": team_id})
        conn_id = conn.execute(text(
            "SELECT id FROM provider_connections "
            "WHERE team_id = :t AND display_name = 'casc'",
        ), {"t": team_id}).scalar_one()
        conn.execute(text(
            "INSERT INTO provider_models_cache "
            "(provider_connection_id, model_id) "
            "VALUES (:c, 'gpt-4o')",
        ), {"c": conn_id})

    with engine.begin() as conn:
        conn.execute(text(
            "DELETE FROM provider_connections WHERE id = :c",
        ), {"c": conn_id})
        remaining = conn.execute(text(
            "SELECT count(*) FROM provider_models_cache "
            "WHERE provider_connection_id = :c",
        ), {"c": conn_id}).scalar_one()

    assert remaining == 0


def test_models_cache_hidden_reason_check(
    postgres_url: str, team_id: str,
) -> None:
    """hidden_reason can be NULL, 'operator-hidden', or
    'missing-upstream'. Anything else is rejected. Catches a regression
    to the rev-10 design where 'disabled-pricing' was a value (removed)."""
    engine = create_engine(postgres_url)
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO provider_connections "
            "(team_id, provider_type, display_name, base_url, "
            " upstream_host, encrypted_api_key_ref, created_by) "
            "VALUES (:t, 'openai-compatible', 'hr', 'https://x', "
            "        'x', 'loom://ref', 'admin:0')",
        ), {"t": team_id})
        conn_id = conn.execute(text(
            "SELECT id FROM provider_connections "
            "WHERE team_id = :t AND display_name = 'hr'",
        ), {"t": team_id}).scalar_one()

    with engine.begin() as conn, pytest.raises(IntegrityError):
        conn.execute(text(
            "INSERT INTO provider_models_cache "
            "(provider_connection_id, model_id, hidden_reason) "
            "VALUES (:c, 'gpt-4o', 'disabled-pricing')",
        ), {"c": conn_id})


def test_orm_models_match_migration_schema(postgres_url: str) -> None:
    """Smoke-check the ORM models import cleanly + their tablenames
    match what migration 0018 created. If a column drifts between the
    migration and the model, the next test that selects via ORM will
    fail more loudly than this one, but this catches the simplest
    typos at module-import time."""
    from loom.db.schema import (
        ProviderConnection,
        ProviderModelCache,
        Secret,
    )

    assert Secret.__tablename__ == "secrets"
    assert ProviderConnection.__tablename__ == "provider_connections"
    assert ProviderModelCache.__tablename__ == "provider_models_cache"

    expected_pc_cols = {
        "id", "team_id", "provider_type", "display_name", "base_url",
        "upstream_host", "resolved_egress_ips", "egress_ips_refreshed_at",
        "egress_ips_min_ttl_seconds", "encrypted_api_key_ref",
        "allowed_models", "status", "last_validated_at",
        "last_validation_error", "pricing_source", "pricing_data",
        "created_by", "deleted_at", "created_at", "updated_at",
    }
    actual_pc_cols = {c.name for c in ProviderConnection.__table__.columns}
    assert actual_pc_cols == expected_pc_cols, (
        f"ORM-schema drift: missing={expected_pc_cols - actual_pc_cols}, "
        f"extra={actual_pc_cols - expected_pc_cols}"
    )
