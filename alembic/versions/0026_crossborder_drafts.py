"""Isolated preparation drafts; no live cross-border operation or balance."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '0026_crossborder_drafts'
down_revision = '0025_teamlead_merchant_referrals'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('crossborder_drafts',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('scope', sa.String(24), nullable=False),
        sa.Column('owner_key', sa.String(64), nullable=False),
        sa.Column('status', sa.String(16), nullable=False),
        sa.Column('data', sa.JSON(), nullable=False),
        sa.Column('updated_by', postgresql.UUID(as_uuid=True), sa.ForeignKey('users.id', ondelete='SET NULL')),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint('scope', 'owner_key', name='uq_crossborder_draft_owner'),
        sa.CheckConstraint("status = 'preparing'", name='ck_crossborder_draft_preparing'))


def downgrade():
    op.drop_table('crossborder_drafts')
