"""Outbound external payment processors and generic Merchant HMAC v2 routing.

Revision ID: 0030_external_processors
Revises: 0029_trader_wallets_local_banks
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = '0030_external_processors'
down_revision = '0029_trader_wallets_local_banks'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'external_processor_connections',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('name', sa.String(160), nullable=False),
        sa.Column('adapter_type', sa.String(32), nullable=False, server_default='merchant_hmac_v2'),
        sa.Column('environment', sa.String(16), nullable=False, server_default='sandbox'),
        sa.Column('api_base_url', sa.String(500), nullable=False),
        sa.Column('api_key_encrypted', sa.Text(), nullable=False),
        sa.Column('api_secret_encrypted', sa.Text(), nullable=False),
        sa.Column('webhook_key_id', sa.String(160), nullable=False),
        sa.Column('webhook_secret_encrypted', sa.Text(), nullable=False),
        sa.Column('supported_methods', postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column('supported_currencies', postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column('min_amount', sa.Numeric(18,2), nullable=False, server_default='100.00'),
        sa.Column('max_amount', sa.Numeric(18,2), nullable=False, server_default='150000.00'),
        sa.Column('daily_limit', sa.Numeric(18,2), nullable=True),
        sa.Column('monthly_limit', sa.Numeric(18,2), nullable=True),
        sa.Column('executor_fee_percent', sa.Numeric(8,4), nullable=True),
        sa.Column('priority', sa.Integer(), nullable=False, server_default='100'),
        sa.Column('routing_enabled', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('config_version', sa.Integer(), nullable=False, server_default='1'),
        sa.Column('last_check_status', sa.String(32), nullable=False, server_default='never'),
        sa.Column('last_check_error', sa.Text(), nullable=True),
        sa.Column('last_checked_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_by', postgresql.UUID(as_uuid=True), sa.ForeignKey('users.id', ondelete='SET NULL'), nullable=True),
        sa.Column('updated_by', postgresql.UUID(as_uuid=True), sa.ForeignKey('users.id', ondelete='SET NULL'), nullable=True),
        sa.Column('is_archived', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint('name', name='uq_external_processor_connections_name'),
        sa.CheckConstraint("adapter_type IN ('merchant_hmac_v2')", name='ck_external_processor_adapter'),
        sa.CheckConstraint("environment IN ('sandbox','production')", name='ck_external_processor_environment'),
        sa.CheckConstraint('min_amount >= 0', name='ck_external_processor_min_amount'),
        sa.CheckConstraint('max_amount > min_amount', name='ck_external_processor_amount_range'),
        sa.CheckConstraint('priority >= 0', name='ck_external_processor_priority'),
        sa.CheckConstraint('executor_fee_percent IS NULL OR (executor_fee_percent >= 0 AND executor_fee_percent <= 100)', name='ck_external_processor_fee_percent'),
    )
    for column in ('name','adapter_type','environment','priority','routing_enabled','last_check_status','is_archived'):
        op.create_index(f'ix_external_processor_connections_{column}', 'external_processor_connections', [column])

    op.create_table(
        'external_processor_versions',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('connection_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('external_processor_connections.id', ondelete='RESTRICT'), nullable=False),
        sa.Column('version', sa.Integer(), nullable=False),
        sa.Column('adapter_type', sa.String(32), nullable=False),
        sa.Column('environment', sa.String(16), nullable=False),
        sa.Column('api_base_url', sa.String(500), nullable=False),
        sa.Column('api_key_encrypted', sa.Text(), nullable=False),
        sa.Column('api_secret_encrypted', sa.Text(), nullable=False),
        sa.Column('webhook_key_id', sa.String(160), nullable=False),
        sa.Column('webhook_secret_encrypted', sa.Text(), nullable=False),
        sa.Column('supported_methods', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('supported_currencies', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('min_amount', sa.Numeric(18,2), nullable=False),
        sa.Column('max_amount', sa.Numeric(18,2), nullable=False),
        sa.Column('daily_limit', sa.Numeric(18,2), nullable=True),
        sa.Column('monthly_limit', sa.Numeric(18,2), nullable=True),
        sa.Column('executor_fee_percent', sa.Numeric(8,4), nullable=False),
        sa.Column('priority', sa.Integer(), nullable=False),
        sa.Column('routing_enabled', sa.Boolean(), nullable=False),
        sa.Column('created_by', postgresql.UUID(as_uuid=True), sa.ForeignKey('users.id', ondelete='SET NULL'), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint('connection_id','version', name='uq_external_processor_version'),
        sa.CheckConstraint("adapter_type IN ('merchant_hmac_v2')", name='ck_external_processor_version_adapter'),
        sa.CheckConstraint("environment IN ('sandbox','production')", name='ck_external_processor_version_environment'),
    )
    op.create_index('ix_external_processor_versions_connection_id', 'external_processor_versions', ['connection_id'])
    op.create_index('ix_external_processor_versions_webhook_key_id', 'external_processor_versions', ['webhook_key_id'])

    op.create_table(
        'external_processor_payments',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('deposit_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('deposits.id', ondelete='RESTRICT'), nullable=False),
        sa.Column('connection_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('external_processor_connections.id', ondelete='RESTRICT'), nullable=False),
        sa.Column('connection_version_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('external_processor_versions.id', ondelete='RESTRICT'), nullable=False),
        sa.Column('idempotency_key', sa.String(128), nullable=False),
        sa.Column('external_order_id', sa.String(128), nullable=False),
        sa.Column('external_payment_id', sa.String(128), nullable=True),
        sa.Column('amount', sa.Numeric(18,2), nullable=False),
        sa.Column('currency', sa.String(8), nullable=False),
        sa.Column('payment_method', sa.String(32), nullable=False),
        sa.Column('request_fingerprint', sa.String(64), nullable=False),
        sa.Column('request_status', sa.String(32), nullable=False, server_default='prepared'),
        sa.Column('external_status', sa.String(32), nullable=True),
        sa.Column('payment_details_snapshot', postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('response_status_code', sa.Integer(), nullable=True),
        sa.Column('last_error', sa.Text(), nullable=True),
        sa.Column('last_reconciled_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('fee_percent', sa.Numeric(8,4), nullable=False),
        sa.Column('fee_amount', sa.Numeric(18,2), nullable=False),
        sa.Column('metadata_json', postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint('deposit_id', name='uq_external_processor_payment_deposit'),
        sa.UniqueConstraint('connection_id','idempotency_key', name='uq_external_processor_payment_idempotency'),
        sa.UniqueConstraint('connection_id','external_order_id', name='uq_external_processor_payment_order'),
        sa.UniqueConstraint('connection_id','external_payment_id', name='uq_external_processor_payment_external_id'),
    )
    for column in ('deposit_id','connection_id','connection_version_id','external_order_id','external_payment_id','request_status','external_status'):
        op.create_index(f'ix_external_processor_payments_{column}', 'external_processor_payments', [column])

    op.create_table(
        'external_processor_webhook_events',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('connection_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('external_processor_connections.id', ondelete='RESTRICT'), nullable=False),
        sa.Column('processor_payment_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('external_processor_payments.id', ondelete='SET NULL'), nullable=True),
        sa.Column('external_event_id', sa.String(128), nullable=False),
        sa.Column('event_type', sa.String(80), nullable=False),
        sa.Column('key_id', sa.String(160), nullable=False),
        sa.Column('event_timestamp', sa.Integer(), nullable=False),
        sa.Column('correlation_id', sa.String(128), nullable=True),
        sa.Column('payload_json', postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column('status', sa.String(32), nullable=False, server_default='processing'),
        sa.Column('result_message', sa.Text(), nullable=True),
        sa.Column('processed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint('connection_id','external_event_id', name='uq_external_processor_webhook_event'),
    )
    for column in ('connection_id','processor_payment_id','event_type','status'):
        op.create_index(f'ix_external_processor_webhook_events_{column}', 'external_processor_webhook_events', [column])

    op.drop_constraint('ck_fee_rules_entity_fee_side', 'fee_rules', type_='check')
    op.drop_constraint('ck_fee_rules_entity_type', 'fee_rules', type_='check')
    op.create_check_constraint('ck_fee_rules_entity_type', 'fee_rules', "entity_type IN ('merchant','trader','aggregator','external_processor')")
    op.create_check_constraint('ck_fee_rules_entity_fee_side', 'fee_rules', "(entity_type = 'merchant' AND fee_side = 'merchant_fee') OR (entity_type IN ('trader','aggregator','external_processor') AND fee_side = 'executor_fee')")
    op.drop_constraint('ck_operation_snapshot_executor_type', 'operation_fee_snapshots', type_='check')
    op.create_check_constraint('ck_operation_snapshot_executor_type', 'operation_fee_snapshots', "executor_type IN ('trader','aggregator','external_processor')")


def downgrade():
    bind = op.get_bind()
    count = bind.execute(sa.text('SELECT count(*) FROM external_processor_connections')).scalar_one()
    if count:
        raise RuntimeError('refusing destructive external processor downgrade while connections exist')
    op.drop_constraint('ck_operation_snapshot_executor_type', 'operation_fee_snapshots', type_='check')
    op.create_check_constraint('ck_operation_snapshot_executor_type', 'operation_fee_snapshots', "executor_type IN ('trader','aggregator')")
    op.drop_constraint('ck_fee_rules_entity_fee_side', 'fee_rules', type_='check')
    op.drop_constraint('ck_fee_rules_entity_type', 'fee_rules', type_='check')
    op.create_check_constraint('ck_fee_rules_entity_type', 'fee_rules', "entity_type IN ('merchant','trader','aggregator')")
    op.create_check_constraint('ck_fee_rules_entity_fee_side', 'fee_rules', "(entity_type = 'merchant' AND fee_side = 'merchant_fee') OR (entity_type IN ('trader','aggregator') AND fee_side = 'executor_fee')")
    op.drop_table('external_processor_webhook_events')
    op.drop_table('external_processor_payments')
    op.drop_table('external_processor_versions')
    op.drop_table('external_processor_connections')
