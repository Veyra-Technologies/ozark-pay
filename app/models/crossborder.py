"""Draft-only configuration, deliberately unrelated to payment routing/balances."""
from sqlalchemy import CheckConstraint, ForeignKey, JSON, String, UniqueConstraint, DateTime, Index, event
from sqlalchemy.dialects.postgresql import JSONB
from datetime import datetime
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column
from app.db.base import Base, TimestampMixin
import uuid


class CrossBorderDraft(Base, TimestampMixin):
    __tablename__ = 'crossborder_drafts'
    __table_args__ = (
        UniqueConstraint('scope', 'owner_key', name='uq_crossborder_draft_owner'),
        CheckConstraint("status = 'preparing'", name='ck_crossborder_draft_preparing'),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    scope: Mapped[str] = mapped_column(String(24), nullable=False)
    owner_key: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default='preparing')
    data: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey('users.id', ondelete='SET NULL'))


class CrossBorderQuote(Base):
    """A fixed preview, never a Deposit or a reservation of funds."""
    __tablename__ = 'crossborder_quotes'
    __table_args__ = (
        CheckConstraint("direction IN ('kz-am','kz-ge')", name='ck_crossborder_quote_direction'),
        CheckConstraint('expires_at > created_at', name='ck_crossborder_quote_expiry'),
        Index('ix_crossborder_quotes_actor_created', 'actor_id', 'created_at'),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    actor_id: Mapped[uuid.UUID] = mapped_column(ForeignKey('users.id', ondelete='RESTRICT'))
    merchant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey('merchants.id', ondelete='RESTRICT'))
    trader_id: Mapped[uuid.UUID] = mapped_column(ForeignKey('users.id', ondelete='RESTRICT'))
    direction: Mapped[str] = mapped_column(String(5))
    snapshot: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class MerchantCrossBorderRateSetting(Base, TimestampMixin):
    """Direction-scoped merchant quote source; independent from trader country FX."""
    __tablename__ = 'merchant_crossborder_rate_settings'
    __table_args__ = (
        UniqueConstraint('merchant_id', 'direction', name='uq_merchant_crossborder_rate_direction'),
        CheckConstraint("direction = 'kz-ge'", name='ck_merchant_crossborder_rate_direction'),
        CheckConstraint("merchant_country = 'KZ'", name='ck_merchant_crossborder_rate_country'),
        CheckConstraint("destination_country = 'GE'", name='ck_merchant_crossborder_rate_destination'),
        CheckConstraint("pay_currency = 'KZT'", name='ck_merchant_crossborder_rate_currency'),
        CheckConstraint(
            "parser_id = 'binance_p2p_kaspi_buy_100000_avg10'",
            name='ck_merchant_crossborder_rate_parser',
        ),
        Index('ix_merchant_crossborder_rate_direction', 'direction'),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    merchant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey('merchants.id', ondelete='CASCADE'), nullable=False)
    direction: Mapped[str] = mapped_column(String(5), nullable=False)
    merchant_country: Mapped[str] = mapped_column(String(2), nullable=False)
    destination_country: Mapped[str] = mapped_column(String(2), nullable=False)
    pay_currency: Mapped[str] = mapped_column(String(3), nullable=False)
    parser_id: Mapped[str] = mapped_column(String(64), nullable=False)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey('users.id', ondelete='SET NULL'), nullable=True)


@event.listens_for(CrossBorderQuote, 'before_update')
@event.listens_for(CrossBorderQuote, 'before_delete')
def protect_quote(mapper, connection, target):
    raise ValueError('crossborder quote snapshots are immutable')
