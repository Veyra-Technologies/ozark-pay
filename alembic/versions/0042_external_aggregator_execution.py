"""Independent external executor, versioned credentials and durable delivery.

No legacy accounts gain execution privileges; no balances or keys are rewritten.
"""
import sqlalchemy as sa
from alembic import op

revision = '0042_external_aggregator'
down_revision = '0041_tj_history_references'
branch_labels = None
depends_on = None

OLD_TARIFF_CHECKS = {'ck_country_tariff_party': "party IN ('merchant','trader')", 'ck_country_tariff_dimensions': "(direction='kz-ge' AND ((basis='base_usdt' AND currency='USDT' AND ((party='merchant' AND country='KZ') OR (party='trader' AND country='GE'))) OR (basis='local_amount' AND ((party='merchant' AND country='KZ' AND currency='KZT') OR (party='trader' AND country='GE' AND currency='GEL'))))) OR (direction='ru' AND party='merchant' AND country='RU' AND currency='RUB' AND basis='local_amount') OR (direction='tj-tj' AND country='TJ' AND currency='TJS' AND basis='local_amount' AND party IN ('merchant','trader'))"}


def base_columns():
    return [sa.Column('id', sa.UUID(), primary_key=True),
            sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now())]


def reference(name, table, **kwargs):
    return sa.Column(name, sa.UUID(), sa.ForeignKey(table+'.id', ondelete='RESTRICT'), **kwargs)


TRANSFORMS = {
    'guard_payment_jurisdiction()': (
        "NOT EXISTS(SELECT 1 FROM requisites r JOIN users u ON u.id=r.trader_id WHERE r.id=NEW.requisites_id\n            AND r.country='TJ' AND r.currency='TJS' AND r.method='c2c' AND u.working_country='TJ' AND u.role='trader')",
        "NOT ((NEW.financial_executor_type IS NULL AND EXISTS(SELECT 1 FROM requisites r JOIN users u ON u.id=r.trader_id WHERE r.id=NEW.requisites_id AND r.country='TJ' AND r.currency='TJS' AND r.method='c2c' AND u.working_country='TJ' AND u.role='trader')) OR (COALESCE(NEW.financial_executor_type='aggregator',false) AND NEW.requisites_id IS NULL AND EXISTS(SELECT 1 FROM aggregator_accounts a JOIN aggregator_integrations i ON i.aggregator_id=a.id WHERE a.id=NEW.financial_executor_id AND a.working_country='TJ' AND a.working_currency='TJS')))"),
    'validate_tj_financial_snapshot()': (
        '        context := NEW.financial_context::jsonb;',
        "        IF NEW.executor_type='aggregator' THEN PERFORM validate_tj_aggregator_snapshot(NEW); RETURN NEW; END IF;\n        context := NEW.financial_context::jsonb;"),
    'check_tj_operation_accounting(uuid)': (
        '      context := snap.financial_context::jsonb;',
        "      IF snap.executor_type='aggregator' THEN PERFORM check_tj_aggregator_accounting(operation); RETURN; END IF;\n      context := snap.financial_context::jsonb;"),
}


def transform_guards(reverse=False):
    for name, (old, new) in TRANSFORMS.items():
        definition = op.get_bind().scalar(sa.text('SELECT pg_get_functiondef(CAST(:name AS regprocedure))'), {'name': name})
        source, target = (new, old) if reverse else (old, new)
        if definition.count(source) != 1:
            raise RuntimeError('Unexpected financial guard: '+name)
        op.execute(definition.replace(source, target, 1))


def upgrade():
    op.add_column('deposits', sa.Column('financial_executor_type', sa.String(32), nullable=True))
    op.add_column('deposits', reference('financial_executor_id', 'aggregator_accounts', nullable=True))
    op.create_check_constraint('ck_deposit_external_executor', 'deposits', "(financial_executor_type IS NULL AND financial_executor_id IS NULL) OR COALESCE(financial_executor_type='aggregator' AND financial_executor_id IS NOT NULL AND currency='TJS' AND requisites_id IS NULL,false)")
    op.create_table('aggregator_integrations', *base_columns(), reference('aggregator_id', 'aggregator_accounts', nullable=False, unique=True),
        sa.Column('active_version', sa.Integer(), nullable=False), sa.Column('status', sa.String(32), nullable=False),
        sa.Column('routing_enabled', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('last_checked_at', sa.DateTime(timezone=True)), sa.Column('last_error', sa.String(120)),
        sa.Column('last_webhook_at', sa.DateTime(timezone=True)),
        sa.CheckConstraint("active_version>0 AND status IN ('configured','awaiting_verification','connected','suspended','connection_error')", name='ck_aggregator_integration_state'))
    op.create_table('aggregator_integration_versions', *base_columns(), reference('integration_id', 'aggregator_integrations', nullable=False),
        sa.Column('version', sa.Integer(), nullable=False), sa.Column('country', sa.String(2), nullable=False),
        sa.Column('currency', sa.String(3), nullable=False), sa.Column('base_url', sa.String(500), nullable=False),
        sa.Column('adapter_id', sa.String(80), nullable=False), sa.Column('api_version', sa.String(80), nullable=False),
        sa.Column('environment', sa.String(16), nullable=False), sa.Column('credentials_encrypted', sa.String(), nullable=False),
        sa.Column('merchant_ids', sa.JSON(), nullable=False), sa.Column('allowed_methods', sa.JSON(), nullable=False),
        sa.Column('allowed_ips', sa.JSON(), nullable=False), reference('created_by', 'users', nullable=False),
        sa.Column('retired_at', sa.DateTime(timezone=True)),
        sa.UniqueConstraint('integration_id', 'version', name='uq_aggregator_integration_version'),
        sa.CheckConstraint("country='TJ' AND currency='TJS' AND environment IN ('sandbox','production')", name='ck_aggregator_integration_scope'))
    op.create_table('aggregator_wallets', *base_columns(), reference('aggregator_id', 'aggregator_accounts', nullable=False),
        sa.Column('currency', sa.String(4), nullable=False), sa.Column('balance', sa.Numeric(24,8), nullable=False),
        sa.Column('hold', sa.Numeric(24,8), nullable=False),
        sa.UniqueConstraint('aggregator_id', 'currency', name='uq_aggregator_wallet_currency'),
        sa.CheckConstraint("currency IN ('TJS','USDT') AND balance>=0 AND hold>=0 AND hold<=balance", name='ck_aggregator_wallet_money'))
    op.create_table('aggregator_wallet_entries', *base_columns(), reference('wallet_id', 'aggregator_wallets', nullable=False),
        reference('aggregator_id', 'aggregator_accounts', nullable=False), reference('operation_id', 'deposits'),
        sa.Column('entry_type', sa.String(32), nullable=False), sa.Column('amount', sa.Numeric(24,8), nullable=False),
        sa.Column('currency', sa.String(4), nullable=False), sa.Column('balance_after', sa.Numeric(24,8), nullable=False),
        sa.Column('hold_after', sa.Numeric(24,8), nullable=False), sa.Column('idempotency_key', sa.String(180), nullable=False, unique=True),
        sa.Column('description', sa.String(500), nullable=False),
        sa.UniqueConstraint('operation_id', 'entry_type', name='uq_aggregator_operation_movement'),
        sa.CheckConstraint("currency='USDT' AND amount>0 AND entry_type IN ('credit','hold','release','debit','debit_available')", name='ck_aggregator_entry_money'))
    op.create_table('aggregator_executions', *base_columns(), reference('deposit_id', 'deposits', nullable=False, unique=True),
        reference('aggregator_id', 'aggregator_accounts', nullable=False), reference('integration_version_id', 'aggregator_integration_versions', nullable=False),
        sa.Column('external_id', sa.String(80), nullable=False, unique=True), sa.Column('provider_order_id', sa.String(160)),
        sa.Column('state', sa.String(32), nullable=False), sa.Column('attempts', sa.Integer(), nullable=False),
        sa.Column('lease_token', sa.UUID()), sa.Column('lease_until', sa.DateTime(timezone=True)),
        sa.Column('next_attempt_at', sa.DateTime(timezone=True)), sa.Column('last_error', sa.String(120)),
        sa.Column('requisites_encrypted', sa.String()),
        sa.UniqueConstraint('integration_version_id', 'provider_order_id', name='uq_aggregator_provider_order'))
    op.create_index('ix_aggregator_outbox_due', 'aggregator_executions', ['state','next_attempt_at','created_at'])
    op.create_table('aggregator_provider_events', *base_columns(), reference('integration_version_id', 'aggregator_integration_versions', nullable=False),
        reference('execution_id', 'aggregator_executions', nullable=False), sa.Column('event_id', sa.String(160), nullable=False),
        sa.Column('payload_hash', sa.String(64), nullable=False), sa.Column('normalized_status', sa.String(32), nullable=False),
        sa.Column('no_credit_confirmed', sa.Boolean(), nullable=False), sa.Column('status', sa.String(32), nullable=False),
        sa.UniqueConstraint('integration_version_id', 'event_id', name='uq_aggregator_provider_event'))
    for constraint, original in OLD_TARIFF_CHECKS.items():
        expanded = original.replace("party IN ('merchant','trader')", "party IN ('merchant','trader','aggregator')")
        op.drop_constraint(constraint, 'country_tariff_plans', type_='check')
        op.create_check_constraint(constraint, 'country_tariff_plans', expanded)
    op.execute(SNAPSHOT_SQL)
    op.execute(ACCOUNTING_SQL)
    op.execute(SCOPE_SQL)
    transform_guards()
    for table in ('aggregator_integrations','aggregator_wallet_entries', 'aggregator_executions', 'aggregator_provider_events', 'aggregator_integration_versions', 'aggregator_wallets'):
        op.execute(f'CREATE TRIGGER {table}_scope BEFORE INSERT OR UPDATE OR DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION guard_external_aggregator_scope()')
    op.execute('CREATE TRIGGER deposit_external_identity BEFORE UPDATE ON deposits FOR EACH ROW EXECUTE FUNCTION protect_external_executor_identity()')
    op.execute('CREATE CONSTRAINT TRIGGER aggregator_wallet_accounting AFTER INSERT OR UPDATE ON aggregator_wallets DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION check_aggregator_wallet_accounting()')
    op.execute('CREATE CONSTRAINT TRIGGER aggregator_entry_accounting AFTER INSERT ON aggregator_wallet_entries DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION check_aggregator_wallet_accounting()')


SNAPSHOT_SQL = """CREATE FUNCTION validate_tj_aggregator_snapshot(s operation_fee_snapshots) RETURNS void LANGUAGE plpgsql AS $$
DECLARE d deposits; c jsonb; observed jsonb;
BEGIN
 SELECT * INTO d FROM deposits WHERE id=s.deposit_id; c:=s.financial_context::jsonb;
 IF NOT tj_isolated_finance_allowed() OR d.currency<>'TJS' OR s.currency<>'TJS' OR d.merchant_id<>s.merchant_id OR
   d.financial_executor_type IS DISTINCT FROM 'aggregator' OR d.financial_executor_id IS DISTINCT FROM s.executor_id OR d.requisites_id IS NOT NULL OR
   c IS NULL OR c->>'country' IS DISTINCT FROM 'TJ' OR c->>'local_currency' IS DISTINCT FROM 'TJS' OR c->>'accounting_currency' IS DISTINCT FROM 'USDT' OR
   c->>'executor_type' IS DISTINCT FROM 'aggregator' OR c->>'executor_id' IS DISTINCT FROM s.executor_id::text OR
   c->'quote'->>'synthetic_qa' IS DISTINCT FROM 'true' OR c->'quote'->>'source' IS DISTINCT FROM 'bybit_p2p_tjs_green_avg3' OR
   NOT (c ?& ARRAY['executor_plan','merchant_plan','amount_tjs','base_usdt','debit_usdt','reserve_usdt','merchant_payable_usdt','platform_margin_usdt','integration_version_id','external_id']) THEN
   RAISE EXCEPTION 'External aggregator snapshot scope mismatch'; END IF;
 IF EXISTS(SELECT 1 FROM unnest(ARRAY['amount_tjs','base_usdt','debit_usdt','reserve_usdt','merchant_payable_usdt','platform_margin_usdt']) AS k
   WHERE coalesce(c->>k,'') !~ '^[0-9]+([.][0-9]+)?$') OR coalesce(c->'quote'->>'rate','') !~ '^[0-9]+([.][0-9]+)?$' THEN
   RAISE EXCEPTION 'External aggregator requires finite fixed amounts'; END IF;
 SELECT snapshot INTO observed FROM rate_observations WHERE id=(c->'quote'->>'observation_id')::uuid;
 IF observed IS NULL OR observed IS DISTINCT FROM c->'quote' OR (c->'quote'->>'rate')::numeric<=0 OR
   (c->>'amount_tjs')::numeric<>d.amount OR s.calculation_base_amount<>d.amount OR
   (c->>'base_usdt')::numeric<>round(d.amount/(c->'quote'->>'rate')::numeric,8) OR
   s.executor_fee_amount<>round(d.amount*s.executor_rate_percent/100,2) OR s.merchant_fee_amount<>round(d.amount*s.merchant_rate_percent/100,2) OR
   (c->>'debit_usdt')::numeric<>round((d.amount-s.executor_fee_amount)/(c->'quote'->>'rate')::numeric,8) OR
   (c->>'merchant_payable_usdt')::numeric<>round((d.amount-s.merchant_fee_amount)/(c->'quote'->>'rate')::numeric,8) OR
   (c->>'reserve_usdt')::numeric<>(c->>'debit_usdt')::numeric OR (c->>'reserve_usdt')::numeric<=0 OR
   (c->>'platform_margin_usdt')::numeric<0 OR (c->>'debit_usdt')::numeric<>(c->>'merchant_payable_usdt')::numeric+(c->>'platform_margin_usdt')::numeric THEN
   RAISE EXCEPTION 'External aggregator conversion mismatch'; END IF;
 IF NOT EXISTS(SELECT 1 FROM fee_rules f JOIN country_tariff_plans p ON p.id=f.country_tariff_plan_id
   WHERE f.id=s.executor_rate_rule_id AND f.entity_type='aggregator' AND f.entity_id=s.executor_id AND f.currency='TJS'
   AND p.party='aggregator' AND p.entity_id=s.executor_id AND p.country='TJ' AND p.currency='TJS' AND p.direction='tj-tj'
   AND p.id=(c->'executor_plan'->>'id')::uuid AND f.rate_percent=s.executor_rate_percent) OR
   NOT EXISTS(SELECT 1 FROM fee_rules f JOIN country_tariff_plans p ON p.id=f.country_tariff_plan_id
   WHERE f.id=s.merchant_rate_rule_id AND f.entity_type='merchant' AND f.entity_id=s.merchant_id AND f.currency='TJS'
   AND p.party='merchant' AND p.entity_id=s.merchant_id AND p.country='TJ' AND p.id=(c->'merchant_plan'->>'id')::uuid
   AND f.rate_percent=s.merchant_rate_percent) OR NOT EXISTS(
   SELECT 1 FROM aggregator_integration_versions v JOIN aggregator_integrations i ON i.id=v.integration_id
   WHERE v.id=(c->>'integration_version_id')::uuid AND i.aggregator_id=s.executor_id AND v.country='TJ' AND v.currency='TJS') THEN
   RAISE EXCEPTION 'External aggregator tariff/integration scope mismatch'; END IF;
END $$"""

ACCOUNTING_SQL = """CREATE FUNCTION check_tj_aggregator_accounting(operation uuid) RETURNS void LANGUAGE plpgsql AS $$
DECLARE d deposits; s operation_fee_snapshots; c jsonb; h numeric; deb numeric; rel numeric; ret numeric; cr numeric; margin numeric; consumed numeric;
BEGIN
 SELECT * INTO d FROM deposits WHERE id=operation; SELECT * INTO s FROM operation_fee_snapshots WHERE deposit_id=operation;
 PERFORM validate_tj_aggregator_snapshot(s); c:=s.financial_context::jsonb;
 IF NOT EXISTS(SELECT 1 FROM aggregator_executions e WHERE e.deposit_id=operation AND e.aggregator_id=s.executor_id
   AND e.integration_version_id=(c->>'integration_version_id')::uuid AND e.external_id=c->>'external_id') OR
   EXISTS(SELECT 1 FROM trader_wallet_ledger_entries WHERE operation_id=operation) THEN RAISE EXCEPTION 'Exactly one external executor is required'; END IF;
 SELECT coalesce(sum(amount) FILTER(WHERE entry_type='hold'),0),coalesce(sum(amount) FILTER(WHERE entry_type IN ('debit','debit_available')),0),
   coalesce(sum(amount) FILTER(WHERE entry_type='release'),0),coalesce(sum(amount) FILTER(WHERE entry_type='credit'),0),
   coalesce(sum(amount) FILTER(WHERE entry_type='debit'),0) INTO h,deb,rel,ret,consumed
   FROM aggregator_wallet_entries WHERE operation_id=operation AND aggregator_id=s.executor_id AND currency='USDT';
 SELECT coalesce(sum(CASE WHEN entry_type='credit' THEN amount WHEN entry_type='local_refund' THEN -amount ELSE 0 END),0)
   INTO cr FROM ledger_entries WHERE operation_id=operation AND merchant_id=d.merchant_id AND currency='USDT';
 SELECT coalesce(sum(amount),0) INTO margin FROM platform_ledger_entries WHERE operation_id=operation
   AND merchant_id=d.merchant_id AND currency='USDT' AND entry_type IN ('platform_income','local_refund');
 IF h<>(c->>'reserve_usdt')::numeric OR rel NOT IN (0,h) OR consumed+rel>h THEN RAISE EXCEPTION 'Aggregator reserve mismatch'; END IF;
 IF d.status IN ('paid','refunded') THEN
   IF deb<>(c->>'debit_usdt')::numeric OR h-rel-consumed<>0 THEN RAISE EXCEPTION 'Aggregator debit mismatch'; END IF;
   IF d.status='paid' AND (cr<>(c->>'merchant_payable_usdt')::numeric OR margin<>(c->>'platform_margin_usdt')::numeric OR ret<>0 OR s.settlement_status<>'settled') THEN RAISE EXCEPTION 'Aggregator paid accounting mismatch'; END IF;
   IF d.status='refunded' AND (ret<>deb OR cr<>0 OR margin<>0 OR s.settlement_status<>'reversed') THEN RAISE EXCEPTION 'Aggregator inverse mismatch'; END IF;
 ELSIF deb<>0 OR ret<>0 OR cr<>0 OR margin<>0 THEN RAISE EXCEPTION 'Unpaid aggregator has settled money'; END IF;
 IF d.status IN ('failed','cancelled','expired') AND rel<>h THEN RAISE EXCEPTION 'Aggregator cancellation reserve mismatch'; END IF;
END $$;
CREATE FUNCTION check_aggregator_wallet_accounting() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE w aggregator_wallets; identity uuid; expected_balance numeric; expected_hold numeric;
BEGIN
 IF TG_TABLE_NAME='aggregator_wallets' THEN identity:=NEW.id; ELSE identity:=NEW.wallet_id; END IF;
 SELECT * INTO w FROM aggregator_wallets WHERE id=identity;
 SELECT coalesce(sum(CASE WHEN entry_type='credit' THEN amount WHEN entry_type IN ('debit','debit_available') THEN -amount ELSE 0 END),0),
   coalesce(sum(CASE WHEN entry_type='hold' THEN amount WHEN entry_type IN ('release','debit') THEN -amount ELSE 0 END),0)
   INTO expected_balance,expected_hold FROM aggregator_wallet_entries WHERE wallet_id=identity;
 IF w.balance<>expected_balance OR w.hold<>expected_hold THEN RAISE EXCEPTION 'Aggregator wallet differs from journal'; END IF;
 IF TG_TABLE_NAME='aggregator_wallet_entries' THEN
   IF NEW.operation_id IS NOT NULL THEN PERFORM check_tj_aggregator_accounting(NEW.operation_id); END IF;
 END IF;
 RETURN NEW;
END $$"""

SCOPE_SQL = """CREATE FUNCTION guard_external_aggregator_scope() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE w aggregator_wallets; d deposits; s operation_fee_snapshots; owner uuid;
BEGIN
 IF TG_OP='DELETE' THEN RAISE EXCEPTION 'External aggregator history cannot be deleted'; END IF;
 IF TG_TABLE_NAME='aggregator_integrations' THEN
   IF NOT EXISTS(SELECT 1 FROM aggregator_accounts WHERE id=NEW.aggregator_id AND working_country='TJ' AND working_currency='TJS') THEN RAISE EXCEPTION 'External integration requires TJ'; END IF;
   IF TG_OP='UPDATE' AND ROW(NEW.id,NEW.aggregator_id) IS DISTINCT FROM ROW(OLD.id,OLD.aggregator_id) THEN RAISE EXCEPTION 'Integration owner is immutable'; END IF;
 ELSIF TG_TABLE_NAME='aggregator_wallet_entries' THEN
   IF TG_OP<>'INSERT' THEN RAISE EXCEPTION 'Aggregator journal is append-only'; END IF;
   IF NOT tj_isolated_finance_allowed() THEN RAISE EXCEPTION 'Aggregator financial execution is QA-only'; END IF;
   SELECT * INTO w FROM aggregator_wallets WHERE id=NEW.wallet_id;
   IF w.aggregator_id IS DISTINCT FROM NEW.aggregator_id OR w.currency<>'USDT' OR NEW.currency<>'USDT' THEN RAISE EXCEPTION 'Aggregator wallet scope mismatch'; END IF;
   IF NEW.operation_id IS NOT NULL THEN
     SELECT * INTO s FROM operation_fee_snapshots WHERE deposit_id=NEW.operation_id;
     IF s.executor_type IS DISTINCT FROM 'aggregator' OR s.executor_id IS DISTINCT FROM NEW.aggregator_id THEN RAISE EXCEPTION 'Aggregator operation owner mismatch'; END IF;
   ELSIF NEW.entry_type<>'credit' THEN RAISE EXCEPTION 'Unlinked movement is not an opening credit'; END IF;
 ELSIF TG_TABLE_NAME='aggregator_wallets' THEN
   IF NOT EXISTS(SELECT 1 FROM aggregator_accounts WHERE id=NEW.aggregator_id AND working_country='TJ' AND working_currency='TJS') THEN RAISE EXCEPTION 'Aggregator wallet country mismatch'; END IF;
   IF TG_OP='UPDATE' AND ROW(NEW.id,NEW.aggregator_id,NEW.currency) IS DISTINCT FROM ROW(OLD.id,OLD.aggregator_id,OLD.currency) THEN RAISE EXCEPTION 'Aggregator wallet identity is immutable'; END IF;
 ELSIF TG_TABLE_NAME='aggregator_executions' THEN
   SELECT * INTO d FROM deposits WHERE id=NEW.deposit_id;
   SELECT i.aggregator_id INTO owner FROM aggregator_integrations i JOIN aggregator_integration_versions v ON v.integration_id=i.id WHERE v.id=NEW.integration_version_id;
   IF d.financial_executor_type IS DISTINCT FROM 'aggregator' OR d.financial_executor_id IS DISTINCT FROM NEW.aggregator_id OR d.requisites_id IS NOT NULL OR owner IS DISTINCT FROM NEW.aggregator_id THEN RAISE EXCEPTION 'External order owner mismatch'; END IF;
   IF TG_OP='UPDATE' AND (ROW(NEW.id,NEW.deposit_id,NEW.aggregator_id,NEW.integration_version_id,NEW.external_id) IS DISTINCT FROM ROW(OLD.id,OLD.deposit_id,OLD.aggregator_id,OLD.integration_version_id,OLD.external_id) OR
     (OLD.provider_order_id IS NOT NULL AND NEW.provider_order_id IS DISTINCT FROM OLD.provider_order_id) OR
     (OLD.requisites_encrypted IS NOT NULL AND NEW.requisites_encrypted IS DISTINCT FROM OLD.requisites_encrypted)) THEN RAISE EXCEPTION 'External accepted order is immutable'; END IF;
 ELSIF TG_TABLE_NAME='aggregator_integration_versions' THEN
   IF NOT EXISTS(SELECT 1 FROM aggregator_integrations i JOIN aggregator_accounts a ON a.id=i.aggregator_id WHERE i.id=NEW.integration_id AND a.working_country='TJ') THEN RAISE EXCEPTION 'Integration country mismatch'; END IF;
   IF TG_OP='UPDATE' AND (to_jsonb(NEW)-ARRAY['retired_at','updated_at']) IS DISTINCT FROM (to_jsonb(OLD)-ARRAY['retired_at','updated_at']) THEN RAISE EXCEPTION 'Integration versions are immutable'; END IF;
   IF TG_OP='UPDATE' AND OLD.retired_at IS NOT NULL AND NEW.retired_at IS DISTINCT FROM OLD.retired_at THEN RAISE EXCEPTION 'Retired credentials cannot be reactivated'; END IF;
 ELSIF TG_TABLE_NAME='aggregator_provider_events' THEN
   IF NOT EXISTS(SELECT 1 FROM aggregator_executions WHERE id=NEW.execution_id AND integration_version_id=NEW.integration_version_id) THEN RAISE EXCEPTION 'Provider event scope mismatch'; END IF;
   IF TG_OP='UPDATE' AND (to_jsonb(NEW)-ARRAY['status','updated_at']) IS DISTINCT FROM (to_jsonb(OLD)-ARRAY['status','updated_at']) THEN RAISE EXCEPTION 'Provider event identity is immutable'; END IF;
 END IF;
 RETURN NEW;
END $$;
CREATE FUNCTION protect_external_executor_identity() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF OLD.currency='TJS' AND ROW(NEW.financial_executor_type,NEW.financial_executor_id) IS DISTINCT FROM ROW(OLD.financial_executor_type,OLD.financial_executor_id) THEN RAISE EXCEPTION 'Financial executor is immutable'; END IF;
 RETURN NEW;
END $$"""


def downgrade():
    op.execute('LOCK TABLE deposits,aggregator_executions,aggregator_wallets,aggregator_wallet_entries,aggregator_integration_versions IN ACCESS EXCLUSIVE MODE')
    if op.get_bind().scalar(sa.text("SELECT EXISTS(SELECT 1 FROM aggregator_integrations) OR EXISTS(SELECT 1 FROM aggregator_wallet_entries) OR EXISTS(SELECT 1 FROM country_tariff_plans WHERE party='aggregator') OR EXISTS(SELECT 1 FROM deposits WHERE financial_executor_type IS NOT NULL)")):
        raise RuntimeError('External aggregator history exists; use a forward-compatible rollback')
    transform_guards(reverse=True)
    op.execute('DROP TRIGGER deposit_external_identity ON deposits')
    op.execute('DROP FUNCTION protect_external_executor_identity()')
    op.execute('DROP TRIGGER aggregator_wallet_accounting ON aggregator_wallets')
    op.execute('DROP TRIGGER aggregator_entry_accounting ON aggregator_wallet_entries')
    for table in ('aggregator_integrations','aggregator_wallet_entries','aggregator_executions','aggregator_provider_events','aggregator_integration_versions','aggregator_wallets'):
        op.execute(f'DROP TRIGGER IF EXISTS {table}_scope ON {table}')
    op.execute('DROP FUNCTION guard_external_aggregator_scope()')
    op.execute('DROP FUNCTION check_aggregator_wallet_accounting()')
    op.execute('DROP FUNCTION check_tj_aggregator_accounting(uuid)')
    op.execute('DROP FUNCTION validate_tj_aggregator_snapshot(operation_fee_snapshots)')
    for constraint, original in OLD_TARIFF_CHECKS.items():
        op.drop_constraint(constraint,'country_tariff_plans',type_='check')
        op.create_check_constraint(constraint,'country_tariff_plans',original)
    for table in ('aggregator_provider_events','aggregator_executions','aggregator_wallet_entries','aggregator_wallets','aggregator_integration_versions','aggregator_integrations'):
        op.drop_table(table)
    op.drop_constraint('ck_deposit_external_executor','deposits',type_='check')
    op.drop_column('deposits','financial_executor_id')
    op.drop_column('deposits','financial_executor_type')
