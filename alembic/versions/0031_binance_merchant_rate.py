"""Direction-scoped Binance P2P merchant quote source.

Revision ID: 0031_binance_merchant_rate
Revises: 0030_external_processors
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = '0031_binance_merchant_rate'
down_revision = '0030_external_processors'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'merchant_crossborder_rate_settings',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('merchant_id', postgresql.UUID(as_uuid=True),
                  sa.ForeignKey('merchants.id', ondelete='CASCADE'), nullable=False),
        sa.Column('direction', sa.String(5), nullable=False),
        sa.Column('merchant_country', sa.String(2), nullable=False),
        sa.Column('destination_country', sa.String(2), nullable=False),
        sa.Column('pay_currency', sa.String(3), nullable=False),
        sa.Column('parser_id', sa.String(64), nullable=False),
        sa.Column('updated_by', postgresql.UUID(as_uuid=True),
                  sa.ForeignKey('users.id', ondelete='SET NULL'), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.UniqueConstraint('merchant_id', 'direction',
                            name='uq_merchant_crossborder_rate_direction'),
        sa.CheckConstraint("direction = 'kz-ge'",
                           name='ck_merchant_crossborder_rate_direction'),
        sa.CheckConstraint("merchant_country = 'KZ'",
                           name='ck_merchant_crossborder_rate_country'),
        sa.CheckConstraint("destination_country = 'GE'",
                           name='ck_merchant_crossborder_rate_destination'),
        sa.CheckConstraint("pay_currency = 'KZT'",
                           name='ck_merchant_crossborder_rate_currency'),
        sa.CheckConstraint(
            "parser_id = 'binance_p2p_kaspi_buy_100000_avg10'",
            name='ck_merchant_crossborder_rate_parser'),
    )
    op.create_index('ix_merchant_crossborder_rate_direction',
                    'merchant_crossborder_rate_settings', ['direction'])


def downgrade():
    if op.get_bind().scalar(sa.text(
        'SELECT count(*) FROM merchant_crossborder_rate_settings')):
        raise RuntimeError(
            'refusing to discard merchant crossborder parser assignments')
    op.drop_table('merchant_crossborder_rate_settings')
