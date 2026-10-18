"""Generic processor naming with a persisted, compatible legacy wire dialect.

Financial amounts, operation IDs, credentials and HMAC canonical bytes stay intact.
"""
from alembic import op
import sqlalchemy as sa

revision = '0043_generic_processor_protocol'
down_revision = '0042_external_aggregator'
branch_labels = None
depends_on = None


def upgrade():
    for table, constraint in [('external_processor_connections', 'ck_external_processor_adapter'),
                              ('external_processor_versions', 'ck_external_processor_version_adapter')]:
        op.add_column(table, sa.Column('webhook_header_prefix', sa.String(64), nullable=False, server_default='X-Processor'))
        # Preserve the previous integration's real HTTP header names as data.
        # Fresh installations already have the generic adapter and keep its default.
        op.execute(sa.text(f"UPDATE {table} SET webhook_header_prefix = 'X-' || adapter_type WHERE adapter_type <> 'merchant_hmac_v2'"))
        op.drop_constraint(constraint, table, type_='check')
        op.execute(sa.text(f"UPDATE {table} SET adapter_type = 'merchant_hmac_v2'"))
        op.create_check_constraint(constraint, table, "adapter_type = 'merchant_hmac_v2'")
    op.alter_column('external_processor_connections', 'adapter_type', server_default='merchant_hmac_v2')


def downgrade():
    connection = op.get_bind()
    for table in ('external_processor_connections', 'external_processor_versions'):
        used = connection.execute(sa.text(f"SELECT count(*) FROM {table} WHERE webhook_header_prefix <> 'X-Processor'")).scalar()
        if used:
            raise RuntimeError('Legacy wire dialect is in use; restore the verified pre-upgrade backup rather than discard protocol history.')
    for table in ('external_processor_versions', 'external_processor_connections'):
        op.drop_column(table, 'webhook_header_prefix')
