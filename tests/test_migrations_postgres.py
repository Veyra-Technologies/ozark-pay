import os
import subprocess
import sys
import uuid
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import psycopg
import pytest
from psycopg import sql


ROOT = Path(__file__).resolve().parents[1]


def _sync_url(value: str) -> str:
    return (
        value.replace('postgresql+asyncpg://', 'postgresql://', 1)
        .replace('postgresql+psycopg://', 'postgresql://', 1)
    )


def _database_urls():
    source = os.getenv('TEST_DATABASE_URL', '')
    if not source:
        pytest.skip('TEST_DATABASE_URL is required for migration tests')
    sync_source = _sync_url(source)
    parts = urlsplit(sync_source)
    database_name = f'ozark_migration_{uuid.uuid4().hex[:16]}'
    admin_url = urlunsplit(
        (parts.scheme, parts.netloc, '/postgres', parts.query, parts.fragment)
    )
    database_url = urlunsplit(
        (
            parts.scheme,
            parts.netloc,
            f'/{database_name}',
            parts.query,
            parts.fragment,
        )
    )
    return admin_url, database_url, database_name


def _alembic(database_url: str, *args: str) -> subprocess.CompletedProcess:
    env = {
        **os.environ,
        'DATABASE_URL': database_url.replace(
            'postgresql://',
            'postgresql+asyncpg://',
            1,
        ),
        'SYNC_DATABASE_URL': database_url.replace(
            'postgresql://',
            'postgresql+psycopg://',
            1,
        ),
    }
    return subprocess.run(
        [sys.executable, '-m', 'alembic', *args],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def test_postgres_clean_downgrade_and_funded_rolling_downgrade_guard():
    admin_url, database_url, database_name = _database_urls()
    with psycopg.connect(admin_url, autocommit=True) as admin:
        admin.execute(
            sql.SQL('CREATE DATABASE {}').format(sql.Identifier(database_name))
        )
    try:
        upgrade = _alembic(database_url, 'upgrade', 'head')
        assert upgrade.returncode == 0, upgrade.stdout + upgrade.stderr

        clean_downgrade = _alembic(
            database_url,
            'downgrade',
            '0014_deposit_finance',
        )
        assert clean_downgrade.returncode == 0, (
            clean_downgrade.stdout + clean_downgrade.stderr
        )
        reupgrade = _alembic(database_url, 'upgrade', 'head')
        assert reupgrade.returncode == 0, reupgrade.stdout + reupgrade.stderr

        owner_id = uuid.uuid4()
        merchant_id = uuid.uuid4()
        account_id = uuid.uuid4()
        ledger_id = uuid.uuid4()
        with psycopg.connect(database_url) as db:
            db.execute(
                """
                INSERT INTO users (
                    id,
                    email,
                    password_hash,
                    role,
                    is_active,
                    is_locked,
                    failed_login_count,
                    twofa_enabled
                )
                VALUES (%s, %s, %s, 'merchant', true, false, 0, false)
                """,
                (
                    owner_id,
                    f'migration-{uuid.uuid4().hex}@example.test',
                    'not-used-in-test',
                ),
            )
            db.execute(
                """
                INSERT INTO merchants (
                    id,
                    owner_id,
                    name,
                    ip_whitelist,
                    sandbox_mode
                )
                VALUES (
                    %s,
                    %s,
                    'Migration guard merchant',
                    '[]'::json,
                    true
                )
                """,
                (merchant_id, owner_id),
            )
            db.execute(
                """
                INSERT INTO merchant_rolling_accounts (
                    id,
                    merchant_id,
                    principal_usdt,
                    recovered_usdt,
                    outstanding_usdt,
                    status
                )
                VALUES (%s, %s, 1, 0, 1, 'active')
                """,
                (account_id, merchant_id),
            )
            db.execute(
                """
                INSERT INTO merchant_rolling_ledger_entries (
                    id,
                    rolling_account_id,
                    merchant_id,
                    entry_type,
                    amount_usdt,
                    network,
                    destination_address,
                    tx_hash,
                    funded_at,
                    reason,
                    idempotency_key
                )
                VALUES (
                    %s,
                    %s,
                    %s,
                    'funding',
                    1,
                    'TESTNET',
                    'test-only-address',
                    %s,
                    now(),
                    'migration guard test',
                    %s
                )
                """,
                (
                    ledger_id,
                    account_id,
                    merchant_id,
                    f'test-{uuid.uuid4().hex}',
                    f'test-{uuid.uuid4().hex}',
                ),
            )
            db.commit()
            with pytest.raises(
                psycopg.errors.RaiseException,
                match='rolling ledger entries are immutable',
            ):
                db.execute(
                    """
                    UPDATE merchant_rolling_ledger_entries
                       SET reason = 'forbidden update'
                     WHERE id = %s
                    """,
                    (ledger_id,),
                )
            db.rollback()
            with pytest.raises(
                psycopg.errors.RaiseException,
                match='rolling ledger entries are immutable',
            ):
                db.execute(
                    """
                    DELETE FROM merchant_rolling_ledger_entries
                     WHERE id = %s
                    """,
                    (ledger_id,),
                )
            db.rollback()

        blocked = _alembic(
            database_url,
            'downgrade',
            '0014_deposit_finance',
        )
        assert blocked.returncode != 0
        assert (
            'refusing destructive Rolling downgrade after funding or traffic'
            in blocked.stdout + blocked.stderr
        )
        with psycopg.connect(database_url) as verify:
            assert verify.execute(
                'SELECT version_num FROM alembic_version'
            ).fetchone()[0] == '0016_rolling_rapira_freshness'
            assert verify.execute(
                'SELECT count(*) FROM merchant_rolling_ledger_entries'
            ).fetchone()[0] == 1
    finally:
        with psycopg.connect(admin_url, autocommit=True) as admin:
            admin.execute(
                sql.SQL('DROP DATABASE IF EXISTS {} WITH (FORCE)').format(
                    sql.Identifier(database_name)
                )
            )
