"""Expand country configuration for TJ; financial admission remains DB-blocked.

No existing cabinet, fee, balance, key or operation is updated. Do not apply to
production before a verified backup, restoration rehearsal and owner approval.
"""
from datetime import datetime, timezone
import json
import uuid

from alembic import op
import sqlalchemy as sa

revision = '0038_tj_cabinet_preparation'
down_revision = '0037_ge_bank_accounts'
branch_labels = None
depends_on = None

USER_OLD = "(working_country IS NULL AND working_currency IS NULL) OR (working_country IS NOT NULL AND working_currency IS NOT NULL AND ((working_country='RU' AND working_currency='RUB') OR (working_country='AM' AND working_currency='AMD') OR (working_country='GE' AND working_currency='GEL')))"
SOURCE_OLD = "(working_country IS NULL AND working_rate_source IS NULL) OR (working_country IS NOT NULL AND working_rate_source IS NOT NULL AND ((working_country='RU' AND working_rate_source='rapira') OR (working_country IN ('AM','GE') AND working_rate_source='xe')))"
MERCHANT_OLD = "(working_country IS NULL AND rate_parser_id IS NULL) OR (working_country IS NOT NULL AND rate_parser_id IS NOT NULL AND business_id IS NOT NULL AND ((working_country='RU' AND rate_parser_id='rapira') OR (working_country='KZ' AND rate_parser_id='binance_p2p_kaspi_buy_100000_avg10') OR (working_country='GE' AND rate_parser_id='xe')))"
CURRENCY_OLD = "working_currency IS NULL OR (working_country='RU' AND working_currency='RUB') OR (working_country='KZ' AND working_currency='KZT') OR (working_country='GE' AND working_currency='GEL')"
TARIFF_OLD = "(direction='kz-ge' AND ((basis='base_usdt' AND currency='USDT' AND ((party='merchant' AND country='KZ') OR (party='trader' AND country='GE'))) OR (basis='local_amount' AND ((party='merchant' AND country='KZ' AND currency='KZT') OR (party='trader' AND country='GE' AND currency='GEL'))))) OR (direction='ru' AND party='merchant' AND country='RU' AND currency='RUB' AND basis='local_amount')"
GUARD_OLD = "IF NEW.enabled AND profile_country <> 'GE' THEN"
GUARD_NEW = "IF NEW.enabled AND profile_country NOT IN ('GE','TJ') THEN"
DEPOSIT_BLOCK = "\n      IF TG_TABLE_NAME='deposits' AND (NEW.currency='TJS' OR EXISTS (SELECT 1 FROM merchants WHERE id=NEW.merchant_id AND working_country='TJ')) THEN RAISE EXCEPTION 'TJ financial contract is not approved'; END IF;\n"
MERCHANT_HISTORY_GUARD = """IF NEW.working_country='TJ' AND (TG_OP='INSERT' OR OLD.working_country IS DISTINCT FROM 'TJ') AND (
      EXISTS(SELECT 1 FROM balances WHERE merchant_id=NEW.id) OR
      EXISTS(SELECT 1 FROM deposits WHERE merchant_id=NEW.id) OR
      EXISTS(SELECT 1 FROM payouts WHERE merchant_id=NEW.id) OR
      EXISTS(SELECT 1 FROM ledger_entries WHERE merchant_id=NEW.id) OR
      EXISTS(SELECT 1 FROM merchant_settlements WHERE merchant_id=NEW.id) OR
      EXISTS(SELECT 1 FROM merchant_rolling_accounts WHERE merchant_id=NEW.id) OR
      EXISTS(SELECT 1 FROM crossborder_operations WHERE merchant_id=NEW.id)) THEN
      RAISE EXCEPTION 'TJ requires a new empty merchant cabinet'; END IF;
      """


def replace_check(table, name, expression):
    op.drop_constraint(name, table, type_='check')
    op.create_check_constraint(name, table, expression)


def upgrade():
    op.create_check_constraint('ck_tj_legacy_trader_balance','users',"working_country IS DISTINCT FROM 'TJ' OR (trader_balance=0 AND trader_hold=0)")
    op.add_column('aggregator_accounts', sa.Column('working_country',sa.String(2),nullable=True))
    op.add_column('aggregator_accounts', sa.Column('working_currency',sa.String(3),nullable=True))
    op.create_check_constraint('ck_aggregator_country_currency','aggregator_accounts',"(working_country IS NULL AND working_currency IS NULL) OR COALESCE(working_country='TJ' AND working_currency='TJS',false)")
    op.execute("""CREATE FUNCTION guard_aggregator_country() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF TG_OP='UPDATE' AND (NEW.working_country IS DISTINCT FROM OLD.working_country OR NEW.working_currency IS DISTINCT FROM OLD.working_currency OR NEW.platform_merchant_id IS DISTINCT FROM OLD.platform_merchant_id) THEN
        RAISE EXCEPTION 'aggregator country/cabinet is immutable'; END IF;
      IF NEW.working_country='TJ' AND NOT EXISTS(SELECT 1 FROM merchants WHERE id=NEW.platform_merchant_id AND working_country='TJ' AND working_currency='TJS') THEN
        RAISE EXCEPTION 'aggregator cabinet country mismatch'; END IF;
      RETURN NEW;
    END $$""")
    op.execute('CREATE TRIGGER aggregator_country_guard BEFORE INSERT OR UPDATE ON aggregator_accounts FOR EACH ROW EXECUTE FUNCTION guard_aggregator_country()')
    op.create_check_constraint('ck_tj_requisite_limits','requisites',"country <> 'TJ' OR (min_check >= 10 AND max_check >= min_check)")
    replace_check('users', 'ck_users_work_country', f"({USER_OLD}) OR COALESCE(working_country='TJ' AND working_currency='TJS',false)")
    replace_check('users', 'ck_users_work_rate_source', f"({SOURCE_OLD}) OR COALESCE(working_country='TJ' AND working_rate_source='rapira_green_tjs',false)")
    replace_check('merchants', 'ck_merchant_country_parser', f"({MERCHANT_OLD}) OR COALESCE(working_country='TJ' AND rate_parser_id='rapira_green_tjs' AND business_id IS NOT NULL,false)")
    replace_check('merchants', 'ck_merchant_currency', f"CASE WHEN working_country='TJ' THEN working_currency IS NOT DISTINCT FROM 'TJS' ELSE ({CURRENCY_OLD}) END")
    replace_check('bank_directory', 'ck_bank_directory_country', "country IN ('AM','GE','TJ')")
    replace_check('bank_directory', 'ck_bank_directory_type', "institution_type IN ('bank','microbank','payment_system')")
    replace_check('country_tariff_plans', 'ck_country_tariff_direction', "direction IN ('kz-ge','ru','tj-tj')")
    replace_check('country_tariff_plans', 'ck_country_tariff_dimensions', f"({TARIFF_OLD}) OR (direction='tj-tj' AND country='TJ' AND currency='TJS' AND basis='local_amount' AND party IN ('merchant','trader'))")
    replace_check('trader_wallets', 'ck_trader_wallet_usdt', "currency IN ('USDT','TJS')")
    replace_check('trader_wallet_ledger_entries', 'ck_trader_wallet_ledger_usdt', "currency IN ('USDT','TJS')")
    bind = op.get_bind()
    definition = bind.scalar(sa.text("SELECT pg_get_functiondef('guard_payment_jurisdiction()'::regprocedure)"))
    if GUARD_OLD not in definition:
        raise RuntimeError('Unexpected payment guard; manual review required')
    definition = definition.replace(GUARD_OLD, GUARD_NEW)
    # Put the block in the deposits branch (NEW.merchant_id is not a Requisite field).
    anchor = 'IF NEW.requisites_id IS NULL THEN RETURN NEW; END IF;'
    if anchor not in definition:
        raise RuntimeError('Unexpected deposit guard; manual review required')
    op.execute(definition.replace(anchor, DEPOSIT_BLOCK + anchor))
    cabinet = bind.scalar(sa.text("SELECT pg_get_functiondef('validate_merchant_cabinet()'::regprocedure)"))
    if "NEW.working_country NOT IN ('RU','KZ')" not in cabinet:
        raise RuntimeError('Unexpected merchant cabinet guard')
    cabinet = cabinet.replace("NEW.working_country NOT IN ('RU','KZ')", "NEW.working_country NOT IN ('RU','KZ','TJ')")
    cabinet = cabinet.replace("OR (NEW.working_country='KZ' AND m='kz-ge')", "OR (NEW.working_country='KZ' AND m='kz-ge') OR (NEW.working_country='TJ' AND m='c2c')")
    cabinet = cabinet.replace("OR NEW.working_currency IS NULL", "OR (NEW.working_country='TJ' AND (plan.direction <> 'tj-tj' OR plan.currency <> 'TJS' OR plan.basis <> 'local_amount')) OR NEW.working_currency IS NULL")
    cabinet = cabinet.replace('IF NEW.tariff_plan_id IS NOT NULL THEN', MERCHANT_HISTORY_GUARD + 'IF NEW.tariff_plan_id IS NOT NULL THEN')
    op.execute(cabinet)
    op.execute("""CREATE FUNCTION guard_tj_wallet() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE owner_country text; wallet_owner uuid; wallet_currency text;
    BEGIN
      IF TG_TABLE_NAME='users' THEN
        IF NEW.working_country='TJ' AND (TG_OP='INSERT' OR OLD.working_country IS DISTINCT FROM 'TJ') AND (
          EXISTS(SELECT 1 FROM trader_wallets WHERE trader_id=NEW.id) OR
          EXISTS(SELECT 1 FROM trader_ledger_entries WHERE trader_id=NEW.id) OR
          EXISTS(SELECT 1 FROM requisites WHERE trader_id=NEW.id)) THEN
          RAISE EXCEPTION 'TJ requires a new empty trader cabinet'; END IF;
      ELSIF TG_TABLE_NAME='balances' THEN
        SELECT working_country INTO owner_country FROM merchants WHERE id=NEW.merchant_id;
        IF (owner_country='TJ' AND NEW.currency NOT IN ('TJS','USDT')) OR
          (NEW.currency='TJS' AND owner_country IS DISTINCT FROM 'TJ') THEN
          RAISE EXCEPTION 'merchant wallet country/currency mismatch'; END IF;
      ELSE
        SELECT working_country INTO owner_country FROM users WHERE id=NEW.trader_id;
        IF NEW.currency='TJS' AND owner_country IS DISTINCT FROM 'TJ' THEN
          RAISE EXCEPTION 'TJS trader wallet requires TJ cabinet'; END IF;
        IF TG_TABLE_NAME='trader_wallet_ledger_entries' THEN
          SELECT trader_id,currency INTO wallet_owner,wallet_currency FROM trader_wallets WHERE id=NEW.wallet_id;
          IF wallet_owner IS DISTINCT FROM NEW.trader_id OR wallet_currency IS DISTINCT FROM NEW.currency THEN
            RAISE EXCEPTION 'wallet ledger owner/currency mismatch'; END IF;
        END IF;
      END IF;
      RETURN NEW;
    END $$""")
    for table in ('users','balances','trader_wallets','trader_wallet_ledger_entries'):
        op.execute(f'CREATE TRIGGER {table}_tj_guard BEFORE INSERT OR UPDATE ON {table} FOR EACH ROW EXECUTE FUNCTION guard_tj_wallet()')
    for bank_id, name, kind, source in (
        ('tj-dushanbe-city', 'Душанбе Сити (Dushanbe City)', 'bank', 'https://next.dc.tj/'),
        ('tj-korti-milli', 'Корти Милли', 'payment_system', 'https://www.nbt.tj/ru/payment_system/card_market.php'),
    ):
        bind.execute(sa.text("""INSERT INTO bank_directory
          (id,country,name,institution_type,status,aliases,source_url,verified_on)
          VALUES (:id,'TJ',:name,:kind,'active','[]',:source,'2026-10-17')"""),
          dict(id=bank_id,name=name,kind=kind,source=source))
    bind.execute(sa.text("""INSERT INTO parser_configurations
      (id,parser_id,version,country,direction,parameters,is_active,created_at,updated_at)
      VALUES (:id,'rapira_green_tjs',1,'TJ','tj-tj',CAST(:parameters AS jsonb),false,:now,:now)"""),
      {'id':uuid.uuid4(),'now':datetime.now(timezone.utc), 'parameters':json.dumps(
          {'refresh_seconds':15,'ttl_seconds':60,'contract_confirmed':False})})


def downgrade():
    # A clean, unused expansion is reversible. Once TJ is configured, keep its
    # schema/history and roll back application code instead of destroying data.
    bind = op.get_bind()
    op.execute('LOCK TABLE users,merchants,requisites,country_tariff_plans,trader_wallets,trader_wallet_ledger_entries,balances,aggregator_accounts,parser_configurations,parser_assignments,rate_observations,bank_directory,fee_rules,deposits,payouts IN ACCESS EXCLUSIVE MODE')
    checks = ("SELECT count(*) FROM users WHERE working_country='TJ'",
              "SELECT count(*) FROM merchants WHERE working_country='TJ'",
              "SELECT count(*) FROM requisites WHERE country='TJ'",
              "SELECT count(*) FROM country_tariff_plans WHERE country='TJ'",
              "SELECT count(*) FROM trader_wallets WHERE currency='TJS'",
              "SELECT count(*) FROM trader_wallet_ledger_entries WHERE currency='TJS'",
              "SELECT count(*) FROM balances WHERE currency='TJS'",
              "SELECT count(*) FROM aggregator_accounts WHERE working_country IS NOT NULL OR working_currency IS NOT NULL",
              "SELECT count(*) FROM fee_rules WHERE currency='TJS'",
              "SELECT count(*) FROM deposits WHERE currency='TJS'",
              "SELECT count(*) FROM payouts WHERE currency='TJS'",
              "SELECT count(*) FROM parser_assignments a JOIN parser_configurations c ON c.id=a.configuration_id WHERE c.parser_id='rapira_green_tjs'",
              "SELECT count(*) FROM rate_observations o JOIN parser_configurations c ON c.id=o.configuration_id WHERE c.parser_id='rapira_green_tjs'",
              "SELECT count(*) FROM parser_configurations WHERE (country='TJ' OR parser_id='rapira_green_tjs') AND NOT (parser_id='rapira_green_tjs' AND country='TJ' AND direction='tj-tj' AND version=1 AND NOT is_active AND created_by IS NULL AND parameters=jsonb_build_object('refresh_seconds',15,'ttl_seconds',60,'contract_confirmed',false))",
              "SELECT count(*) FROM bank_directory WHERE country='TJ' AND NOT (status='active' AND aliases::jsonb='[]'::jsonb AND verified_on='2026-10-17' AND ((id='tj-dushanbe-city' AND name='Душанбе Сити (Dushanbe City)' AND institution_type='bank' AND source_url='https://next.dc.tj/') OR (id='tj-korti-milli' AND name='Корти Милли' AND institution_type='payment_system' AND source_url='https://www.nbt.tj/ru/payment_system/card_market.php')))")
    if any(bind.scalar(sa.text(query)) for query in checks):
        raise RuntimeError('TJ cabinets/history exist; retain the expanded schema and use an approved rollback plan')
    definition = bind.scalar(sa.text("SELECT pg_get_functiondef('guard_payment_jurisdiction()'::regprocedure)"))
    if GUARD_NEW not in definition or DEPOSIT_BLOCK not in definition:
        raise RuntimeError('Unexpected TJ payment guard; manual review required')
    op.execute(definition.replace(GUARD_NEW, GUARD_OLD).replace(DEPOSIT_BLOCK, ''))
    cabinet = bind.scalar(sa.text("SELECT pg_get_functiondef('validate_merchant_cabinet()'::regprocedure)"))
    if MERCHANT_HISTORY_GUARD not in cabinet:
        raise RuntimeError('Unexpected TJ merchant history guard; manual review required')
    cabinet = cabinet.replace(MERCHANT_HISTORY_GUARD, '')
    cabinet = cabinet.replace("NEW.working_country NOT IN ('RU','KZ','TJ')", "NEW.working_country NOT IN ('RU','KZ')")
    cabinet = cabinet.replace(" OR (NEW.working_country='TJ' AND m='c2c')", '')
    cabinet = cabinet.replace("OR (NEW.working_country='TJ' AND (plan.direction <> 'tj-tj' OR plan.currency <> 'TJS' OR plan.basis <> 'local_amount')) ", '')
    op.execute(cabinet)
    for table in ('users','balances','trader_wallets','trader_wallet_ledger_entries'):
        op.execute(f'DROP TRIGGER {table}_tj_guard ON {table}')
    op.execute('DROP FUNCTION guard_tj_wallet()')
    op.execute('DROP TRIGGER aggregator_country_guard ON aggregator_accounts')
    op.execute('DROP FUNCTION guard_aggregator_country()')
    op.drop_constraint('ck_aggregator_country_currency','aggregator_accounts',type_='check')
    op.drop_column('aggregator_accounts','working_currency')
    op.drop_column('aggregator_accounts','working_country')
    op.drop_constraint('ck_tj_legacy_trader_balance','users',type_='check')
    op.drop_constraint('ck_tj_requisite_limits','requisites',type_='check')
    op.execute('ALTER TABLE parser_configurations DISABLE TRIGGER parser_configurations_immutable')
    op.execute("DELETE FROM parser_configurations WHERE parser_id='rapira_green_tjs'")
    op.execute('ALTER TABLE parser_configurations ENABLE TRIGGER parser_configurations_immutable')
    op.execute("DELETE FROM bank_directory WHERE country='TJ'")
    for table,name,expression in (
        ('users','ck_users_work_country',USER_OLD),
        ('users','ck_users_work_rate_source',SOURCE_OLD),
        ('merchants','ck_merchant_country_parser',MERCHANT_OLD),
        ('merchants','ck_merchant_currency',CURRENCY_OLD),
        ('bank_directory','ck_bank_directory_country',"country IN ('AM','GE')"),
        ('bank_directory','ck_bank_directory_type',"institution_type IN ('bank','microbank')"),
        ('country_tariff_plans','ck_country_tariff_direction',"direction IN ('kz-ge','ru')"),
        ('country_tariff_plans','ck_country_tariff_dimensions',TARIFF_OLD),
        ('trader_wallets','ck_trader_wallet_usdt',"currency='USDT'"),
        ('trader_wallet_ledger_entries','ck_trader_wallet_ledger_usdt',"currency='USDT'"),
    ):
        replace_check(table,name,expression)
