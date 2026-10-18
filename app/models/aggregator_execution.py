"""Independent outbound providers; incoming AggregatorPayment remains unchanged."""
import uuid
from datetime import datetime
from decimal import Decimal
from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Index, Integer, JSON, Numeric, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column
from app.models.entities import Base, TimestampMixin


class AggregatorIntegration(Base, TimestampMixin):
    __tablename__ = 'aggregator_integrations'
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    aggregator_id: Mapped[uuid.UUID] = mapped_column(ForeignKey('aggregator_accounts.id', ondelete='RESTRICT'), unique=True)
    active_version: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(32), default='configured')
    routing_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(String(120))
    last_webhook_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (CheckConstraint("active_version>0 AND status IN ('configured','awaiting_verification','connected','suspended','connection_error')", name='ck_aggregator_integration_state'),)


class AggregatorIntegrationVersion(Base, TimestampMixin):
    __tablename__ = 'aggregator_integration_versions'
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    integration_id: Mapped[uuid.UUID] = mapped_column(ForeignKey('aggregator_integrations.id', ondelete='RESTRICT'))
    version: Mapped[int] = mapped_column(Integer)
    country: Mapped[str] = mapped_column(String(2), default='TJ')
    currency: Mapped[str] = mapped_column(String(3), default='TJS')
    base_url: Mapped[str] = mapped_column(String(500))
    adapter_id: Mapped[str] = mapped_column(String(80))
    api_version: Mapped[str] = mapped_column(String(80))
    environment: Mapped[str] = mapped_column(String(16), default='sandbox')
    credentials_encrypted: Mapped[str] = mapped_column(String)
    merchant_ids: Mapped[list] = mapped_column(JSON, default=list)
    allowed_methods: Mapped[list] = mapped_column(JSON, default=lambda: ['c2c'])
    allowed_ips: Mapped[list] = mapped_column(JSON, default=list)
    created_by: Mapped[uuid.UUID] = mapped_column(ForeignKey('users.id', ondelete='RESTRICT'))
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        UniqueConstraint('integration_id', 'version', name='uq_aggregator_integration_version'),
        CheckConstraint("country='TJ' AND currency='TJS' AND environment IN ('sandbox','production')", name='ck_aggregator_integration_scope'),
    )


class AggregatorWallet(Base, TimestampMixin):
    __tablename__ = 'aggregator_wallets'
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    aggregator_id: Mapped[uuid.UUID] = mapped_column(ForeignKey('aggregator_accounts.id', ondelete='RESTRICT'))
    currency: Mapped[str] = mapped_column(String(4))
    balance: Mapped[Decimal] = mapped_column(Numeric(24, 8), default=0)
    hold: Mapped[Decimal] = mapped_column(Numeric(24, 8), default=0)
    __table_args__ = (
        UniqueConstraint('aggregator_id', 'currency', name='uq_aggregator_wallet_currency'),
        CheckConstraint("currency IN ('TJS','USDT') AND balance>=0 AND hold>=0 AND hold<=balance", name='ck_aggregator_wallet_money'),
    )


class AggregatorWalletEntry(Base, TimestampMixin):
    __tablename__ = 'aggregator_wallet_entries'
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    wallet_id: Mapped[uuid.UUID] = mapped_column(ForeignKey('aggregator_wallets.id', ondelete='RESTRICT'))
    aggregator_id: Mapped[uuid.UUID] = mapped_column(ForeignKey('aggregator_accounts.id', ondelete='RESTRICT'))
    operation_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey('deposits.id', ondelete='RESTRICT'), nullable=True)
    entry_type: Mapped[str] = mapped_column(String(32))
    amount: Mapped[Decimal] = mapped_column(Numeric(24, 8))
    currency: Mapped[str] = mapped_column(String(4), default='USDT')
    balance_after: Mapped[Decimal] = mapped_column(Numeric(24, 8))
    hold_after: Mapped[Decimal] = mapped_column(Numeric(24, 8))
    idempotency_key: Mapped[str] = mapped_column(String(180), unique=True)
    description: Mapped[str] = mapped_column(String(500), default='')
    __table_args__ = (
        UniqueConstraint('operation_id', 'entry_type', name='uq_aggregator_operation_movement'),
        CheckConstraint("currency='USDT' AND amount>0 AND entry_type IN ('credit','hold','release','debit','debit_available')", name='ck_aggregator_entry_money'),
    )


class AggregatorExecution(Base, TimestampMixin):
    """Durable send intent and external state; never an internal Trader."""
    __tablename__ = 'aggregator_executions'
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    deposit_id: Mapped[uuid.UUID] = mapped_column(ForeignKey('deposits.id', ondelete='RESTRICT'), unique=True)
    aggregator_id: Mapped[uuid.UUID] = mapped_column(ForeignKey('aggregator_accounts.id', ondelete='RESTRICT'))
    integration_version_id: Mapped[uuid.UUID] = mapped_column(ForeignKey('aggregator_integration_versions.id', ondelete='RESTRICT'))
    external_id: Mapped[str] = mapped_column(String(80), unique=True)
    provider_order_id: Mapped[str | None] = mapped_column(String(160))
    state: Mapped[str] = mapped_column(String(32), default='prepared')
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    lease_token: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(String(120))
    requisites_encrypted: Mapped[str | None] = mapped_column(String)
    __table_args__ = (
        UniqueConstraint('integration_version_id', 'provider_order_id', name='uq_aggregator_provider_order'),
        Index('ix_aggregator_outbox_due', 'state','next_attempt_at','created_at'),
    )


class AggregatorProviderEvent(Base, TimestampMixin):
    __tablename__ = 'aggregator_provider_events'
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    integration_version_id: Mapped[uuid.UUID] = mapped_column(ForeignKey('aggregator_integration_versions.id', ondelete='RESTRICT'))
    execution_id: Mapped[uuid.UUID] = mapped_column(ForeignKey('aggregator_executions.id', ondelete='RESTRICT'))
    event_id: Mapped[str] = mapped_column(String(160))
    payload_hash: Mapped[str] = mapped_column(String(64))
    normalized_status: Mapped[str] = mapped_column(String(32))
    no_credit_confirmed: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(32), default='verified')
    __table_args__ = (UniqueConstraint('integration_version_id', 'event_id', name='uq_aggregator_provider_event'),)
