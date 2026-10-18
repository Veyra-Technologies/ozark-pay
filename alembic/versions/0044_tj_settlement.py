"""TJ Settlement reuses the country-scoped USDT/TRC20 contract in isolated QA."""
from alembic import op
import sqlalchemy as sa

revision = '0044_tj_settlement'
down_revision = '0043_generic_processor_protocol'
branch_labels = None
depends_on = None

PREVIOUS_FINANCIAL = "contract_version=0 OR ((contract_version=1 AND fee_usdt=5 AND amount_usdt>0\nAND total_usdt=amount_usdt+fee_usdt AND reserve_amount=payout_balance_amount+fee_balance_amount\nAND balance_id IS NOT NULL AND network='TRC20'\nAND ((country='RU' AND balance_currency='RUB' AND rate_rub>0\nAND payout_balance_amount=amount_rub AND fee_balance_amount=fee_rub AND reserve_amount=total_debit_rub\nAND amount_rub=round(amount_usdt*rate_rub,2) AND fee_rub=round(fee_usdt*rate_rub,2))\nOR (country='KZ' AND balance_currency='USDT' AND payout_balance_amount=amount_usdt\nAND fee_balance_amount=fee_usdt AND reserve_amount=total_usdt AND rate_rub IS NULL\nAND amount_rub IS NULL AND fee_rub IS NULL AND total_debit_rub IS NULL))) IS TRUE)"
FINANCIAL = "contract_version=0 OR ((contract_version=1 AND fee_usdt=5 AND amount_usdt>0\nAND total_usdt=amount_usdt+fee_usdt AND reserve_amount=payout_balance_amount+fee_balance_amount\nAND balance_id IS NOT NULL AND network='TRC20'\nAND ((country='RU' AND balance_currency='RUB' AND rate_rub>0\nAND payout_balance_amount=amount_rub AND fee_balance_amount=fee_rub AND reserve_amount=total_debit_rub\nAND amount_rub=round(amount_usdt*rate_rub,2) AND fee_rub=round(fee_usdt*rate_rub,2))\nOR (country IN ('KZ','TJ') AND balance_currency='USDT' AND payout_balance_amount=amount_usdt\nAND fee_balance_amount=fee_usdt AND reserve_amount=total_usdt AND rate_rub IS NULL\nAND amount_rub IS NULL AND fee_rub IS NULL AND total_debit_rub IS NULL))) IS TRUE)"


def upgrade():
    op.drop_constraint('ck_settlement_country_currency', 'merchant_settlements', type_='check')
    op.create_check_constraint('ck_settlement_country_currency', 'merchant_settlements', "country IN ('RU','KZ','TJ') AND balance_currency IN ('RUB','USDT')")
    op.drop_constraint('ck_settlement_financial_contract', 'merchant_settlements', type_='check')
    op.create_check_constraint('ck_settlement_financial_contract', 'merchant_settlements', FINANCIAL)
    op.execute("""
    CREATE FUNCTION guard_tj_settlement_admission() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF (TG_OP <> 'INSERT' AND OLD.country='TJ') OR (TG_OP <> 'DELETE' AND NEW.country='TJ') THEN
        IF NOT tj_isolated_finance_allowed() THEN RAISE EXCEPTION 'TJ settlement production admission is closed'; END IF;
        IF TG_OP='DELETE' THEN RAISE EXCEPTION 'TJ settlement history is immutable'; END IF;
      END IF;
      IF TG_OP='DELETE' THEN RETURN OLD; ELSE RETURN NEW; END IF;
    END $$;
    CREATE TRIGGER tj_settlement_admission BEFORE INSERT OR UPDATE OR DELETE ON merchant_settlements
      FOR EACH ROW EXECUTE FUNCTION guard_tj_settlement_admission();
    """)


def downgrade():
    if op.get_bind().scalar(sa.text("SELECT count(*) FROM merchant_settlements WHERE country='TJ'")):
        raise RuntimeError('TJ settlement history exists; downgrade would discard the financial contract. Use a compatible forward rollback.')
    op.execute('DROP TRIGGER tj_settlement_admission ON merchant_settlements')
    op.execute('DROP FUNCTION guard_tj_settlement_admission()')
    op.drop_constraint('ck_settlement_country_currency', 'merchant_settlements', type_='check')
    op.create_check_constraint('ck_settlement_country_currency', 'merchant_settlements', "country IN ('RU','KZ') AND balance_currency IN ('RUB','USDT')")
    op.drop_constraint('ck_settlement_financial_contract', 'merchant_settlements', type_='check')
    op.create_check_constraint('ck_settlement_financial_contract', 'merchant_settlements', PREVIOUS_FINANCIAL)
