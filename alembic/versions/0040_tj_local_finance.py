"""Currency policy for local QA financial acceptance; production remains closed."""
import sqlalchemy as sa
from alembic import op

revision = '0040_tj_local_finance'
down_revision = '0039_tj_bybit_individual'
branch_labels = None
depends_on = None

PREVIOUS_BLOCK = "\n      IF TG_TABLE_NAME='deposits' AND (NEW.currency='TJS' OR EXISTS (SELECT 1 FROM merchants WHERE id=NEW.merchant_id AND working_country='TJ')) THEN RAISE EXCEPTION 'TJ financial contract is not approved'; END IF;\n"
QA_BLOCK = """
      IF TG_TABLE_NAME='deposits' AND (NEW.currency='TJS' OR EXISTS (SELECT 1 FROM merchants WHERE id=NEW.merchant_id AND working_country='TJ')) THEN
        IF NOT tj_isolated_finance_allowed() THEN RAISE EXCEPTION 'TJ financial contract is not approved'; END IF;
        IF NEW.currency <> 'TJS' OR NEW.method <> 'c2c' OR NEW.amount < 10 OR
          NOT EXISTS(SELECT 1 FROM merchants WHERE id=NEW.merchant_id AND working_country='TJ' AND working_currency='TJS') OR
          NOT EXISTS(SELECT 1 FROM requisites r JOIN users u ON u.id=r.trader_id WHERE r.id=NEW.requisites_id
            AND r.country='TJ' AND r.currency='TJS' AND r.method='c2c' AND u.working_country='TJ' AND u.role='trader') THEN
          RAISE EXCEPTION 'TJ payment owner/currency/requisite mismatch'; END IF;
      END IF;
"""


def upgrade():
    op.add_column('fee_rules', sa.Column('country_tariff_plan_id', sa.UUID(),
        sa.ForeignKey('country_tariff_plans.id', ondelete='RESTRICT'), nullable=True))
    op.add_column('operation_fee_snapshots', sa.Column('financial_context', sa.JSON(), nullable=True))
    op.execute("""CREATE FUNCTION tj_isolated_finance_allowed() RETURNS boolean LANGUAGE sql STABLE AS $$
      SELECT COALESCE(left(current_database(),15)='ozark_tj_wave3_' AND current_user='ozark_tj_test'
        AND current_setting('ozark.tj_isolated_qa',true)='on',false)
    $$""")
    bind = op.get_bind()
    definition = bind.scalar(sa.text("SELECT pg_get_functiondef('guard_payment_jurisdiction()'::regprocedure)"))
    if PREVIOUS_BLOCK not in definition:
        raise RuntimeError('Unexpected TJ admission guard; review migration history')
    op.execute(definition.replace(PREVIOUS_BLOCK, QA_BLOCK))
    op.execute("""CREATE FUNCTION validate_tj_financial_snapshot() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE dep deposits; context jsonb; observed jsonb;
    BEGIN
      SELECT * INTO dep FROM deposits WHERE id=NEW.deposit_id;
      IF dep.currency='TJS' OR NEW.currency='TJS' THEN
        context := NEW.financial_context::jsonb;
        IF NOT tj_isolated_finance_allowed() OR dep.currency <> 'TJS' OR NEW.currency <> 'TJS' OR
          dep.merchant_id IS DISTINCT FROM NEW.merchant_id OR dep.amount IS DISTINCT FROM NEW.calculation_base_amount OR
          NEW.executor_type <> 'trader' OR NOT EXISTS(SELECT 1 FROM requisites WHERE id=dep.requisites_id AND trader_id=NEW.executor_id AND country='TJ') OR
          context IS NULL OR context->>'country' IS DISTINCT FROM 'TJ' OR
          context->>'accounting_currency' IS DISTINCT FROM 'USDT' OR context->>'local_currency' IS DISTINCT FROM 'TJS' OR
          NOT (context ?& ARRAY['amount_tjs','base_usdt','reserve_usdt','debit_usdt','merchant_payable_usdt','platform_margin_usdt','merchant_plan','trader_plan']) OR
          context->'quote'->>'synthetic_qa' IS DISTINCT FROM 'true' OR
          context->'quote'->>'source' IS DISTINCT FROM 'bybit_p2p_tjs_green_avg3' THEN
          RAISE EXCEPTION 'TJ financial snapshot scope is invalid'; END IF;
        SELECT snapshot INTO observed FROM rate_observations WHERE id=(context->'quote'->>'observation_id')::uuid;
        IF observed IS NULL OR observed IS DISTINCT FROM context->'quote' OR
          (context->>'amount_tjs')::numeric <> dep.amount OR (context->>'reserve_usdt')::numeric <= 0 OR
          (context->>'debit_usdt')::numeric < (context->>'merchant_payable_usdt')::numeric OR
          (context->>'debit_usdt')::numeric <> (context->>'merchant_payable_usdt')::numeric + (context->>'platform_margin_usdt')::numeric OR
          (context->>'reserve_usdt')::numeric <> (context->>'debit_usdt')::numeric THEN
          RAISE EXCEPTION 'TJ fixed quote/accounting invariant is invalid'; END IF;
        IF NOT (context->'quote' ?& ARRAY['rate','observation_id','configuration_id','configuration_version']) OR
          (context->'quote'->>'rate')::numeric <= 0 OR
          (context->>'base_usdt')::numeric <> round(dep.amount/(context->'quote'->>'rate')::numeric,8) OR
          (context->>'debit_usdt')::numeric <> round((dep.amount-NEW.executor_fee_amount)/(context->'quote'->>'rate')::numeric,8) OR
          (context->>'merchant_payable_usdt')::numeric <> round((dep.amount-NEW.merchant_fee_amount)/(context->'quote'->>'rate')::numeric,8) OR
          NEW.merchant_fee_amount <> round(dep.amount*NEW.merchant_rate_percent/100,2) OR
          NEW.executor_fee_amount <> round(dep.amount*NEW.executor_rate_percent/100,2) OR
          NOT EXISTS(SELECT 1 FROM fee_rules f JOIN country_tariff_plans p ON p.id=f.country_tariff_plan_id
            WHERE f.id=NEW.merchant_rate_rule_id AND f.entity_id=NEW.merchant_id AND f.entity_type='merchant'
            AND f.currency='TJS' AND p.id=(context->'merchant_plan'->>'id')::uuid AND p.entity_id=NEW.merchant_id
            AND p.country='TJ' AND p.party='merchant' AND f.rate_percent=NEW.merchant_rate_percent) OR
          NOT EXISTS(SELECT 1 FROM fee_rules f JOIN country_tariff_plans p ON p.id=f.country_tariff_plan_id
            WHERE f.id=NEW.executor_rate_rule_id AND f.entity_id=NEW.executor_id AND f.entity_type='trader'
            AND f.currency='TJS' AND p.id=(context->'trader_plan'->>'id')::uuid AND p.entity_id=NEW.executor_id
            AND p.country='TJ' AND p.party='trader' AND f.rate_percent=NEW.executor_rate_percent) THEN
          RAISE EXCEPTION 'TJ tariff/conversion does not match the fixed financial context'; END IF;
      ELSIF NEW.financial_context IS NOT NULL THEN RAISE EXCEPTION 'TJ context cannot be used for another currency';
      END IF;
      RETURN NEW;
    END $$""")
    op.execute('CREATE TRIGGER tj_financial_snapshot_guard BEFORE INSERT OR UPDATE ON operation_fee_snapshots FOR EACH ROW EXECUTE FUNCTION validate_tj_financial_snapshot()')
    op.execute("""CREATE FUNCTION check_tj_operation_accounting(operation uuid) RETURNS void LANGUAGE plpgsql AS $$
    DECLARE dep deposits; snap operation_fee_snapshots; context jsonb; held numeric; debited numeric;
      released numeric; returned numeric; credited numeric; margin numeric;
    BEGIN
      SELECT * INTO dep FROM deposits WHERE id=operation;
      IF dep.id IS NULL OR dep.currency <> 'TJS' THEN RETURN; END IF;
      IF NOT tj_isolated_finance_allowed() THEN RAISE EXCEPTION 'TJ financial contract is not approved'; END IF;
      SELECT * INTO snap FROM operation_fee_snapshots WHERE deposit_id=dep.id;
      IF snap.id IS NULL THEN RAISE EXCEPTION 'TJ operation requires an immutable snapshot'; END IF;
      context := snap.financial_context::jsonb;
      SELECT coalesce(sum(amount) FILTER(WHERE entry_type='hold'),0),
        coalesce(sum(amount) FILTER(WHERE entry_type IN ('debit','debit_available')),0),
        coalesce(sum(amount) FILTER(WHERE entry_type='release'),0),
        coalesce(sum(amount) FILTER(WHERE entry_type='credit'),0)
        INTO held,debited,released,returned FROM trader_wallet_ledger_entries
        WHERE operation_id=dep.id AND trader_id=snap.executor_id AND currency='USDT';
      SELECT coalesce(sum(CASE WHEN entry_type='credit' THEN amount WHEN entry_type='local_refund' THEN -amount ELSE 0 END),0)
        INTO credited FROM ledger_entries WHERE operation_id=dep.id AND merchant_id=dep.merchant_id AND currency='USDT';
      SELECT coalesce(sum(amount),0) INTO margin FROM platform_ledger_entries WHERE operation_id=dep.id
        AND merchant_id=dep.merchant_id AND currency='USDT' AND entry_type IN ('platform_income','local_refund');
      IF held <> (context->>'reserve_usdt')::numeric THEN RAISE EXCEPTION 'TJ reserve journal mismatch'; END IF;
      IF dep.status IN ('paid','refunded') THEN
        IF debited <> (context->>'debit_usdt')::numeric THEN RAISE EXCEPTION 'TJ debit journal mismatch'; END IF;
        IF dep.status='paid' AND (credited <> (context->>'merchant_payable_usdt')::numeric OR
          margin <> (context->>'platform_margin_usdt')::numeric OR returned<>0 OR snap.settlement_status<>'settled') THEN
          RAISE EXCEPTION 'TJ paid accounting mismatch'; END IF;
        IF dep.status='refunded' AND (returned<>debited OR credited<>0 OR margin<>0 OR snap.settlement_status<>'reversed') THEN
          RAISE EXCEPTION 'TJ refund accounting mismatch'; END IF;
      ELSIF debited<>0 OR returned<>0 OR credited<>0 OR margin<>0 THEN
        RAISE EXCEPTION 'TJ unpaid operation cannot have settled money';
      END IF;
      IF dep.status IN ('failed','cancelled','expired') AND released<>held THEN RAISE EXCEPTION 'TJ cancellation reserve mismatch'; END IF;
      RETURN;
    END $$""")
    op.execute("""CREATE FUNCTION check_tj_deposit_accounting() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF TG_TABLE_NAME='deposits' THEN PERFORM check_tj_operation_accounting(NEW.id);
      ELSE PERFORM check_tj_operation_accounting(NEW.operation_id); END IF;
      RETURN NEW;
    END $$""")
    op.execute('CREATE CONSTRAINT TRIGGER tj_deposit_accounting_guard AFTER INSERT OR UPDATE ON deposits DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION check_tj_deposit_accounting()')
    op.execute("""CREATE FUNCTION guard_tj_journal_scope() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE dep deposits; executor uuid;
    BEGIN
      SELECT * INTO dep FROM deposits WHERE id=CASE WHEN TG_OP='DELETE' THEN OLD.operation_id ELSE NEW.operation_id END;
      IF dep.id IS NULL OR dep.currency <> 'TJS' THEN
        IF TG_OP='DELETE' THEN RETURN OLD; ELSE RETURN NEW; END IF;
      END IF;
      IF TG_OP <> 'INSERT' THEN RAISE EXCEPTION 'TJ financial journal is append-only'; END IF;
      IF NOT tj_isolated_finance_allowed() THEN RAISE EXCEPTION 'TJ financial contract is not approved'; END IF;
      IF TG_TABLE_NAME='trader_wallet_ledger_entries' THEN
        SELECT executor_id INTO executor FROM operation_fee_snapshots WHERE deposit_id=dep.id;
        IF NEW.currency <> 'USDT' OR NEW.trader_id IS DISTINCT FROM executor THEN RAISE EXCEPTION 'TJ trader ledger scope mismatch'; END IF;
      ELSE
        IF NEW.merchant_id IS DISTINCT FROM dep.merchant_id THEN RAISE EXCEPTION 'TJ merchant ledger scope mismatch'; END IF;
        IF TG_TABLE_NAME='ledger_entries' AND NOT ((NEW.currency='USDT' AND NEW.entry_type IN ('credit','local_refund')) OR
          (NEW.currency='TJS' AND NEW.entry_type IN ('fee','local_fee_reversal'))) THEN RAISE EXCEPTION 'TJ merchant ledger currency mismatch'; END IF;
        IF TG_TABLE_NAME='platform_ledger_entries' AND NOT ((NEW.currency='USDT' AND NEW.entry_type IN ('platform_income','local_refund')) OR
          (NEW.currency='TJS' AND NEW.entry_type='executor_fee')) THEN RAISE EXCEPTION 'TJ platform ledger currency mismatch'; END IF;
      END IF;
      RETURN NEW;
    END $$""")
    for table in ('ledger_entries','platform_ledger_entries','trader_wallet_ledger_entries'):
        op.execute(f'CREATE TRIGGER {table}_tj_scope BEFORE INSERT OR UPDATE OR DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION guard_tj_journal_scope()')
        op.execute(f'CREATE CONSTRAINT TRIGGER {table}_tj_accounting AFTER INSERT OR UPDATE ON {table} DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION check_tj_deposit_accounting()')
    op.execute("""CREATE FUNCTION guard_tj_requisite_history() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF EXISTS(SELECT 1 FROM deposits WHERE requisites_id=OLD.id AND currency='TJS') THEN
        IF ROW(NEW.trader_id,NEW.country,NEW.currency,NEW.method) IS DISTINCT FROM ROW(OLD.trader_id,OLD.country,OLD.currency,OLD.method) THEN
          RAISE EXCEPTION 'TJ requisite accounting owner is immutable'; END IF;
        IF ROW(NEW.value_encrypted,NEW.bank_id) IS DISTINCT FROM ROW(OLD.value_encrypted,OLD.bank_id) AND
          EXISTS(SELECT 1 FROM deposits WHERE requisites_id=OLD.id AND currency='TJS' AND status IN ('created','pending','appeal_opened')) THEN
          RAISE EXCEPTION 'TJ requisite has an active payment'; END IF;
      END IF;
      RETURN NEW;
    END $$""")
    op.execute('CREATE TRIGGER requisite_tj_history_guard BEFORE UPDATE ON requisites FOR EACH ROW EXECUTE FUNCTION guard_tj_requisite_history()')


def downgrade():
    bind = op.get_bind()
    op.execute('LOCK TABLE deposits,operation_fee_snapshots,fee_rules IN ACCESS EXCLUSIVE MODE')
    if bind.scalar(sa.text("SELECT count(*) FROM deposits WHERE currency='TJS'")) or bind.scalar(sa.text(
            'SELECT count(*) FROM operation_fee_snapshots WHERE financial_context IS NOT NULL')) or bind.scalar(sa.text(
            'SELECT count(*) FROM fee_rules WHERE country_tariff_plan_id IS NOT NULL')):
        raise RuntimeError('TJ financial history exists; use a forward-compatible rollback')
    definition = bind.scalar(sa.text("SELECT pg_get_functiondef('guard_payment_jurisdiction()'::regprocedure)"))
    if QA_BLOCK not in definition:
        raise RuntimeError('Unexpected TJ QA guard')
    op.execute(definition.replace(QA_BLOCK, PREVIOUS_BLOCK))
    op.execute('DROP TRIGGER requisite_tj_history_guard ON requisites')
    op.execute('DROP FUNCTION guard_tj_requisite_history()')
    for table in ('ledger_entries','platform_ledger_entries','trader_wallet_ledger_entries'):
        op.execute(f'DROP TRIGGER {table}_tj_scope ON {table}')
        op.execute(f'DROP TRIGGER {table}_tj_accounting ON {table}')
    op.execute('DROP FUNCTION guard_tj_journal_scope()')
    op.execute('DROP TRIGGER tj_deposit_accounting_guard ON deposits')
    op.execute('DROP FUNCTION check_tj_deposit_accounting()')
    op.execute('DROP FUNCTION check_tj_operation_accounting(uuid)')
    op.execute('DROP TRIGGER tj_financial_snapshot_guard ON operation_fee_snapshots')
    op.execute('DROP FUNCTION validate_tj_financial_snapshot()')
    op.execute('DROP FUNCTION tj_isolated_finance_allowed()')
    op.drop_column('operation_fee_snapshots', 'financial_context')
    op.drop_column('fee_rules', 'country_tariff_plan_id')
