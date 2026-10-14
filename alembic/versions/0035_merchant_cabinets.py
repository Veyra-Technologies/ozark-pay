"""Country-scoped merchant plans; no conversion of historical financial data."""
from datetime import datetime, timezone
import json
import uuid
from alembic import op
import sqlalchemy as sa

revision = '0035_merchant_cabinets'
down_revision = '0034_canonical_usdt'
branch_labels = None
depends_on = None

LEGACY = "(basis='base_usdt' AND currency='USDT' AND ((party='merchant' AND country='KZ') OR (party='trader' AND country='GE'))) OR (basis='local_amount' AND ((party='merchant' AND country='KZ' AND currency='KZT') OR (party='trader' AND country='GE' AND currency='GEL')))"
DIMENSIONS = f"(direction='kz-ge' AND ({LEGACY})) OR (direction='ru' AND party='merchant' AND country='RU' AND currency='RUB' AND basis='local_amount')"


def upgrade():
    op.add_column('merchants', sa.Column('working_currency', sa.String(3), nullable=True))
    op.add_column('merchants', sa.Column('tariff_plan_id', sa.UUID(), nullable=True))
    op.add_column('merchants', sa.Column('payment_methods', sa.JSON(), nullable=True))
    op.create_foreign_key('fk_merchant_tariff_plan', 'merchants', 'country_tariff_plans', ['tariff_plan_id'], ['id'], ondelete='RESTRICT')
    op.execute("UPDATE merchants SET working_currency=CASE working_country WHEN 'RU' THEN 'RUB' WHEN 'KZ' THEN 'KZT' WHEN 'GE' THEN 'GEL' END")
    op.create_check_constraint('ck_merchant_currency', 'merchants', "working_currency IS NULL OR (working_country='RU' AND working_currency='RUB') OR (working_country='KZ' AND working_currency='KZT') OR (working_country='GE' AND working_currency='GEL')")
    for name in ('ck_country_tariff_direction', 'ck_country_tariff_dimensions'):
        op.drop_constraint(name, 'country_tariff_plans', type_='check')
    op.create_check_constraint('ck_country_tariff_direction', 'country_tariff_plans', "direction IN ('kz-ge','ru')")
    op.create_check_constraint('ck_country_tariff_dimensions', 'country_tariff_plans', DIMENSIONS)
    # Existing new-RU-cabinet default (15%) becomes a selectable template. No
    # existing merchant is assigned this template or has its fee rules altered.
    op.get_bind().execute(sa.text("""INSERT INTO country_tariff_plans
        (id,scope_key,version,party,country,currency,basis,direction,entity_id,tiers,reference,effective_from,is_active)
        VALUES (:id,'ru:merchant:default',1,'merchant','RU','RUB','local_amount','ru',NULL,
                CAST(:tiers AS jsonb),'{}',:now,true)"""),
        {'id': uuid.uuid4(), 'now': datetime.now(timezone.utc),
         'tiers': json.dumps([{'min': '0.01', 'max': None, 'percent': '15.00'}])})
    op.execute("""CREATE FUNCTION validate_merchant_cabinet() RETURNS trigger AS $$
    DECLARE plan country_tariff_plans%ROWTYPE;
    BEGIN
      IF TG_OP='UPDATE' AND OLD.working_country IS NOT NULL AND
        (NEW.working_country IS DISTINCT FROM OLD.working_country OR NEW.business_id IS DISTINCT FROM OLD.business_id)
      THEN RAISE EXCEPTION 'merchant country and business are immutable'; END IF;
      IF NEW.tariff_plan_id IS NOT NULL THEN
        SELECT * INTO plan FROM country_tariff_plans WHERE id=NEW.tariff_plan_id;
        IF NOT FOUND OR plan.party <> 'merchant' OR plan.country IS DISTINCT FROM NEW.working_country
          OR (plan.entity_id IS NOT NULL AND plan.entity_id <> NEW.id)
          OR (NEW.working_country='KZ' AND plan.basis <> 'base_usdt')
          OR NEW.working_currency IS NULL OR NEW.payment_methods IS NULL
        THEN RAISE EXCEPTION 'incompatible merchant tariff assignment'; END IF;
      END IF;
      IF NEW.payment_methods IS NOT NULL THEN
        IF json_typeof(NEW.payment_methods)<>'array' OR json_array_length(NEW.payment_methods)=0
          OR NEW.working_country NOT IN ('RU','KZ') OR NEW.working_country IS NULL
        THEN RAISE EXCEPTION 'invalid country methods'; END IF;
        IF EXISTS(SELECT 1 FROM json_array_elements_text(NEW.payment_methods) m
            WHERE NOT ((NEW.working_country='RU' AND m IN ('sbp','c2c','mobile_commerce','sbp_qr'))
              OR (NEW.working_country='KZ' AND m='kz-ge')))
        THEN RAISE EXCEPTION 'incompatible country method'; END IF;
      END IF;
      RETURN NEW;
    END; $$ LANGUAGE plpgsql""")
    op.execute('CREATE TRIGGER merchant_cabinet_guard BEFORE INSERT OR UPDATE ON merchants FOR EACH ROW EXECUTE FUNCTION validate_merchant_cabinet()')


def downgrade():
    # Explicitly refuse losing any configured cabinet or RU tariff history.
    count = op.get_bind().execute(sa.text("SELECT count(*) FROM merchants WHERE tariff_plan_id IS NOT NULL OR payment_methods IS NOT NULL")).scalar()
    ru_versions = op.get_bind().execute(sa.text("SELECT count(*) FROM country_tariff_plans WHERE direction='ru'")).scalar()
    if count or ru_versions > 1:
        raise RuntimeError('Configured merchant cabinets require a verified database backup for rollback')
    op.execute('DROP TRIGGER merchant_cabinet_guard ON merchants')
    op.execute('DROP FUNCTION validate_merchant_cabinet()')
    op.drop_constraint('fk_merchant_tariff_plan', 'merchants', type_='foreignkey')
    op.drop_constraint('ck_merchant_currency', 'merchants', type_='check')
    for column in ('payment_methods', 'tariff_plan_id', 'working_currency'):
        op.drop_column('merchants', column)
    op.execute('ALTER TABLE country_tariff_plans DISABLE TRIGGER country_tariff_plans_immutable')
    op.execute("DELETE FROM country_tariff_plans WHERE direction='ru'")
    op.execute('ALTER TABLE country_tariff_plans ENABLE TRIGGER country_tariff_plans_immutable')
    for name in ('ck_country_tariff_direction', 'ck_country_tariff_dimensions'):
        op.drop_constraint(name, 'country_tariff_plans', type_='check')
    op.create_check_constraint('ck_country_tariff_direction', 'country_tariff_plans', "direction='kz-ge'")
    op.create_check_constraint('ck_country_tariff_dimensions', 'country_tariff_plans', LEGACY)
