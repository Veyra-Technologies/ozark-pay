"""Enable GE card routing and close nullable country/evidence checks."""
from alembic import op
import sqlalchemy as sa

revision = '0033_ge_route_guards'
down_revision = '0032_crossborder_live'
branch_labels = None
depends_on = None

COUNTRY_CHECK = "(working_country IS NULL AND rate_parser_id IS NULL) OR (working_country IS NOT NULL AND rate_parser_id IS NOT NULL AND business_id IS NOT NULL AND ((working_country='RU' AND rate_parser_id='rapira') OR (working_country='KZ' AND rate_parser_id='binance_p2p_kaspi_buy_100000_avg10') OR (working_country='GE' AND rate_parser_id='xe')))"
EVIDENCE_CHECK = "NOT evidence_validated OR COALESCE(operation_type='deposit' AND metadata_json::jsonb ?& ARRAY['receipt_file','trader_id','amount_claimed','requisite','recipient_bank','operation_date','validation_version'],false)"
OLD = "IF NEW.enabled THEN RAISE EXCEPTION 'foreign payment directions are not enabled'; END IF;"
NEW = "IF NEW.enabled AND profile_country <> 'GE' THEN RAISE EXCEPTION 'foreign payment directions are not enabled'; END IF;"


def upgrade():
    definition = op.get_bind().scalar(sa.text("SELECT pg_get_functiondef('guard_payment_jurisdiction()'::regprocedure)"))
    if OLD not in definition:
        raise RuntimeError('unexpected jurisdiction guard; review before enabling GE')
    op.execute(definition.replace(OLD, NEW))
    op.drop_constraint('ck_merchant_country_parser', 'merchants', type_='check')
    op.create_check_constraint('ck_merchant_country_parser', 'merchants', COUNTRY_CHECK)
    op.drop_constraint('ck_appeal_validated_evidence', 'appeals', type_='check')
    op.create_check_constraint('ck_appeal_validated_evidence', 'appeals', EVIDENCE_CHECK)


def downgrade():
    if op.get_bind().scalar(sa.text("SELECT count(*) FROM requisites WHERE country='GE' AND enabled")):
        raise RuntimeError('refusing to disable a route with active GE requisites; use verified rollback backup')
    definition = op.get_bind().scalar(sa.text("SELECT pg_get_functiondef('guard_payment_jurisdiction()'::regprocedure)"))
    if NEW not in definition:
        raise RuntimeError('unexpected jurisdiction guard')
    op.execute(definition.replace(NEW, OLD))
    # Stronger checks are compatible with 0032 and are kept until its downgrade.
