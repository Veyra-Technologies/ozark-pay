"""TJS evidence adapter for the existing confirmed-advance FIFO lifecycle."""
from alembic import op
import sqlalchemy as sa

revision = '0045_tj_rolling_admission'
down_revision = '0044_tj_settlement'
branch_labels = None
depends_on = None

TABLES = ('merchant_rolling_accounts', 'merchant_rolling_transfers',
          'merchant_rolling_allocations', 'merchant_rolling_transfer_consumptions',
          'merchant_rolling_ledger_entries')
WIDEN = {TABLES[0]: ('principal_usdt','recovered_usdt','outstanding_usdt'),
         TABLES[1]: ('amount_usdt','recovered_usdt','remaining_usdt'),
         TABLES[2]: ('merchant_net_usdt','rolling_applied_usdt'),
         TABLES[3]: ('amount_usdt',), TABLES[4]: ('amount_usdt',)}
RAPIRA = ('rapira_rate_rub','rapira_rate_symbol','rapira_rate_side','rapira_rate_source',
          'rapira_rate_field','rapira_rate_updated_at','rapira_fetched_at','rapira_freshness_basis')
ALLOCATION_LOCAL = ('gross_local','merchant_fee_local','merchant_net_local',
                    'rolling_applied_local','settle_credited_local')
TRANSFORMS = {
    'check_tj_operation_accounting(uuid)': ('      SELECT coalesce(sum(amount),0) INTO margin',
        '      credited := credited + tj_rolling_net(dep.id);\n      SELECT coalesce(sum(amount),0) INTO margin'),
    'check_tj_aggregator_accounting(uuid)': (' SELECT coalesce(sum(amount),0) INTO margin',
        ' cr := cr + tj_rolling_net(operation);\n SELECT coalesce(sum(amount),0) INTO margin'),
}


def transform(reverse=False):
    for signature,(old,new) in TRANSFORMS.items():
        definition = op.get_bind().scalar(sa.text('SELECT pg_get_functiondef(CAST(:name AS regprocedure))'), {'name':signature})
        source,target = (new,old) if reverse else (old,new)
        if definition.count(source) != 1:
            raise RuntimeError('Unexpected independent accounting guard: '+signature)
        op.execute(definition.replace(source,target,1))


def upgrade():
    for table,columns in WIDEN.items():
        for column in columns:
            op.alter_column(table,column,type_=sa.Numeric(24,8))
    for table in TABLES[2:]:
        op.add_column(table,sa.Column('local_currency',sa.String(3),nullable=False,server_default='RUB'))
        op.add_column(table,sa.Column('rate_local',sa.Numeric(30,12)))
        if table != TABLES[2]:
            op.add_column(table,sa.Column('amount_local',sa.Numeric(18,2)))
        op.create_check_constraint('ck_'+table+'_local_currency',table,"local_currency IN ('RUB','TJS')")
    for column in ALLOCATION_LOCAL:
        op.add_column(TABLES[2],sa.Column(column,sa.Numeric(18,2)))
    op.add_column(TABLES[2],sa.Column('settle_credited_usdt',sa.Numeric(24,8)))
    op.add_column(TABLES[2],sa.Column('quote_snapshot',sa.JSON()))
    for column in RAPIRA:
        op.alter_column(TABLES[2],column,nullable=True)
    op.alter_column(TABLES[3],'rate_rub',nullable=True)
    op.create_check_constraint('ck_rolling_allocation_currency_evidence',TABLES[2],"""
    COALESCE((local_currency='RUB' AND gross_local IS NULL AND merchant_fee_local IS NULL AND merchant_net_local IS NULL
      AND rate_local IS NULL AND quote_snapshot IS NULL AND rolling_applied_local IS NULL
      AND settle_credited_local IS NULL AND settle_credited_usdt IS NULL AND rapira_rate_rub>0
      AND rapira_rate_symbol IS NOT NULL AND rapira_rate_side='ask' AND rapira_rate_source IS NOT NULL
      AND rapira_rate_field IS NOT NULL AND rapira_rate_updated_at IS NOT NULL AND rapira_fetched_at IS NOT NULL
      AND rapira_freshness_basis IS NOT NULL),false) OR COALESCE((local_currency='TJS'
      AND gross_rub=0 AND merchant_fee_rub=0 AND merchant_net_rub=0 AND rolling_applied_rub=0 AND settle_credited_rub=0
      AND rapira_rate_rub IS NULL AND rapira_rate_symbol IS NULL AND rapira_rate_source IS NULL
      AND gross_local>=10 AND merchant_fee_local>=0 AND merchant_net_local=gross_local-merchant_fee_local
      AND rate_local>0 AND rolling_applied_local>=0 AND settle_credited_local>=0 AND settle_credited_usdt>=0
      AND rolling_applied_local+settle_credited_local<=merchant_net_local
      AND rolling_applied_usdt+settle_credited_usdt<=merchant_net_usdt AND quote_snapshot IS NOT NULL),false)
    """)
    op.execute(SQL)
    transform()
    for table in TABLES:
        op.execute(f'CREATE TRIGGER tj_rolling_admission BEFORE INSERT OR UPDATE OR DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION guard_tj_rolling_admission()')
        op.execute(f'CREATE CONSTRAINT TRIGGER tj_rolling_accounting AFTER INSERT OR UPDATE ON {table} DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION check_tj_rolling_accounting()')


SQL = """
CREATE FUNCTION tj_rolling_net(operation uuid) RETURNS numeric LANGUAGE sql STABLE AS $$
 SELECT coalesce(sum(CASE WHEN entry_type='recovery' THEN amount_usdt ELSE -amount_usdt END),0)
 FROM merchant_rolling_transfer_consumptions WHERE deposit_id=operation AND local_currency='TJS'
$$;
CREATE FUNCTION guard_tj_rolling_admission() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE roll_merchant_id uuid; country_code text; d deposits; s operation_fee_snapshots; a merchant_rolling_allocations; t merchant_rolling_transfers;
BEGIN
 roll_merchant_id:=CASE WHEN TG_OP='DELETE' THEN OLD.merchant_id ELSE NEW.merchant_id END;
 SELECT working_country INTO country_code FROM merchants WHERE id=roll_merchant_id;
 IF TG_OP='UPDATE' AND OLD.merchant_id IS DISTINCT FROM NEW.merchant_id THEN RAISE EXCEPTION 'Rolling owner is immutable'; END IF;
 IF TG_OP<>'DELETE' AND TG_TABLE_NAME IN ('merchant_rolling_allocations','merchant_rolling_transfer_consumptions','merchant_rolling_ledger_entries') THEN
  IF country_code IS DISTINCT FROM 'TJ' AND NEW.local_currency<>'RUB' THEN
   RAISE EXCEPTION 'Rolling local currency does not match Merchant country';
  END IF;
 END IF;
 IF country_code='TJ' THEN
  IF NOT tj_isolated_finance_allowed() THEN RAISE EXCEPTION 'TJ Rolling production admission is closed'; END IF;
  IF TG_OP='DELETE' THEN RAISE EXCEPTION 'TJ Rolling history is immutable'; END IF;
  IF TG_TABLE_NAME='merchant_rolling_allocations' THEN
   SELECT * INTO d FROM deposits WHERE id=NEW.deposit_id;
   SELECT * INTO s FROM operation_fee_snapshots WHERE deposit_id=NEW.deposit_id;
   IF NEW.local_currency<>'TJS' OR d.currency<>'TJS' OR d.merchant_id<>roll_merchant_id OR s.id IS NULL
     OR NEW.gross_local<>d.amount OR NEW.merchant_fee_local<>s.merchant_fee_amount
     OR NEW.merchant_fee_percent_snapshot<>s.merchant_rate_percent
     OR NEW.merchant_net_usdt<>(s.financial_context->>'merchant_payable_usdt')::numeric
     OR NEW.quote_snapshot::jsonb IS DISTINCT FROM s.financial_context::jsonb->'quote'
     OR NEW.rate_local<>(s.financial_context->'quote'->>'rate')::numeric
     OR NEW.eligibility_status<>'eligible' OR NEW.rolling_eligible_at<>d.created_at
     OR NOT EXISTS(SELECT 1 FROM merchant_rolling_transfers f WHERE f.merchant_id=roll_merchant_id
       AND f.rolling_account_id=NEW.rolling_account_id AND f.sequence_no=NEW.eligible_transfer_sequence
       AND f.status='confirmed' AND f.confirmed_at<=d.created_at) THEN RAISE EXCEPTION 'TJ Rolling snapshot mismatch'; END IF;
   IF TG_OP='UPDATE' AND (to_jsonb(NEW)-ARRAY['status','release_reason','finalized_at','updated_at','rolling_applied_usdt','rolling_applied_local','settle_credited_local','settle_credited_usdt'])
     IS DISTINCT FROM (to_jsonb(OLD)-ARRAY['status','release_reason','finalized_at','updated_at','rolling_applied_usdt','rolling_applied_local','settle_credited_local','settle_credited_usdt']) THEN RAISE EXCEPTION 'TJ Rolling snapshot is immutable'; END IF;
  ELSIF TG_TABLE_NAME='merchant_rolling_transfer_consumptions' THEN
   SELECT * INTO a FROM merchant_rolling_allocations WHERE id=NEW.rolling_allocation_id;
   SELECT * INTO t FROM merchant_rolling_transfers WHERE id=NEW.rolling_transfer_id;
   IF NEW.local_currency<>'TJS' OR a.id IS NULL OR t.id IS NULL OR NEW.merchant_id<>a.merchant_id
    OR NEW.deposit_id<>a.deposit_id OR NEW.rolling_account_id<>a.rolling_account_id OR t.merchant_id<>roll_merchant_id
    OR t.rolling_account_id<>a.rolling_account_id OR t.sequence_no>a.eligible_transfer_sequence
    OR t.confirmed_at>a.rolling_eligible_at OR NEW.rate_local<>a.rate_local
    OR NEW.amount_rub<>0 OR NEW.rate_rub IS NOT NULL OR NEW.amount_local<0 THEN RAISE EXCEPTION 'TJ Rolling consumption scope mismatch'; END IF;
  ELSIF TG_TABLE_NAME='merchant_rolling_ledger_entries' THEN
   IF NEW.deposit_id IS NOT NULL THEN
    IF NEW.local_currency<>'TJS' OR NEW.amount_rub IS NOT NULL OR NEW.rate_rub IS NOT NULL
    OR NOT EXISTS(SELECT 1 FROM merchant_rolling_allocations x WHERE x.deposit_id=NEW.deposit_id
     AND x.merchant_id=roll_merchant_id AND x.rolling_account_id=NEW.rolling_account_id) THEN RAISE EXCEPTION 'TJ Rolling journal scope mismatch'; END IF;
   END IF;
  END IF;
 END IF;
 IF TG_OP='DELETE' THEN RETURN OLD; ELSE RETURN NEW; END IF;
END $$;
CREATE FUNCTION check_tj_rolling_accounting() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE roll_merchant_id uuid; a merchant_rolling_accounts; p numeric; r numeric; o numeric; n numeric; x merchant_rolling_allocations;
BEGIN
 roll_merchant_id:=NEW.merchant_id;
 IF NOT EXISTS(SELECT 1 FROM merchants WHERE id=roll_merchant_id AND working_country='TJ') THEN RETURN NEW; END IF;
 SELECT * INTO a FROM merchant_rolling_accounts WHERE merchant_id=roll_merchant_id;
 SELECT coalesce(sum(amount_usdt),0),coalesce(sum(recovered_usdt),0),coalesce(sum(remaining_usdt),0)
  INTO p,r,o FROM merchant_rolling_transfers WHERE merchant_id=roll_merchant_id AND status='confirmed';
 SELECT coalesce(sum(CASE WHEN entry_type='recovery' THEN amount_usdt ELSE -amount_usdt END),0)
  INTO n FROM merchant_rolling_transfer_consumptions WHERE merchant_id=roll_merchant_id;
 IF (a.id IS NULL AND (p<>0 OR n<>0)) OR (a.id IS NOT NULL AND
  (a.principal_usdt<>p OR a.recovered_usdt<>r OR a.outstanding_usdt<>o OR p<>r+o OR r<>n)) THEN RAISE EXCEPTION 'TJ Rolling account journal mismatch'; END IF;
 IF EXISTS(SELECT 1 FROM merchant_rolling_transfers f WHERE f.merchant_id=roll_merchant_id AND f.status='confirmed'
  AND (f.recovered_usdt<>(SELECT coalesce(sum(CASE WHEN c.entry_type='recovery' THEN c.amount_usdt ELSE -c.amount_usdt END),0)
      FROM merchant_rolling_transfer_consumptions c WHERE c.rolling_transfer_id=f.id)
   OR (SELECT count(*) FROM merchant_rolling_ledger_entries l WHERE l.rolling_transfer_id=f.id AND l.entry_type IN ('funding','topup') AND l.amount_usdt=f.amount_usdt)<>1)) THEN RAISE EXCEPTION 'TJ Rolling transfer journal mismatch'; END IF;
 FOR x IN SELECT * FROM merchant_rolling_allocations WHERE merchant_id=roll_merchant_id LOOP
  n:=tj_rolling_net(x.deposit_id);
  IF (x.status='paid' AND (n<>x.rolling_applied_usdt OR x.rolling_applied_usdt+x.settle_credited_usdt<>x.merchant_net_usdt
    OR x.rolling_applied_local+x.settle_credited_local<>x.merchant_net_local)) OR
   (x.status<>'paid' AND n<>0) THEN RAISE EXCEPTION 'TJ Rolling allocation split mismatch'; END IF;
  PERFORM check_tj_operation_accounting(x.deposit_id);
 END LOOP;
 RETURN NEW;
END $$;
"""


def downgrade():
    if op.get_bind().scalar(sa.text("SELECT EXISTS(SELECT 1 FROM merchant_rolling_transfers t JOIN merchants m ON m.id=t.merchant_id WHERE m.working_country='TJ') OR EXISTS(SELECT 1 FROM merchant_rolling_accounts a JOIN merchants m ON m.id=a.merchant_id WHERE m.working_country='TJ')")):
        raise RuntimeError('TJ Rolling history exists; retain evidence with a compatible forward rollback.')
    transform(reverse=True)
    for table in TABLES:
        op.execute(f'DROP TRIGGER tj_rolling_accounting ON {table}')
        op.execute(f'DROP TRIGGER tj_rolling_admission ON {table}')
    op.execute('DROP FUNCTION check_tj_rolling_accounting()')
    op.execute('DROP FUNCTION guard_tj_rolling_admission()')
    op.execute('DROP FUNCTION tj_rolling_net(uuid)')
    op.drop_constraint('ck_rolling_allocation_currency_evidence',TABLES[2],type_='check')
    for column in RAPIRA:
        op.alter_column(TABLES[2],column,nullable=False)
    op.alter_column(TABLES[3],'rate_rub',nullable=False)
    for column in (*ALLOCATION_LOCAL,'settle_credited_usdt','quote_snapshot'):
        op.drop_column(TABLES[2],column)
    for table in TABLES[2:]:
        op.drop_constraint('ck_'+table+'_local_currency',table,type_='check')
        for column in ('local_currency','rate_local') + (() if table==TABLES[2] else ('amount_local',)):
            op.drop_column(table,column)
    for table,columns in WIDEN.items():
        for column in columns:
            if op.get_bind().scalar(sa.text(f'SELECT EXISTS(SELECT 1 FROM {table} WHERE {column}<>round({column},6))')):
                raise RuntimeError('Eight-place history cannot be narrowed without loss')
            op.alter_column(table,column,type_=sa.Numeric(24,6))
