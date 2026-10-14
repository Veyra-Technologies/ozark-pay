"""XE profile sources, direction-scoped existing tariffs and immutable previews."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '0028_xe_crossborder_quotes'
down_revision = '0027_trader_work_country'
branch_labels = None
depends_on = None


def fee_index(with_direction):
    fields = ['entity_type', 'entity_id', 'fee_side', 'payment_method', 'currency']
    if with_direction:
        fields.append('direction')
    fields += ['min_amount', 'max_amount', 'effective_from', 'effective_to']
    op.create_index('uq_fee_rules_active_exact', 'fee_rules', fields, unique=True,
                    postgresql_where=sa.text('is_active'), postgresql_nulls_not_distinct=True)


def upgrade():
    op.add_column('users', sa.Column('working_rate_source', sa.String(16)))
    op.execute("UPDATE users SET working_rate_source=CASE WHEN working_country='RU' THEN 'rapira' ELSE 'xe' END WHERE working_country IS NOT NULL")
    op.create_check_constraint('ck_users_work_rate_source', 'users',
        "(working_country IS NULL AND working_rate_source IS NULL) OR "
        "(working_country IS NOT NULL AND working_rate_source IS NOT NULL AND ((working_country='RU' AND working_rate_source='rapira') OR (working_country IN ('AM','GE') AND working_rate_source='xe')))")
    # The existing country/history/ownership trigger remains in force.
    op.add_column('fee_rules', sa.Column('direction', sa.String(5)))
    op.create_check_constraint('ck_fee_rules_direction', 'fee_rules',
        "direction IS NULL OR (direction IN ('kz-am','kz-ge') AND currency='USDT' AND payment_method='bank_transfer' AND entity_type IN ('merchant','trader'))")
    op.drop_index('uq_fee_rules_active_exact', table_name='fee_rules')
    fee_index(True)
    op.create_table('crossborder_quotes',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('actor_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('users.id', ondelete='RESTRICT'), nullable=False),
        sa.Column('merchant_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('merchants.id', ondelete='RESTRICT'), nullable=False),
        sa.Column('trader_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('users.id', ondelete='RESTRICT'), nullable=False),
        sa.Column('direction', sa.String(5), nullable=False),
        sa.Column('snapshot', postgresql.JSONB(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("direction IN ('kz-am','kz-ge')", name='ck_crossborder_quote_direction'),
        sa.CheckConstraint('expires_at > created_at', name='ck_crossborder_quote_expiry'))
    op.create_index('ix_crossborder_quotes_actor_created', 'crossborder_quotes', ['actor_id', 'created_at'])
    op.execute("""CREATE FUNCTION protect_crossborder_quote() RETURNS trigger LANGUAGE plpgsql AS $$
      BEGIN RAISE EXCEPTION 'crossborder quote snapshots are immutable'; END $$""")
    op.execute('CREATE TRIGGER crossborder_quote_immutable BEFORE UPDATE OR DELETE ON crossborder_quotes FOR EACH ROW EXECUTE FUNCTION protect_crossborder_quote()')


def downgrade():
    # Never discard fixed calculations implicitly.
    if op.get_bind().scalar(sa.text('SELECT count(*) FROM crossborder_quotes')):
        raise RuntimeError('Export immutable crossborder quotes before downgrading')
    if op.get_bind().scalar(sa.text('SELECT count(*) FROM fee_rules WHERE direction IS NOT NULL')):
        raise RuntimeError('Direction tariffs require an explicit downgrade migration')
    op.drop_table('crossborder_quotes')
    op.execute('DROP FUNCTION protect_crossborder_quote()')
    op.drop_index('uq_fee_rules_active_exact', table_name='fee_rules')
    fee_index(False)
    op.drop_constraint('ck_fee_rules_direction', 'fee_rules')
    op.drop_column('fee_rules', 'direction')
    op.drop_constraint('ck_users_work_rate_source', 'users')
    op.drop_column('users', 'working_rate_source')
