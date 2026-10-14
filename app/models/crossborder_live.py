"""Versioned configuration and currency-explicit KZ -> GE settlement records."""
import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Index, Integer, Numeric, String, UniqueConstraint, event
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin


class BusinessAccount(Base, TimestampMixin):
    __tablename__ = 'business_accounts'
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(160))
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey('users.id', ondelete='RESTRICT'))


class ParserConfiguration(Base, TimestampMixin):
    __tablename__ = 'parser_configurations'
    __table_args__ = (UniqueConstraint('parser_id', 'version', name='uq_parser_version'),)
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    parser_id: Mapped[str] = mapped_column(String(64), index=True)
    version: Mapped[int] = mapped_column(Integer)
    country: Mapped[str] = mapped_column(String(2))
    direction: Mapped[str | None] = mapped_column(String(5))
    parameters: Mapped[dict] = mapped_column(JSONB)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey('users.id', ondelete='RESTRICT'))


class RateObservation(Base):
    __tablename__ = 'rate_observations'
    __table_args__ = (
        UniqueConstraint('configuration_id', 'fingerprint', name='uq_rate_observation'),
        CheckConstraint('expires_at > fetched_at', name='ck_rate_observation_expiry'),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    configuration_id: Mapped[uuid.UUID] = mapped_column(ForeignKey('parser_configurations.id', ondelete='RESTRICT'), index=True)
    fingerprint: Mapped[str] = mapped_column(String(64))
    snapshot: Mapped[dict] = mapped_column(JSONB)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    fixed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class TariffPlan(Base, TimestampMixin):
    __tablename__ = 'country_tariff_plans'
    __table_args__ = (
        UniqueConstraint('scope_key', 'version', name='uq_country_tariff_version'),
        CheckConstraint("party IN ('merchant','trader')", name='ck_country_tariff_party'),
        CheckConstraint("direction IN ('kz-ge','ru')", name='ck_country_tariff_direction'),
        CheckConstraint("(direction='kz-ge' AND ((basis='base_usdt' AND currency='USDT' AND ((party='merchant' AND country='KZ') OR (party='trader' AND country='GE'))) OR (basis='local_amount' AND ((party='merchant' AND country='KZ' AND currency='KZT') OR (party='trader' AND country='GE' AND currency='GEL'))))) OR (direction='ru' AND party='merchant' AND country='RU' AND currency='RUB' AND basis='local_amount')", name='ck_country_tariff_dimensions'),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    scope_key: Mapped[str] = mapped_column(String(160), index=True)
    version: Mapped[int] = mapped_column(Integer)
    party: Mapped[str] = mapped_column(String(16))
    country: Mapped[str] = mapped_column(String(2))
    currency: Mapped[str] = mapped_column(String(4))
    basis: Mapped[str] = mapped_column(String(16), default='local_amount', server_default='local_amount')
    direction: Mapped[str] = mapped_column(String(5), default='kz-ge')
    entity_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), index=True)
    tiers: Mapped[list] = mapped_column(JSONB)
    reference: Mapped[dict] = mapped_column(JSONB, default=dict)
    effective_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey('users.id', ondelete='RESTRICT'))


class CrossBorderOperation(Base, TimestampMixin):
    __tablename__ = 'crossborder_operations'
    __table_args__ = (
        UniqueConstraint('merchant_id', 'external_id', name='uq_crossborder_external'),
        UniqueConstraint('quote_id', name='uq_crossborder_operation_quote'),
        CheckConstraint("direction = 'kz-ge'", name='ck_crossborder_operation_direction'),
        CheckConstraint("status IN ('accepted','paid','cancelled','refunded')", name='ck_crossborder_operation_status'),
        CheckConstraint('amount_kzt > 0 AND base_usdt > 0 AND reserved_usdt >= 0 AND merchant_usdt >= 0', name='ck_crossborder_operation_amounts'),
        CheckConstraint("status NOT IN ('paid','refunded') OR (actual_gel > 0 AND confirmed_at IS NOT NULL AND trader_usdt IS NOT NULL AND margin_usdt IS NOT NULL)", name='ck_crossborder_operation_confirmation'),
        Index('ix_crossborder_operation_trader_status', 'trader_id', 'status'),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    merchant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey('merchants.id', ondelete='RESTRICT'), index=True)
    trader_id: Mapped[uuid.UUID] = mapped_column(ForeignKey('users.id', ondelete='RESTRICT'))
    requisite_id: Mapped[uuid.UUID] = mapped_column(ForeignKey('requisites.id', ondelete='RESTRICT'))
    quote_id: Mapped[uuid.UUID] = mapped_column(ForeignKey('crossborder_quotes.id', ondelete='RESTRICT'))
    merchant_plan_id: Mapped[uuid.UUID] = mapped_column(ForeignKey('country_tariff_plans.id', ondelete='RESTRICT'))
    trader_plan_id: Mapped[uuid.UUID] = mapped_column(ForeignKey('country_tariff_plans.id', ondelete='RESTRICT'))
    external_id: Mapped[str] = mapped_column(String(128))
    direction: Mapped[str] = mapped_column(String(5), default='kz-ge')
    status: Mapped[str] = mapped_column(String(16), default='accepted')
    amount_kzt: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    expected_gel: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    actual_gel: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    base_usdt: Mapped[Decimal] = mapped_column(Numeric(24, 8))
    merchant_usdt: Mapped[Decimal] = mapped_column(Numeric(24, 8))
    reserved_usdt: Mapped[Decimal] = mapped_column(Numeric(24, 8))
    trader_usdt: Mapped[Decimal | None] = mapped_column(Numeric(24, 8))
    margin_usdt: Mapped[Decimal | None] = mapped_column(Numeric(24, 8))
    expenses_usdt: Mapped[Decimal] = mapped_column(Numeric(24, 8), default=Decimal('0'))
    snapshot: Mapped[dict] = mapped_column(JSONB)
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    confirmation_source: Mapped[str | None] = mapped_column(String(32))
    confirmation_reference: Mapped[str | None] = mapped_column(String(200))
    confirmed_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey('users.id', ondelete='RESTRICT'))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class CrossBorderEvent(Base, TimestampMixin):
    __tablename__ = 'crossborder_events'
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    operation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey('crossborder_operations.id', ondelete='RESTRICT'), index=True)
    event_key: Mapped[str] = mapped_column(String(160), unique=True)
    payload_hash: Mapped[str] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(32))


class ParserAssignment(Base, TimestampMixin):
    """Each change appends a version; the latest version of a scope is effective."""
    __tablename__ = 'parser_assignments'
    __table_args__ = (
        UniqueConstraint('scope_key', 'version', name='uq_parser_assignment_version'),
        CheckConstraint('(merchant_id IS NULL) <> (trader_id IS NULL)', name='ck_parser_assignment_owner'),
        CheckConstraint("purpose IN ('financial','informational')", name='ck_parser_assignment_purpose'),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    scope_key: Mapped[str] = mapped_column(String(200), index=True)
    version: Mapped[int] = mapped_column(Integer)
    merchant_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey('merchants.id', ondelete='RESTRICT'), index=True)
    trader_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey('users.id', ondelete='RESTRICT'), index=True)
    country: Mapped[str] = mapped_column(String(2))
    parser_id: Mapped[str] = mapped_column(String(64))
    purpose: Mapped[str] = mapped_column(String(16))
    configuration_id: Mapped[uuid.UUID] = mapped_column(ForeignKey('parser_configurations.id', ondelete='RESTRICT'))
    configuration_version: Mapped[int] = mapped_column(Integer)
    is_active: Mapped[bool] = mapped_column(Boolean)
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey('users.id', ondelete='RESTRICT'))
    reason: Mapped[str] = mapped_column(String(200))


def immutable(mapper, connection, target):
    raise ValueError('financial configuration and observations are append-only')


for model in (RateObservation, TariffPlan, ParserConfiguration, CrossBorderEvent, ParserAssignment):
    event.listen(model, 'before_update', immutable)
    event.listen(model, 'before_delete', immutable)
