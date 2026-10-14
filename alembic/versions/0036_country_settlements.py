"""Country-scoped merchant settlements; preserve all original RU snapshots."""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = '0036_country_settlements'
down_revision = '0035_merchant_cabinets'
branch_labels = None
depends_on = None

FINANCIAL = """contract_version=0 OR ((contract_version=1 AND fee_usdt=5 AND amount_usdt>0
AND total_usdt=amount_usdt+fee_usdt AND reserve_amount=payout_balance_amount+fee_balance_amount
AND balance_id IS NOT NULL AND network='TRC20'
AND ((country='RU' AND balance_currency='RUB' AND rate_rub>0
AND payout_balance_amount=amount_rub AND fee_balance_amount=fee_rub AND reserve_amount=total_debit_rub
AND amount_rub=round(amount_usdt*rate_rub,2) AND fee_rub=round(fee_usdt*rate_rub,2))
OR (country='KZ' AND balance_currency='USDT' AND payout_balance_amount=amount_usdt
AND fee_balance_amount=fee_usdt AND reserve_amount=total_usdt AND rate_rub IS NULL
AND amount_rub IS NULL AND fee_rub IS NULL AND total_debit_rub IS NULL))) IS TRUE)"""


def upgrade():
    fields = [
        ('contract_version', sa.Integer(), False, '0'), ('country', sa.String(2), False, 'RU'),
        ('balance_currency', sa.String(8), False, 'RUB'),
        ('business_id', postgresql.UUID(as_uuid=True), True, None),
        ('balance_id', postgresql.UUID(as_uuid=True), True, None),
        ('payout_balance_amount', sa.Numeric(24,8), False, '0'),
        ('fee_balance_amount', sa.Numeric(24,8), False, '0'),
        ('reserve_amount', sa.Numeric(24,8), False, '0'),
        ('total_usdt', sa.Numeric(18,2), False, '0'),
        ('approved_by_id', postgresql.UUID(as_uuid=True), True, None),
        ('approved_at', sa.DateTime(timezone=True), True, None),
        ('sent_by_id', postgresql.UUID(as_uuid=True), True, None),
        ('sent_at', sa.DateTime(timezone=True), True, None),
        ('confirmation_kind', sa.String(32), True, None),
        ('network_verified', sa.Boolean(), False, 'false'),
        ('execution_reference', sa.String(500), True, None),
        ('reconciliation_reason', sa.String(500), True, None),
        ('reconciliation_by_id', postgresql.UUID(as_uuid=True), True, None),
        ('reconciliation_at', sa.DateTime(timezone=True), True, None),
    ]
    for name, typ, nullable, default in fields:
        op.add_column('merchant_settlements', sa.Column(name, typ, nullable=nullable, server_default=default))
    op.execute("""UPDATE merchant_settlements s SET
      country=CASE WHEN m.working_country='KZ' THEN 'KZ' ELSE 'RU' END,
      business_id=m.business_id,
      balance_id=(SELECT id FROM balances b WHERE b.merchant_id=s.merchant_id AND b.currency='RUB'),
      payout_balance_amount=coalesce(s.amount_rub,0), fee_balance_amount=coalesce(s.fee_rub,0),
      reserve_amount=coalesce(s.total_debit_rub,0), total_usdt=s.amount_usdt+s.fee_usdt,
      confirmation_kind=CASE WHEN s.status='completed' THEN 'legacy_manual' ELSE NULL END
      FROM merchants m WHERE m.id=s.merchant_id""")
    op.alter_column('merchant_settlements', 'contract_version', server_default='1')
    for name in ('rate_rub','amount_rub','fee_rub','total_debit_rub'):
        op.alter_column('merchant_settlements', name, nullable=True)
    for name, target in [('business_id','business_accounts'),('balance_id','balances'),
                         ('approved_by_id','users'),('sent_by_id','users'),('reconciliation_by_id','users')]:
        op.create_foreign_key('fk_settlement_'+name, 'merchant_settlements', target, [name], ['id'],
                              ondelete='SET NULL' if target=='users' else 'RESTRICT')
    op.create_check_constraint('ck_settlement_country_currency','merchant_settlements',
                               "country IN ('RU','KZ') AND balance_currency IN ('RUB','USDT')")
    op.create_check_constraint('ck_settlement_status','merchant_settlements',
                               "status IN ('pending','approved','sent','reconciliation','completed','rejected')")
    op.create_check_constraint('ck_settlement_financial_contract','merchant_settlements',FINANCIAL)
    op.drop_index('uq_merchant_settlement_one_pending', table_name='merchant_settlements')
    op.create_index('uq_merchant_settlement_one_pending','merchant_settlements',['merchant_id'], unique=True,
                    postgresql_where=sa.text("status IN ('pending','approved','sent','reconciliation')"))
    op.create_index('uq_merchant_settlement_registered_tx','merchant_settlements',
                    ['network',sa.text('lower(tx_hash)')],unique=True,
                    postgresql_where=sa.text('tx_hash IS NOT NULL AND contract_version=1'))
    op.create_index('ix_settlement_country_status','merchant_settlements',['country','status','created_at'])
    op.execute("""
    CREATE FUNCTION protect_country_settlement() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE cabinet merchants%ROWTYPE; owner users%ROWTYPE; wallet balances%ROWTYPE;
    BEGIN
      IF TG_OP='DELETE' THEN RAISE EXCEPTION 'settlement history cannot be deleted'; END IF;
      IF TG_OP='INSERT' AND NEW.contract_version<>1 THEN
        RAISE EXCEPTION 'legacy settlement contract is migration-only';
      END IF;
      IF TG_OP='UPDATE' THEN
        IF OLD.status IN ('completed','rejected') AND to_jsonb(NEW) IS DISTINCT FROM to_jsonb(OLD) THEN
          RAISE EXCEPTION 'terminal settlement is immutable';
        END IF;
        IF ROW(NEW.merchant_id,NEW.requested_by_id,NEW.amount_usdt,NEW.fee_usdt,NEW.rate_rub,
          NEW.amount_rub,NEW.fee_rub,NEW.total_debit_rub,NEW.trc20_address,NEW.network,
          NEW.idempotency_key,NEW.rate_symbol,NEW.rate_side,NEW.rate_source,NEW.rate_provider_field,
          NEW.provider_timestamp,NEW.fetched_at,NEW.freshness_basis,NEW.metadata_json::jsonb,
          NEW.contract_version,NEW.country,NEW.balance_currency,NEW.business_id,NEW.balance_id,
          NEW.payout_balance_amount,NEW.fee_balance_amount,NEW.reserve_amount,NEW.total_usdt)
          IS DISTINCT FROM ROW(OLD.merchant_id,OLD.requested_by_id,OLD.amount_usdt,OLD.fee_usdt,OLD.rate_rub,
          OLD.amount_rub,OLD.fee_rub,OLD.total_debit_rub,OLD.trc20_address,OLD.network,
          OLD.idempotency_key,OLD.rate_symbol,OLD.rate_side,OLD.rate_source,OLD.rate_provider_field,
          OLD.provider_timestamp,OLD.fetched_at,OLD.freshness_basis,OLD.metadata_json::jsonb,
          OLD.contract_version,OLD.country,OLD.balance_currency,OLD.business_id,OLD.balance_id,
          OLD.payout_balance_amount,OLD.fee_balance_amount,OLD.reserve_amount,OLD.total_usdt) THEN
          RAISE EXCEPTION 'settlement financial snapshot is immutable';
        END IF;
        IF OLD.tx_hash IS NOT NULL AND NEW.tx_hash IS DISTINCT FROM OLD.tx_hash THEN
          RAISE EXCEPTION 'registered settlement TXID is immutable';
        END IF;
        IF NEW.status<>OLD.status AND NOT (
          (OLD.status='pending' AND NEW.status IN ('approved','rejected')) OR
          (OLD.status='approved' AND NEW.status IN ('sent','rejected')) OR
          (OLD.status='sent' AND NEW.status IN ('completed','reconciliation')) OR
          (OLD.status='reconciliation' AND NEW.status IN ('completed','rejected')) OR
          (OLD.contract_version=0 AND OLD.status='pending' AND NEW.status='completed')) THEN
          RAISE EXCEPTION 'invalid settlement state transition';
        END IF;
        IF OLD.status='reconciliation' AND NEW.status='rejected'
          AND (NEW.confirmation_kind IS DISTINCT FROM 'not_sent_verified'
               OR coalesce(length(trim(NEW.execution_reference)),0)=0) THEN
          RAISE EXCEPTION 'reconciliation evidence required before reserve release';
        END IF;
      END IF;
      SELECT * INTO cabinet FROM merchants WHERE id=NEW.merchant_id;
      IF NOT FOUND THEN RAISE EXCEPTION 'settlement cabinet not found'; END IF;
      IF NEW.contract_version=1 THEN
        IF NEW.country IS DISTINCT FROM cabinet.working_country
           OR NEW.business_id IS DISTINCT FROM cabinet.business_id
           OR NEW.requested_by_id IS DISTINCT FROM cabinet.owner_id THEN
          RAISE EXCEPTION 'settlement country/cabinet/owner mismatch';
        END IF;
        SELECT * INTO wallet FROM balances WHERE id=NEW.balance_id;
        IF NOT FOUND OR wallet.merchant_id<>NEW.merchant_id OR wallet.currency<>NEW.balance_currency THEN
          RAISE EXCEPTION 'settlement wallet mismatch';
        END IF;
        IF TG_OP='INSERT' THEN
          SELECT * INTO owner FROM users WHERE id=NEW.requested_by_id;
          IF NOT FOUND OR owner.role<>'merchant' OR NOT owner.is_active OR owner.is_locked OR owner.is_archived
             OR cabinet.is_archived OR NEW.status<>'pending' OR NEW.tx_hash IS NOT NULL THEN
            RAISE EXCEPTION 'settlement must be requested by active merchant';
          END IF;
        END IF;
        IF NEW.network_verified THEN RAISE EXCEPTION 'on-chain verification is not implemented'; END IF;
        IF NEW.status IN ('pending','approved') AND NEW.tx_hash IS NOT NULL THEN
          RAISE EXCEPTION 'not-sent settlement must not have a TXID';
        END IF;
        IF NEW.status IN ('sent','reconciliation','completed') AND
          (NEW.tx_hash IS NULL OR NEW.tx_hash !~ '^[0-9a-f]{64}$' OR NEW.sent_at IS NULL
           OR NEW.sent_by_id IS NULL OR NEW.approved_at IS NULL OR NEW.approved_by_id IS NULL) THEN
          RAISE EXCEPTION 'approved dispatch evidence required';
        END IF;
        IF NEW.status='completed' AND (NEW.confirmation_kind IS DISTINCT FROM 'administrator_confirmed'
           OR coalesce(length(trim(NEW.execution_reference)),0)=0 OR NEW.processed_at IS NULL
           OR NEW.processed_by_id IS NULL) THEN
          RAISE EXCEPTION 'manual execution confirmation required';
        END IF;
      END IF;
      IF NEW.tx_hash IS NOT NULL AND (TG_OP='INSERT' OR OLD.tx_hash IS NULL) THEN
        IF NEW.tx_hash !~ '^[0-9a-f]{64}$' THEN RAISE EXCEPTION 'invalid TRON TXID'; END IF;
        PERFORM pg_advisory_xact_lock(hashtextextended('settlement:TRC20:'||lower(NEW.tx_hash),0));
        IF EXISTS(SELECT 1 FROM merchant_settlements WHERE network=NEW.network
                  AND lower(tx_hash)=lower(NEW.tx_hash) AND id<>NEW.id) THEN
          RAISE EXCEPTION 'settlement TXID already registered';
        END IF;
      END IF;
      RETURN NEW;
    END $$;
    CREATE TRIGGER country_settlement_guard BEFORE INSERT OR UPDATE OR DELETE ON merchant_settlements
      FOR EACH ROW EXECUTE FUNCTION protect_country_settlement();
    """)


def downgrade():
    op.execute("""DO $$ BEGIN
      IF EXISTS(SELECT 1 FROM merchant_settlements WHERE contract_version=1
        OR status IN ('approved','sent','reconciliation') OR approved_at IS NOT NULL OR sent_at IS NOT NULL
        OR execution_reference IS NOT NULL OR confirmation_kind='not_sent_verified') THEN
        RAISE EXCEPTION 'country settlement downgrade blocked: new settlement contract or execution evidence exists';
      END IF; END $$;""")
    op.execute('DROP TRIGGER country_settlement_guard ON merchant_settlements')
    op.execute('DROP FUNCTION protect_country_settlement()')
    op.drop_index('ix_settlement_country_status',table_name='merchant_settlements')
    op.drop_index('uq_merchant_settlement_registered_tx',table_name='merchant_settlements')
    op.drop_index('uq_merchant_settlement_one_pending',table_name='merchant_settlements')
    op.create_index('uq_merchant_settlement_one_pending','merchant_settlements',['merchant_id'],unique=True,
                    postgresql_where=sa.text("status='pending'"))
    for constraint in ('ck_settlement_financial_contract','ck_settlement_status','ck_settlement_country_currency'):
        op.drop_constraint(constraint,'merchant_settlements',type_='check')
    for name in ('business_id','balance_id','approved_by_id','sent_by_id','reconciliation_by_id'):
        op.drop_constraint('fk_settlement_'+name,'merchant_settlements',type_='foreignkey')
    for name in ('contract_version','country','balance_currency','business_id','balance_id','payout_balance_amount',
      'fee_balance_amount','reserve_amount','total_usdt','approved_by_id','approved_at','sent_by_id','sent_at',
      'confirmation_kind','network_verified','execution_reference','reconciliation_reason','reconciliation_by_id','reconciliation_at'):
        op.drop_column('merchant_settlements',name)
    for name in ('rate_rub','amount_rub','fee_rub','total_debit_rub'):
        op.alter_column('merchant_settlements',name,nullable=False)
