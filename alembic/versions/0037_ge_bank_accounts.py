"""Admit GE bank accounts alongside existing cards without rewriting any rows."""
import sqlalchemy as sa
from alembic import op

revision = '0037_ge_bank_accounts'
down_revision = '0036_country_settlements'
branch_labels = None
depends_on = None

OLD = "NEW.method IS DISTINCT FROM 'c2c' OR NEW.bank_id IS NULL"
NEW = "(NEW.method IS DISTINCT FROM 'c2c' AND NOT (profile_country='GE' AND NEW.method='bank_account')) OR NEW.bank_id IS NULL"


def upgrade():
    definition = op.get_bind().scalar(sa.text("SELECT pg_get_functiondef('guard_payment_jurisdiction()'::regprocedure)"))
    if OLD not in definition:
        raise RuntimeError('Unexpected jurisdiction guard; manual review required')
    op.execute(definition.replace(OLD, NEW).replace('foreign requisite requires a directory bank and c2c',
                                                  'foreign requisite requires a supported method and directory bank'))


def downgrade():
    if op.get_bind().scalar(sa.text("SELECT count(*) FROM requisites WHERE country='GE' AND method='bank_account'")):
        raise RuntimeError('GE account requisites must be retained; use a verified rollback backup')
    definition = op.get_bind().scalar(sa.text("SELECT pg_get_functiondef('guard_payment_jurisdiction()'::regprocedure)"))
    if NEW not in definition:
        raise RuntimeError('Unexpected jurisdiction guard')
    op.execute(definition.replace(NEW, OLD).replace('foreign requisite requires a supported method and directory bank',
                                                  'foreign requisite requires a directory bank and c2c'))
