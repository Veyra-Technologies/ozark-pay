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
            ).fetchone()[0] == '0017_teamlead'
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


def test_postgres_teamlead_clean_downgrade_immutable_trigger_and_finance_guard():
    admin_url, database_url, database_name = _database_urls()
    with psycopg.connect(admin_url, autocommit=True) as admin:
        admin.execute(
            sql.SQL('CREATE DATABASE {}').format(sql.Identifier(database_name))
        )
    try:
        upgrade = _alembic(database_url, 'upgrade', 'head')
        assert upgrade.returncode == 0, upgrade.stdout + upgrade.stderr
        clean = _alembic(
            database_url,
            'downgrade',
            '0016_rolling_rapira_freshness',
        )
        assert clean.returncode == 0, clean.stdout + clean.stderr
        reupgrade = _alembic(database_url, 'upgrade', 'head')
        assert reupgrade.returncode == 0, reupgrade.stdout + reupgrade.stderr

        teamlead_id = uuid.uuid4()
        balance_id = uuid.uuid4()
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
                VALUES (%s, %s, %s, 'teamlead', true, false, 0, true)
                """,
                (
                    teamlead_id,
                    f'teamlead-migration-{uuid.uuid4().hex}@example.test',
                    'not-used-in-test',
                ),
            )
            db.execute(
                """
                INSERT INTO teamlead_balances (
                    id,
                    teamlead_id,
                    available_rub,
                    frozen_rub,
                    debt_rub,
                    total_earned_rub,
                    total_paid_rub
                )
                VALUES (%s, %s, 1, 0, 0, 1, 0)
                """,
                (balance_id, teamlead_id),
            )
            db.execute(
                """
                INSERT INTO teamlead_ledger_entries (
                    id,
                    teamlead_id,
                    entry_type,
                    amount_rub,
                    available_after,
                    frozen_after,
                    debt_after,
                    idempotency_key,
                    reason
                )
                VALUES (%s, %s, 'manual_adjustment', 1, 1, 0, 0, %s, %s)
                """,
                (
                    ledger_id,
                    teamlead_id,
                    f'teamlead-migration-{uuid.uuid4().hex}',
                    'migration guard test',
                ),
            )
            db.commit()
            with pytest.raises(
                psycopg.errors.RaiseException,
                match='teamlead ledger entries are immutable',
            ):
                db.execute(
                    """
                    UPDATE teamlead_ledger_entries
                       SET reason = 'forbidden'
                     WHERE id = %s
                    """,
                    (ledger_id,),
                )
            db.rollback()
            with pytest.raises(
                psycopg.errors.RaiseException,
                match='teamlead ledger entries are immutable',
            ):
                db.execute(
                    'DELETE FROM teamlead_ledger_entries WHERE id = %s',
                    (ledger_id,),
                )
            db.rollback()

        blocked = _alembic(
            database_url,
            'downgrade',
            '0016_rolling_rapira_freshness',
        )
        assert blocked.returncode != 0
        assert (
            'refusing destructive TeamLead downgrade after financial activity'
            in blocked.stdout + blocked.stderr
        )
        with psycopg.connect(database_url) as verify:
            assert verify.execute(
                'SELECT version_num FROM alembic_version'
            ).fetchone()[0] == '0017_teamlead'
            assert verify.execute(
                'SELECT count(*) FROM teamlead_ledger_entries'
            ).fetchone()[0] == 1
    finally:
        with psycopg.connect(admin_url, autocommit=True) as admin:
            admin.execute(
                sql.SQL('DROP DATABASE IF EXISTS {} WITH (FORCE)').format(
                    sql.Identifier(database_name)
                )
            )


def test_postgres_teamlead_role_is_stored_as_string_and_0017_migration_has_expected_indexes():
    admin_url, database_url, database_name = _database_urls()
    with psycopg.connect(admin_url, autocommit=True) as admin:
        admin.execute(
            sql.SQL('CREATE DATABASE {}').format(sql.Identifier(database_name))
        )
    try:
        upgrade = _alembic(database_url, 'upgrade', '0016_rolling_rapira_freshness')
        assert upgrade.returncode == 0, upgrade.stdout + upgrade.stderr
        to_0017 = _alembic(database_url, 'upgrade', '0017_teamlead')
        assert to_0017.returncode == 0, to_0017.stdout + to_0017.stderr
        teamlead_id = uuid.uuid4()
        trader_id = uuid.uuid4()
        with psycopg.connect(database_url) as db:
            role_column = db.execute(
                """
                SELECT data_type
                  FROM information_schema.columns
                 WHERE table_name = 'users'
                   AND column_name = 'role'
                """
            ).fetchone()
            assert role_column is not None
            assert role_column[0] == 'character varying'
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
                VALUES
                    (%s, %s, 'unused', 'teamlead', true, false, 0, true),
                    (%s, %s, 'unused', 'trader', true, false, 0, false)
                """,
                (
                    teamlead_id,
                    f'teamlead-clean-{uuid.uuid4().hex}@example.test',
                    trader_id,
                    f'trader-clean-{uuid.uuid4().hex}@example.test',
                ),
            )
            db.execute(
                """
                INSERT INTO teamlead_trader_assignments (
                    id,
                    teamlead_id,
                    trader_id,
                    commission_percent,
                    effective_from,
                    creation_reason
                )
                VALUES (%s, %s, %s, 0.5, now(), 'clean downgrade fixture')
                """,
                (uuid.uuid4(), teamlead_id, trader_id),
            )
            index_names = {
                row[0]
                for row in db.execute(
                    """
                    SELECT indexname
                      FROM pg_indexes
                     WHERE schemaname = 'public'
                       AND indexname IN (
                           'uq_teamlead_assignment_active_trader',
                           'uq_teamlead_settlement_pending',
                           'uq_teamlead_settlement_completed_tx'
                       )
                    """
                ).fetchall()
            }
            assert index_names == {
                'uq_teamlead_assignment_active_trader',
                'uq_teamlead_settlement_pending',
                'uq_teamlead_settlement_completed_tx',
            }
            constraint_names = {
                row[0]
                for row in db.execute(
                    """
                    SELECT conname
                      FROM pg_constraint
                     WHERE conrelid = 'teamlead_settlements'::regclass
                       AND conname IN (
                           'ck_teamlead_settlement_total_rub',
                           'ck_teamlead_settlement_state'
                       )
                    """
                ).fetchall()
            }
            assert constraint_names == {
                'ck_teamlead_settlement_total_rub',
                'ck_teamlead_settlement_state',
            }
            assert db.execute(
                """
                SELECT 1
                  FROM pg_indexes
                 WHERE schemaname = 'public'
                   AND tablename = 'teamlead_settlements'
                   AND indexname = 'uq_teamlead_settlement_completed_tx'
                """
            ).fetchone() is not None
            db.commit()
        rollback = _alembic(database_url, 'downgrade', '0016_rolling_rapira_freshness')
        assert rollback.returncode == 0, rollback.stdout + rollback.stderr
        with psycopg.connect(database_url) as db:
            assert db.execute(
                'SELECT role FROM users WHERE id = %s',
                (teamlead_id,),
            ).fetchone()[0] == 'teamlead'
            assert db.execute(
                "SELECT to_regclass('public.teamlead_trader_assignments')"
            ).fetchone()[0] is None
        reupgrade = _alembic(database_url, 'upgrade', '0017_teamlead')
        assert reupgrade.returncode == 0, reupgrade.stdout + reupgrade.stderr
        with psycopg.connect(database_url) as db:
            assert db.execute(
                'SELECT role FROM users WHERE id = %s',
                (teamlead_id,),
            ).fetchone()[0] == 'teamlead'
            assert db.execute(
                'SELECT count(*) FROM teamlead_trader_assignments'
            ).fetchone()[0] == 0
    finally:
        with psycopg.connect(admin_url, autocommit=True) as admin:
            admin.execute(
                sql.SQL('DROP DATABASE IF EXISTS {} WITH (FORCE)').format(
                    sql.Identifier(database_name)
                )
            )
