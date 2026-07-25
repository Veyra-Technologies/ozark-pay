import uuid
from datetime import datetime
from decimal import Decimal
from sqlalchemy import CheckConstraint, DateTime, String, Boolean, ForeignKey, Numeric, Text, UniqueConstraint, Index, Integer, JSON, event, inspect, text
from sqlalchemy.dialects.postgresql import UUID, INET
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.db.base import Base, TimestampMixin
from app.core.enums import Role, DepositStatus, PayoutStatus, PaymentMethod, AppealStatus, LedgerType
from app.core.security import encrypt_secret

class User(Base, TimestampMixin):
    __tablename__='users'
    id: Mapped[uuid.UUID]=mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email: Mapped[str]=mapped_column(String(255), unique=True, index=True)
    password_hash: Mapped[str]=mapped_column(String(255))
    role: Mapped[str]=mapped_column(String(32), default=Role.merchant.value, index=True)
    is_active: Mapped[bool]=mapped_column(Boolean, default=True)
    is_locked: Mapped[bool]=mapped_column(Boolean, default=False)
    failed_login_count: Mapped[int]=mapped_column(Integer, default=0)
    twofa_secret: Mapped[str|None]=mapped_column(String(255), nullable=True)
    twofa_enabled: Mapped[bool]=mapped_column(Boolean, default=False)
    trader_balance: Mapped[Decimal]=mapped_column(Numeric(18,2), default=Decimal('0.00'))
    trader_hold: Mapped[Decimal]=mapped_column(Numeric(18,2), default=Decimal('0.00'))
    trader_traffic_priority: Mapped[int]=mapped_column(Integer, default=50)
    trader_assigned_merchants: Mapped[list]=mapped_column(JSON, default=list)
    trader_commission_percent: Mapped[Decimal]=mapped_column(Numeric(5,2), default=Decimal('7.00'))
    trader_traffic_status: Mapped[str]=mapped_column(String(32), default='active', index=True)
    trader_risk_score: Mapped[int]=mapped_column(Integer, default=0)
    trader_withdrawals_frozen: Mapped[bool]=mapped_column(Boolean, default=False)
    trader_limited_until: Mapped[datetime|None]=mapped_column(DateTime(timezone=True), nullable=True)
    trader_limited_max_active_payments: Mapped[int|None]=mapped_column(Integer, nullable=True)
    trader_limited_max_amount: Mapped[Decimal|None]=mapped_column(Numeric(18,2), nullable=True)
    trader_allow_high_amount: Mapped[bool]=mapped_column(Boolean, default=True)
    is_archived: Mapped[bool]=mapped_column(Boolean, default=False, index=True)
    archived_at: Mapped[datetime|None]=mapped_column(DateTime(timezone=True), nullable=True)
    archived_reason: Mapped[str|None]=mapped_column(String(255), nullable=True)
    merchant = relationship('Merchant', back_populates='owner', uselist=False)

class Merchant(Base, TimestampMixin):
    __tablename__='merchants'
    id: Mapped[uuid.UUID]=mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID]=mapped_column(ForeignKey('users.id', ondelete='CASCADE'), unique=True)
    name: Mapped[str]=mapped_column(String(160))
    webhook_url: Mapped[str|None]=mapped_column(String(500), nullable=True)
    ip_whitelist: Mapped[list]=mapped_column(JSON, default=list)
    sandbox_mode: Mapped[bool]=mapped_column(Boolean, default=True)
    merchant_commission_percent: Mapped[Decimal]=mapped_column(Numeric(5,2), default=Decimal('15.00'))
    is_archived: Mapped[bool]=mapped_column(Boolean, default=False, index=True)
    archived_at: Mapped[datetime|None]=mapped_column(DateTime(timezone=True), nullable=True)
    owner=relationship('User', back_populates='merchant')
    balances=relationship('Balance', back_populates='merchant')
    settlements=relationship('MerchantSettlement', back_populates='merchant')

class ApiKey(Base, TimestampMixin):
    __tablename__='api_keys'
    id: Mapped[uuid.UUID]=mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    merchant_id: Mapped[uuid.UUID]=mapped_column(ForeignKey('merchants.id', ondelete='CASCADE'), index=True)
    api_key: Mapped[str]=mapped_column(String(80), unique=True, index=True)
    secret_hash: Mapped[str]=mapped_column(String(255))
    is_active: Mapped[bool]=mapped_column(Boolean, default=True)
    mode: Mapped[str]=mapped_column(String(16), default='sandbox')
    last_used_at: Mapped[datetime|None]=mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime|None]=mapped_column(DateTime(timezone=True), nullable=True)
    __table_args__=(
        CheckConstraint("mode IN ('sandbox', 'production')", name='ck_api_keys_mode'),
        Index(
            'uq_api_keys_one_active_per_merchant_mode',
            'merchant_id',
            'mode',
            unique=True,
            postgresql_where=text('is_active'),
            sqlite_where=text('is_active'),
        ),
    )

class RefreshToken(Base, TimestampMixin):
    __tablename__='refresh_tokens'
    id: Mapped[uuid.UUID]=mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID]=mapped_column(ForeignKey('users.id', ondelete='CASCADE'), index=True)
    token_hash: Mapped[str]=mapped_column(String(64), unique=True, index=True)
    jti: Mapped[str]=mapped_column(String(80), unique=True, index=True)
    expires_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime|None]=mapped_column(DateTime(timezone=True), nullable=True)
    ip: Mapped[str|None]=mapped_column(String(64), nullable=True)
    user_agent: Mapped[str|None]=mapped_column(String(300), nullable=True)

class Balance(Base, TimestampMixin):
    __tablename__='balances'
    __table_args__=(UniqueConstraint('merchant_id','currency', name='uq_balance_merchant_currency'),)
    id: Mapped[uuid.UUID]=mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    merchant_id: Mapped[uuid.UUID]=mapped_column(ForeignKey('merchants.id', ondelete='CASCADE'), index=True)
    currency: Mapped[str]=mapped_column(String(8), default='RUB')
    available: Mapped[Decimal]=mapped_column(Numeric(18,2), default=Decimal('0.00'))
    frozen: Mapped[Decimal]=mapped_column(Numeric(18,2), default=Decimal('0.00'))
    merchant=relationship('Merchant', back_populates='balances')

class LedgerEntry(Base, TimestampMixin):
    __tablename__='ledger_entries'
    id: Mapped[uuid.UUID]=mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    merchant_id: Mapped[uuid.UUID]=mapped_column(ForeignKey('merchants.id', ondelete='CASCADE'), index=True)
    operation_id: Mapped[uuid.UUID|None]=mapped_column(UUID(as_uuid=True), nullable=True, index=True)
    entry_type: Mapped[str]=mapped_column(String(32))
    amount: Mapped[Decimal]=mapped_column(Numeric(18,2))
    currency: Mapped[str]=mapped_column(String(8), default='RUB')
    description: Mapped[str]=mapped_column(String(500), default='')
    idempotency_key: Mapped[str|None]=mapped_column(String(128), unique=True, nullable=True)

class Deposit(Base, TimestampMixin):
    __tablename__='deposits'
    id: Mapped[uuid.UUID]=mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    merchant_id: Mapped[uuid.UUID]=mapped_column(ForeignKey('merchants.id', ondelete='CASCADE'), index=True)
    external_id: Mapped[str]=mapped_column(String(128), index=True)
    idempotency_key: Mapped[str|None]=mapped_column(String(128), nullable=True, index=True)
    amount: Mapped[Decimal]=mapped_column(Numeric(18,2))
    currency: Mapped[str]=mapped_column(String(8), default='RUB')
    method: Mapped[str]=mapped_column(String(32))
    status: Mapped[str]=mapped_column(String(32), default=DepositStatus.created.value, index=True)
    requisites_id: Mapped[uuid.UUID|None]=mapped_column(UUID(as_uuid=True), ForeignKey('requisites.id'), nullable=True)
    client_ip: Mapped[str|None]=mapped_column(String(64), nullable=True)
    metadata_json: Mapped[dict]=mapped_column(JSON, default=dict)
    __table_args__=(
        UniqueConstraint('merchant_id','external_id', name='uq_deposit_merchant_external'),
        UniqueConstraint('merchant_id','idempotency_key', name='uq_deposit_merchant_idempotency'),
    )

class Payout(Base, TimestampMixin):
    __tablename__='payouts'
    id: Mapped[uuid.UUID]=mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    merchant_id: Mapped[uuid.UUID]=mapped_column(ForeignKey('merchants.id', ondelete='CASCADE'), index=True)
    external_id: Mapped[str]=mapped_column(String(128), index=True)
    idempotency_key: Mapped[str|None]=mapped_column(String(128), nullable=True, index=True)
    amount: Mapped[Decimal]=mapped_column(Numeric(18,2))
    currency: Mapped[str]=mapped_column(String(8), default='RUB')
    method: Mapped[str]=mapped_column(String(32))
    status: Mapped[str]=mapped_column(String(32), default=PayoutStatus.created.value, index=True)
    destination: Mapped[str]=mapped_column(String(255))
    metadata_json: Mapped[dict]=mapped_column(JSON, default=dict)
    __table_args__=(
        UniqueConstraint('merchant_id','external_id', name='uq_payout_merchant_external'),
        UniqueConstraint('merchant_id','idempotency_key', name='uq_payout_merchant_idempotency'),
    )


class MerchantSettlement(Base, TimestampMixin):
    __tablename__='merchant_settlements'
    id: Mapped[uuid.UUID]=mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    merchant_id: Mapped[uuid.UUID]=mapped_column(ForeignKey('merchants.id', ondelete='CASCADE'), index=True)
    requested_by_id: Mapped[uuid.UUID|None]=mapped_column(UUID(as_uuid=True), ForeignKey('users.id', ondelete='SET NULL'), nullable=True, index=True)
    processed_by_id: Mapped[uuid.UUID|None]=mapped_column(UUID(as_uuid=True), ForeignKey('users.id', ondelete='SET NULL'), nullable=True, index=True)
    amount_usdt: Mapped[Decimal]=mapped_column(Numeric(18,2))
    fee_usdt: Mapped[Decimal]=mapped_column(Numeric(18,2), default=Decimal('5.00'))
    rate_rub: Mapped[Decimal]=mapped_column(Numeric(18,4), default=Decimal('100.0000'))
    amount_rub: Mapped[Decimal]=mapped_column(Numeric(18,2))
    fee_rub: Mapped[Decimal]=mapped_column(Numeric(18,2))
    total_debit_rub: Mapped[Decimal]=mapped_column(Numeric(18,2))
    trc20_address: Mapped[str]=mapped_column(String(128))
    status: Mapped[str]=mapped_column(String(32), default='pending', index=True)
    reject_reason: Mapped[str|None]=mapped_column(String(500), nullable=True)
    processed_at: Mapped[datetime|None]=mapped_column(DateTime(timezone=True), nullable=True)
    metadata_json: Mapped[dict]=mapped_column(JSON, default=dict)
    merchant=relationship('Merchant', back_populates='settlements')


class TraderLedgerEntry(Base, TimestampMixin):
    __tablename__='trader_ledger_entries'
    id: Mapped[uuid.UUID]=mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    trader_id: Mapped[uuid.UUID]=mapped_column(UUID(as_uuid=True), ForeignKey('users.id', ondelete='CASCADE'), index=True)
    operation_id: Mapped[uuid.UUID|None]=mapped_column(UUID(as_uuid=True), nullable=True, index=True)
    entry_type: Mapped[str]=mapped_column(String(64), index=True)
    amount: Mapped[Decimal]=mapped_column(Numeric(18,2))
    balance_after: Mapped[Decimal]=mapped_column(Numeric(18,2), default=Decimal('0.00'))
    hold_after: Mapped[Decimal]=mapped_column(Numeric(18,2), default=Decimal('0.00'))
    currency: Mapped[str]=mapped_column(String(8), default='RUB')
    description: Mapped[str]=mapped_column(String(500), default='')
    idempotency_key: Mapped[str|None]=mapped_column(String(160), unique=True, nullable=True)


class PlatformLedgerEntry(Base, TimestampMixin):
    __tablename__='platform_ledger_entries'
    id: Mapped[uuid.UUID]=mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    merchant_id: Mapped[uuid.UUID|None]=mapped_column(UUID(as_uuid=True), ForeignKey('merchants.id', ondelete='SET NULL'), nullable=True, index=True)
    operation_id: Mapped[uuid.UUID|None]=mapped_column(UUID(as_uuid=True), nullable=True, index=True)
    entry_type: Mapped[str]=mapped_column(String(64), index=True)
    amount: Mapped[Decimal]=mapped_column(Numeric(18,2))
    currency: Mapped[str]=mapped_column(String(8), default='RUB')
    description: Mapped[str]=mapped_column(String(500), default='')
    idempotency_key: Mapped[str|None]=mapped_column(String(160), unique=True, nullable=True)


class Requisite(Base, TimestampMixin):
    __tablename__='requisites'
    id: Mapped[uuid.UUID]=mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    trader_id: Mapped[uuid.UUID|None]=mapped_column(UUID(as_uuid=True), ForeignKey('users.id', ondelete='SET NULL'), nullable=True, index=True)
    owner_name: Mapped[str]=mapped_column(String(160))
    full_name: Mapped[str|None]=mapped_column(String(160), nullable=True)
    method: Mapped[str]=mapped_column(String(32))
    value_encrypted: Mapped[str]=mapped_column(Text)
    bank_code: Mapped[str|None]=mapped_column(String(64), nullable=True, index=True)
    operator_code: Mapped[str|None]=mapped_column(String(64), nullable=True, index=True)
    bank_name: Mapped[str|None]=mapped_column(String(100), nullable=True)
    automation_id: Mapped[str|None]=mapped_column(String(120), nullable=True)
    last4: Mapped[str|None]=mapped_column(String(4), nullable=True)
    request_count: Mapped[int]=mapped_column(Integer, default=10)
    timeframe: Mapped[str]=mapped_column(String(16), default='час')
    success_delay_minutes: Mapped[int]=mapped_column(Integer, default=0)
    simultaneous_limit: Mapped[int]=mapped_column(Integer, default=1)
    min_check: Mapped[Decimal]=mapped_column(Numeric(18,2), default=Decimal('100.00'))
    max_check: Mapped[Decimal]=mapped_column(Numeric(18,2), default=Decimal('150000.00'))
    enabled: Mapped[bool]=mapped_column(Boolean, default=True)
    daily_limit: Mapped[Decimal]=mapped_column(Numeric(18,2), default=Decimal('0.00'))
    operation_limit: Mapped[int]=mapped_column(Integer, default=0)
    status: Mapped[str]=mapped_column(String(32), default='active')
    usage_count: Mapped[int]=mapped_column(Integer, default=0)
    last_success_at: Mapped[datetime|None]=mapped_column(DateTime(timezone=True), nullable=True)
    traffic_status: Mapped[str]=mapped_column(String(32), default='active', index=True)
    risk_score: Mapped[int]=mapped_column(Integer, default=0)
    failed_in_row: Mapped[int]=mapped_column(Integer, default=0)
    last_payment_at: Mapped[datetime|None]=mapped_column(DateTime(timezone=True), nullable=True)
    auto_paused_at: Mapped[datetime|None]=mapped_column(DateTime(timezone=True), nullable=True)
    limited_until: Mapped[datetime|None]=mapped_column(DateTime(timezone=True), nullable=True)
    limited_max_active_payments: Mapped[int|None]=mapped_column(Integer, nullable=True)
    limited_max_amount: Mapped[Decimal|None]=mapped_column(Numeric(18,2), nullable=True)
    allow_high_amount: Mapped[bool]=mapped_column(Boolean, default=True)
    is_archived: Mapped[bool]=mapped_column(Boolean, default=False, index=True)
    archived_at: Mapped[datetime|None]=mapped_column(DateTime(timezone=True), nullable=True)


class AntiscamGlobalSettings(Base, TimestampMixin):
    __tablename__='antiscam_global_settings'
    id: Mapped[int]=mapped_column(Integer, primary_key=True, default=1)
    antiscam_enabled: Mapped[bool]=mapped_column(Boolean, default=True)
    auto_disable_traffic_enabled: Mapped[bool]=mapped_column(Boolean, default=True)
    failed_payments_in_row_limit_requisite: Mapped[int]=mapped_column(Integer, default=5)
    failed_payments_in_row_limit_trader: Mapped[int]=mapped_column(Integer, default=10)
    min_payments_for_conversion_check: Mapped[int]=mapped_column(Integer, default=20)
    conversion_check_window_minutes: Mapped[int]=mapped_column(Integer, default=60)
    min_requisite_conversion_percent: Mapped[Decimal]=mapped_column(Numeric(5,2), default=Decimal('35.00'))
    min_trader_conversion_percent: Mapped[Decimal]=mapped_column(Numeric(5,2), default=Decimal('45.00'))
    conversion_drop_percent_limit: Mapped[Decimal]=mapped_column(Numeric(5,2), default=Decimal('40.00'))
    max_confirmation_delay_minutes: Mapped[int]=mapped_column(Integer, default=10)
    merchant_complaints_limit_requisite: Mapped[int]=mapped_column(Integer, default=3)
    merchant_complaints_limit_trader: Mapped[int]=mapped_column(Integer, default=5)
    high_amount_extra_risk_enabled: Mapped[bool]=mapped_column(Boolean, default=True)
    high_amount_threshold: Mapped[Decimal]=mapped_column(Numeric(18,2), default=Decimal('100000.00'))
    freeze_withdrawals_on_trader_auto_pause: Mapped[bool]=mapped_column(Boolean, default=True)
    default_reinstate_mode: Mapped[str]=mapped_column(String(32), default='limited')
    limited_reinstate_duration_minutes: Mapped[int]=mapped_column(Integer, default=120)
    limited_reinstate_max_active_payments: Mapped[int]=mapped_column(Integer, default=3)
    limited_reinstate_max_amount: Mapped[Decimal]=mapped_column(Numeric(18,2), default=Decimal('30000.00'))


class TraderAntiscamSettings(Base, TimestampMixin):
    __tablename__='trader_antiscam_settings'
    id: Mapped[uuid.UUID]=mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    trader_id: Mapped[uuid.UUID]=mapped_column(UUID(as_uuid=True), ForeignKey('users.id', ondelete='CASCADE'), unique=True, index=True)
    use_global_antiscam_settings: Mapped[bool]=mapped_column(Boolean, default=True)
    antiscam_enabled: Mapped[bool]=mapped_column(Boolean, default=True)
    failed_payments_in_row_limit: Mapped[int]=mapped_column(Integer, default=10)
    min_conversion_percent: Mapped[Decimal]=mapped_column(Numeric(5,2), default=Decimal('45.00'))
    conversion_check_window_minutes: Mapped[int]=mapped_column(Integer, default=60)
    conversion_drop_percent_limit: Mapped[Decimal]=mapped_column(Numeric(5,2), default=Decimal('40.00'))
    max_confirmation_delay_minutes: Mapped[int]=mapped_column(Integer, default=10)
    max_active_payments_when_risky: Mapped[int]=mapped_column(Integer, default=3)
    allow_high_amount_traffic: Mapped[bool]=mapped_column(Boolean, default=False)
    risk_level: Mapped[str]=mapped_column(String(32), default='strict')
    auto_disable_requisites_enabled: Mapped[bool]=mapped_column(Boolean, default=True)
    auto_disable_trader_enabled: Mapped[bool]=mapped_column(Boolean, default=True)
    freeze_withdrawals_on_auto_pause: Mapped[bool]=mapped_column(Boolean, default=True)


class RequisiteAntiscamSettings(Base, TimestampMixin):
    __tablename__='requisite_antiscam_settings'
    id: Mapped[uuid.UUID]=mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    requisite_id: Mapped[uuid.UUID]=mapped_column(UUID(as_uuid=True), ForeignKey('requisites.id', ondelete='CASCADE'), unique=True, index=True)
    use_global_antiscam_settings: Mapped[bool]=mapped_column(Boolean, default=True)
    antiscam_enabled: Mapped[bool]=mapped_column(Boolean, default=True)
    failed_payments_in_row_limit: Mapped[int]=mapped_column(Integer, default=5)
    min_conversion_percent: Mapped[Decimal]=mapped_column(Numeric(5,2), default=Decimal('35.00'))
    conversion_check_window_minutes: Mapped[int]=mapped_column(Integer, default=60)
    max_confirmation_delay_minutes: Mapped[int]=mapped_column(Integer, default=10)
    allow_high_amount_traffic: Mapped[bool]=mapped_column(Boolean, default=True)
    limited_max_active_payments: Mapped[int]=mapped_column(Integer, default=3)
    limited_max_amount: Mapped[Decimal]=mapped_column(Numeric(18,2), default=Decimal('30000.00'))

class Appeal(Base, TimestampMixin):
    __tablename__='appeals'
    id: Mapped[uuid.UUID]=mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    operation_type: Mapped[str]=mapped_column(String(32))
    operation_id: Mapped[uuid.UUID]=mapped_column(UUID(as_uuid=True), index=True)
    status: Mapped[str]=mapped_column(String(32), default=AppealStatus.opened.value)
    created_by: Mapped[uuid.UUID|None]=mapped_column(UUID(as_uuid=True), ForeignKey('users.id'), nullable=True)
    decision: Mapped[str|None]=mapped_column(String(64), nullable=True)
    metadata_json: Mapped[dict]=mapped_column(JSON, default=dict)

class AppealMessage(Base, TimestampMixin):
    __tablename__='appeal_messages'
    id: Mapped[uuid.UUID]=mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    appeal_id: Mapped[uuid.UUID]=mapped_column(ForeignKey('appeals.id', ondelete='CASCADE'), index=True)
    author_id: Mapped[uuid.UUID|None]=mapped_column(UUID(as_uuid=True), ForeignKey('users.id'), nullable=True)
    message: Mapped[str]=mapped_column(Text)
    attachment_path: Mapped[str|None]=mapped_column(String(500), nullable=True)

class SmsMessage(Base, TimestampMixin):
    __tablename__='sms_messages'
    id: Mapped[uuid.UUID]=mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    provider_message_id: Mapped[str]=mapped_column(String(128), unique=True, index=True)
    sender: Mapped[str]=mapped_column(String(128))
    body: Mapped[str]=mapped_column(Text)
    parsed_amount: Mapped[Decimal|None]=mapped_column(Numeric(18,2), nullable=True)
    message_hash: Mapped[str|None]=mapped_column(String(64), unique=True, nullable=True)
    parse_confidence: Mapped[Decimal|None]=mapped_column(Numeric(5,2), nullable=True)
    review_required: Mapped[bool]=mapped_column(Boolean, default=False)
    linked_deposit_id: Mapped[uuid.UUID|None]=mapped_column(UUID(as_uuid=True), ForeignKey('deposits.id'), nullable=True)
    processed: Mapped[bool]=mapped_column(Boolean, default=False)

class WebhookEvent(Base, TimestampMixin):
    __tablename__='webhook_events'
    id: Mapped[uuid.UUID]=mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    merchant_id: Mapped[uuid.UUID]=mapped_column(ForeignKey('merchants.id', ondelete='CASCADE'), index=True)
    event_type: Mapped[str]=mapped_column(String(64))
    payload: Mapped[dict]=mapped_column(JSON)
    status: Mapped[str]=mapped_column(String(32), default='queued')
    attempts: Mapped[int]=mapped_column(Integer, default=0)
    last_error: Mapped[str|None]=mapped_column(Text, nullable=True)
    last_status_code: Mapped[int|None]=mapped_column(Integer, nullable=True)
    response_snippet: Mapped[str|None]=mapped_column(Text, nullable=True)
    next_retry_at: Mapped[datetime|None]=mapped_column(DateTime(timezone=True), nullable=True)


class WebhookDeliveryAttempt(Base, TimestampMixin):
    __tablename__='webhook_delivery_attempts'
    id: Mapped[uuid.UUID]=mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    webhook_event_id: Mapped[uuid.UUID]=mapped_column(UUID(as_uuid=True), ForeignKey('webhook_events.id', ondelete='CASCADE'), index=True)
    attempt_no: Mapped[int]=mapped_column(Integer, default=1)
    status: Mapped[str]=mapped_column(String(32), default='pending', index=True)
    status_code: Mapped[int|None]=mapped_column(Integer, nullable=True)
    error: Mapped[str|None]=mapped_column(Text, nullable=True)
    response_snippet: Mapped[str|None]=mapped_column(Text, nullable=True)


class ApiReplayNonce(Base, TimestampMixin):
    __tablename__='api_replay_nonces'
    id: Mapped[uuid.UUID]=mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    api_key_id: Mapped[uuid.UUID]=mapped_column(UUID(as_uuid=True), ForeignKey('api_keys.id', ondelete='CASCADE'), index=True)
    signature: Mapped[str]=mapped_column(String(128), index=True)
    request_hash: Mapped[str]=mapped_column(String(64), index=True)
    timestamp: Mapped[int]=mapped_column(Integer, index=True)
    expires_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), index=True)
    __table_args__=(UniqueConstraint('api_key_id','signature','request_hash', name='uq_api_replay_signature_hash'),)

class ApiRequestLog(Base, TimestampMixin):
    __tablename__='api_request_logs'
    id: Mapped[uuid.UUID]=mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    merchant_id: Mapped[uuid.UUID|None]=mapped_column(UUID(as_uuid=True), nullable=True, index=True)
    method: Mapped[str]=mapped_column(String(16))
    path: Mapped[str]=mapped_column(String(500))
    ip: Mapped[str]=mapped_column(String(64))
    status_code: Mapped[int]=mapped_column(Integer, default=0)
    request_id: Mapped[str]=mapped_column(String(64), index=True)

class AggregatorAccount(Base, TimestampMixin):
    __tablename__='aggregator_accounts'
    id: Mapped[uuid.UUID]=mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    platform_merchant_id: Mapped[uuid.UUID]=mapped_column(UUID(as_uuid=True), ForeignKey('merchants.id', ondelete='RESTRICT'), index=True)
    name: Mapped[str]=mapped_column(String(160), unique=True, index=True)
    status: Mapped[str]=mapped_column(String(32), default='active', index=True)
    api_key: Mapped[str]=mapped_column(String(80), unique=True, index=True)
    secret_hash: Mapped[str]=mapped_column(String(255))
    callback_url: Mapped[str|None]=mapped_column(String(500), nullable=True)
    success_url: Mapped[str|None]=mapped_column(String(500), nullable=True)
    fail_url: Mapped[str|None]=mapped_column(String(500), nullable=True)
    commission_percent: Mapped[Decimal]=mapped_column(Numeric(8,4), default=Decimal('0.00'))
    min_payment_amount: Mapped[Decimal]=mapped_column(Numeric(18,2), default=Decimal('100.00'))
    max_payment_amount: Mapped[Decimal]=mapped_column(Numeric(18,2), default=Decimal('150000.00'))
    daily_limit: Mapped[Decimal|None]=mapped_column(Numeric(18,2), nullable=True)
    monthly_limit: Mapped[Decimal|None]=mapped_column(Numeric(18,2), nullable=True)
    balance: Mapped[Decimal]=mapped_column(Numeric(18,2), default=Decimal('0.00'))
    hold_balance: Mapped[Decimal]=mapped_column(Numeric(18,2), default=Decimal('0.00'))
    total_turnover: Mapped[Decimal]=mapped_column(Numeric(18,2), default=Decimal('0.00'))
    is_archived: Mapped[bool]=mapped_column(Boolean, default=False, index=True)
    archived_at: Mapped[datetime|None]=mapped_column(DateTime(timezone=True), nullable=True)


class AggregatorMerchant(Base, TimestampMixin):
    __tablename__='aggregator_merchants'
    id: Mapped[uuid.UUID]=mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    aggregator_id: Mapped[uuid.UUID]=mapped_column(UUID(as_uuid=True), ForeignKey('aggregator_accounts.id', ondelete='CASCADE'), index=True)
    external_merchant_id: Mapped[str]=mapped_column(String(128), index=True)
    merchant_name: Mapped[str]=mapped_column(String(160))
    merchant_callback_url: Mapped[str|None]=mapped_column(String(500), nullable=True)
    merchant_secret_hash: Mapped[str]=mapped_column(String(255))
    status: Mapped[str]=mapped_column(String(32), default='active', index=True)
    commission_percent: Mapped[Decimal|None]=mapped_column(Numeric(8,4), nullable=True)
    is_archived: Mapped[bool]=mapped_column(Boolean, default=False, index=True)
    archived_at: Mapped[datetime|None]=mapped_column(DateTime(timezone=True), nullable=True)
    __table_args__=(UniqueConstraint('aggregator_id','external_merchant_id', name='uq_aggregator_merchants_external'),)


class AggregatorPayment(Base, TimestampMixin):
    __tablename__='aggregator_payments'
    id: Mapped[uuid.UUID]=mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    aggregator_id: Mapped[uuid.UUID]=mapped_column(UUID(as_uuid=True), ForeignKey('aggregator_accounts.id', ondelete='CASCADE'), index=True)
    aggregator_merchant_id: Mapped[uuid.UUID|None]=mapped_column(UUID(as_uuid=True), ForeignKey('aggregator_merchants.id', ondelete='SET NULL'), nullable=True, index=True)
    merchant_order_id: Mapped[str]=mapped_column(String(128), index=True)
    aggregator_order_id: Mapped[str]=mapped_column(String(128), index=True)
    platform_payment_id: Mapped[uuid.UUID]=mapped_column(UUID(as_uuid=True), ForeignKey('deposits.id', ondelete='RESTRICT'), unique=True, index=True)
    amount: Mapped[Decimal]=mapped_column(Numeric(18,2))
    currency: Mapped[str]=mapped_column(String(8), default='RUB')
    payment_method: Mapped[str]=mapped_column(String(32))
    status: Mapped[str]=mapped_column(String(32), default='created', index=True)
    client_id: Mapped[str|None]=mapped_column(String(128), nullable=True)
    client_ip: Mapped[str|None]=mapped_column(String(64), nullable=True)
    callback_url_to_merchant: Mapped[str|None]=mapped_column(String(500), nullable=True)
    callback_status: Mapped[str]=mapped_column(String(32), default='pending', index=True)
    callback_attempts: Mapped[int]=mapped_column(Integer, default=0)
    last_callback_error: Mapped[str|None]=mapped_column(Text, nullable=True)
    expires_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), nullable=False)
    paid_at: Mapped[datetime|None]=mapped_column(DateTime(timezone=True), nullable=True)
    metadata_json: Mapped[dict]=mapped_column(JSON, default=dict)
    __table_args__=(
        UniqueConstraint('aggregator_id','aggregator_order_id', name='uq_aggregator_payments_aggregator_order'),
        UniqueConstraint('aggregator_id','merchant_order_id', name='uq_aggregator_payments_merchant_order'),
    )


class AggregatorCallbackLog(Base, TimestampMixin):
    __tablename__='aggregator_callback_logs'
    id: Mapped[uuid.UUID]=mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    direction: Mapped[str]=mapped_column(String(32), index=True)
    related_payment_id: Mapped[uuid.UUID]=mapped_column(UUID(as_uuid=True), ForeignKey('aggregator_payments.id', ondelete='CASCADE'), index=True)
    target_url: Mapped[str]=mapped_column(String(500))
    payload_json: Mapped[dict]=mapped_column(JSON, default=dict)
    response_status_code: Mapped[int|None]=mapped_column(Integer, nullable=True)
    response_body: Mapped[str|None]=mapped_column(Text, nullable=True)
    status: Mapped[str]=mapped_column(String(32), default='pending', index=True)
    attempt: Mapped[int]=mapped_column(Integer, default=0)
    error_message: Mapped[str|None]=mapped_column(Text, nullable=True)
    next_retry_at: Mapped[datetime|None]=mapped_column(DateTime(timezone=True), nullable=True)
    sent_at: Mapped[datetime|None]=mapped_column(DateTime(timezone=True), nullable=True)


class AggregatorReplayNonce(Base, TimestampMixin):
    __tablename__='aggregator_replay_nonces'
    id: Mapped[uuid.UUID]=mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    aggregator_id: Mapped[uuid.UUID]=mapped_column(UUID(as_uuid=True), ForeignKey('aggregator_accounts.id', ondelete='CASCADE'), index=True)
    signature: Mapped[str]=mapped_column(String(128), index=True)
    request_hash: Mapped[str]=mapped_column(String(64), index=True)
    timestamp: Mapped[int]=mapped_column(Integer, index=True)
    expires_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), index=True)
    __table_args__=(UniqueConstraint('aggregator_id','signature','request_hash', name='uq_aggregator_replay_signature_hash'),)

class AuditLog(Base, TimestampMixin):
    __tablename__='audit_logs'
    id: Mapped[uuid.UUID]=mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    actor_id: Mapped[uuid.UUID|None]=mapped_column(UUID(as_uuid=True), nullable=True, index=True)
    action: Mapped[str]=mapped_column(String(160), index=True)
    target_type: Mapped[str]=mapped_column(String(80))
    target_id: Mapped[str|None]=mapped_column(String(128), nullable=True)
    ip: Mapped[str|None]=mapped_column(String(64), nullable=True)
    details: Mapped[dict]=mapped_column(JSON, default=dict)

class FeeRule(Base, TimestampMixin):
    __tablename__='fee_rules'
    id: Mapped[uuid.UUID]=mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    merchant_id: Mapped[uuid.UUID|None]=mapped_column(UUID(as_uuid=True), nullable=True, index=True)
    method: Mapped[str]=mapped_column(String(32))
    percent: Mapped[Decimal]=mapped_column(Numeric(8,4), default=Decimal('0'))
    fixed: Mapped[Decimal]=mapped_column(Numeric(18,2), default=Decimal('0'))
    entity_type: Mapped[str]=mapped_column(String(32), default='merchant', index=True)
    entity_id: Mapped[uuid.UUID]=mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    fee_side: Mapped[str]=mapped_column(String(32), default='merchant_fee', index=True)
    payment_method: Mapped[str]=mapped_column(String(32), default='sbp', index=True)
    currency: Mapped[str]=mapped_column(String(8), default='RUB', index=True)
    min_amount: Mapped[Decimal]=mapped_column(Numeric(18,2), default=Decimal('0.00'))
    max_amount: Mapped[Decimal|None]=mapped_column(Numeric(18,2), nullable=True)
    rate_percent: Mapped[Decimal]=mapped_column(Numeric(8,4), default=Decimal('0.0000'))
    effective_from: Mapped[datetime]=mapped_column(DateTime(timezone=True))
    effective_to: Mapped[datetime|None]=mapped_column(DateTime(timezone=True), nullable=True)
    is_active: Mapped[bool]=mapped_column(Boolean, default=True, index=True)
    version: Mapped[int]=mapped_column(Integer, default=1)
    created_by: Mapped[uuid.UUID|None]=mapped_column(UUID(as_uuid=True), nullable=True)
    updated_by: Mapped[uuid.UUID|None]=mapped_column(UUID(as_uuid=True), nullable=True)
    __table_args__=(
        CheckConstraint('min_amount >= 0', name='ck_fee_rules_min_amount_nonnegative'),
        CheckConstraint('max_amount IS NULL OR max_amount > min_amount', name='ck_fee_rules_amount_range'),
        CheckConstraint('rate_percent >= 0 AND rate_percent <= 100', name='ck_fee_rules_rate_percent'),
        CheckConstraint('effective_to IS NULL OR effective_to > effective_from', name='ck_fee_rules_effective_period'),
        Index(
            'ix_fee_rules_lookup',
            'entity_type', 'entity_id', 'fee_side', 'payment_method', 'currency',
            'is_active', 'effective_from',
        ),
    )


class OperationFeeSnapshot(Base, TimestampMixin):
    __tablename__='operation_fee_snapshots'
    id: Mapped[uuid.UUID]=mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    deposit_id: Mapped[uuid.UUID]=mapped_column(UUID(as_uuid=True), ForeignKey('deposits.id', ondelete='RESTRICT'), unique=True, index=True)
    merchant_id: Mapped[uuid.UUID]=mapped_column(UUID(as_uuid=True), ForeignKey('merchants.id', ondelete='RESTRICT'), nullable=False, index=True)
    merchant_rate_rule_id: Mapped[uuid.UUID]=mapped_column(UUID(as_uuid=True), ForeignKey('fee_rules.id', ondelete='RESTRICT'), nullable=False)
    merchant_rate_version: Mapped[int]=mapped_column(Integer)
    merchant_rate_percent: Mapped[Decimal]=mapped_column(Numeric(8,4))
    merchant_fee_amount: Mapped[Decimal]=mapped_column(Numeric(18,2))
    executor_type: Mapped[str]=mapped_column(String(32), index=True)
    executor_id: Mapped[uuid.UUID]=mapped_column(UUID(as_uuid=True), index=True)
    executor_rate_rule_id: Mapped[uuid.UUID]=mapped_column(UUID(as_uuid=True), ForeignKey('fee_rules.id', ondelete='RESTRICT'), nullable=False)
    executor_rate_version: Mapped[int]=mapped_column(Integer)
    executor_rate_percent: Mapped[Decimal]=mapped_column(Numeric(8,4))
    executor_fee_amount: Mapped[Decimal]=mapped_column(Numeric(18,2))
    platform_margin_percent: Mapped[Decimal]=mapped_column(Numeric(8,4))
    platform_income_amount: Mapped[Decimal]=mapped_column(Numeric(18,2))
    calculation_base_amount: Mapped[Decimal]=mapped_column(Numeric(18,2))
    currency: Mapped[str]=mapped_column(String(8), index=True)
    payment_method: Mapped[str]=mapped_column(String(32), index=True)
    rate_snapshot_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), index=True)
    settlement_status: Mapped[str]=mapped_column(String(32), default='pending', index=True)
    settled_at: Mapped[datetime|None]=mapped_column(DateTime(timezone=True), nullable=True)
    ledger_reference: Mapped[uuid.UUID|None]=mapped_column(UUID(as_uuid=True), nullable=True)
    __table_args__=(
        CheckConstraint('merchant_rate_percent >= executor_rate_percent', name='ck_operation_snapshot_nonnegative_margin'),
        CheckConstraint('merchant_fee_amount = executor_fee_amount + platform_income_amount', name='ck_operation_snapshot_fee_invariant'),
        CheckConstraint("executor_type IN ('trader','aggregator')", name='ck_operation_snapshot_executor_type'),
    )


@event.listens_for(OperationFeeSnapshot, 'before_update')
def protect_operation_fee_snapshot(mapper, connection, target):
    allowed = {'settlement_status', 'settled_at', 'ledger_reference', 'updated_at'}
    changed = {
        attribute.key
        for attribute in inspect(target).attrs
        if attribute.history.has_changes()
    }
    forbidden = changed.difference(allowed)
    if forbidden:
        raise ValueError(
            'operation fee snapshot is immutable: ' + ', '.join(sorted(forbidden))
        )

class LimitRule(Base, TimestampMixin):
    __tablename__='limit_rules'
    id: Mapped[uuid.UUID]=mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    merchant_id: Mapped[uuid.UUID|None]=mapped_column(UUID(as_uuid=True), nullable=True, index=True)
    method: Mapped[str|None]=mapped_column(String(32), nullable=True)
    min_amount: Mapped[Decimal]=mapped_column(Numeric(18,2), default=Decimal('0'))
    max_amount: Mapped[Decimal]=mapped_column(Numeric(18,2), default=Decimal('1000000'))
    daily_amount: Mapped[Decimal]=mapped_column(Numeric(18,2), default=Decimal('0'))

class Blacklist(Base, TimestampMixin):
    __tablename__='blacklist'
    id: Mapped[uuid.UUID]=mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    kind: Mapped[str]=mapped_column(String(32), index=True)
    value: Mapped[str]=mapped_column(String(255), index=True)
    reason: Mapped[str]=mapped_column(String(500), default='')
    is_active: Mapped[bool]=mapped_column(Boolean, default=True)
    __table_args__=(UniqueConstraint('kind','value', name='uq_blacklist_kind_value'),)

class RequisiteUsage(Base, TimestampMixin):
    __tablename__='requisite_usage'
    id: Mapped[uuid.UUID]=mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    requisite_id: Mapped[uuid.UUID]=mapped_column(UUID(as_uuid=True), ForeignKey('requisites.id', ondelete='CASCADE'), index=True)
    operation_type: Mapped[str]=mapped_column(String(32))
    operation_id: Mapped[uuid.UUID]=mapped_column(UUID(as_uuid=True), index=True)
    amount: Mapped[Decimal]=mapped_column(Numeric(18,2))
    status: Mapped[str]=mapped_column(String(32), default='created')

class KycProfile(Base, TimestampMixin):
    __tablename__='kyc_profiles'
    id: Mapped[uuid.UUID]=mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    merchant_id: Mapped[uuid.UUID]=mapped_column(ForeignKey('merchants.id', ondelete='CASCADE'), unique=True, index=True)
    status: Mapped[str]=mapped_column(String(32), default='not_started', index=True)
    legal_name: Mapped[str|None]=mapped_column(String(255), nullable=True)
    tax_id_encrypted: Mapped[str|None]=mapped_column(Text, nullable=True)
    country: Mapped[str]=mapped_column(String(2), default='RU')
    risk_level: Mapped[str]=mapped_column(String(32), default='standard')
    reviewed_by: Mapped[uuid.UUID|None]=mapped_column(UUID(as_uuid=True), ForeignKey('users.id'), nullable=True)
    reviewed_at: Mapped[datetime|None]=mapped_column(DateTime(timezone=True), nullable=True)

class RiskEvent(Base, TimestampMixin):
    __tablename__='risk_events'
    id: Mapped[uuid.UUID]=mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    merchant_id: Mapped[uuid.UUID|None]=mapped_column(UUID(as_uuid=True), ForeignKey('merchants.id', ondelete='CASCADE'), nullable=True, index=True)
    operation_type: Mapped[str]=mapped_column(String(32), index=True)
    operation_id: Mapped[uuid.UUID|None]=mapped_column(UUID(as_uuid=True), nullable=True, index=True)
    score: Mapped[int]=mapped_column(Integer, default=0)
    decision: Mapped[str]=mapped_column(String(32), default='allow', index=True)
    reason: Mapped[str]=mapped_column(String(500), default='')
    details: Mapped[dict]=mapped_column(JSON, default=dict)
    source: Mapped[str]=mapped_column(String(32), default='risk', index=True)
    target_type: Mapped[str|None]=mapped_column(String(32), nullable=True, index=True)
    target_id: Mapped[uuid.UUID|None]=mapped_column(UUID(as_uuid=True), nullable=True, index=True)
    trader_id: Mapped[uuid.UUID|None]=mapped_column(UUID(as_uuid=True), ForeignKey('users.id', ondelete='SET NULL'), nullable=True, index=True)
    requisite_id: Mapped[uuid.UUID|None]=mapped_column(UUID(as_uuid=True), ForeignKey('requisites.id', ondelete='SET NULL'), nullable=True, index=True)
    severity: Mapped[str|None]=mapped_column(String(32), nullable=True, index=True)
    old_status: Mapped[str|None]=mapped_column(String(32), nullable=True)
    new_status: Mapped[str|None]=mapped_column(String(32), nullable=True)
    risk_score_before: Mapped[int|None]=mapped_column(Integer, nullable=True)
    risk_score_after: Mapped[int|None]=mapped_column(Integer, nullable=True)
    payments_count_in_window: Mapped[int|None]=mapped_column(Integer, nullable=True)
    successful_count_in_window: Mapped[int|None]=mapped_column(Integer, nullable=True)
    failed_count_in_window: Mapped[int|None]=mapped_column(Integer, nullable=True)
    conversion_before: Mapped[Decimal|None]=mapped_column(Numeric(5,2), nullable=True)
    conversion_after: Mapped[Decimal|None]=mapped_column(Numeric(5,2), nullable=True)
    window_minutes: Mapped[int|None]=mapped_column(Integer, nullable=True)
    amount_at_risk: Mapped[Decimal|None]=mapped_column(Numeric(18,2), nullable=True)
    auto_action_taken: Mapped[str|None]=mapped_column(String(64), nullable=True, index=True)
    resolved_at: Mapped[datetime|None]=mapped_column(DateTime(timezone=True), nullable=True)
    resolved_by: Mapped[uuid.UUID|None]=mapped_column(UUID(as_uuid=True), ForeignKey('users.id', ondelete='SET NULL'), nullable=True, index=True)
    resolution_status: Mapped[str|None]=mapped_column(String(32), nullable=True, index=True)
    resolution_comment: Mapped[str|None]=mapped_column(Text, nullable=True)


class RiskDecisionRecord(Base, TimestampMixin):
    __tablename__='risk_decisions'
    id: Mapped[uuid.UUID]=mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    risk_event_id: Mapped[uuid.UUID|None]=mapped_column(UUID(as_uuid=True), ForeignKey('risk_events.id', ondelete='SET NULL'), nullable=True, index=True)
    actor_id: Mapped[uuid.UUID|None]=mapped_column(UUID(as_uuid=True), ForeignKey('users.id', ondelete='SET NULL'), nullable=True, index=True)
    target_type: Mapped[str]=mapped_column(String(32), index=True)
    target_id: Mapped[uuid.UUID]=mapped_column(UUID(as_uuid=True), index=True)
    trader_id: Mapped[uuid.UUID|None]=mapped_column(UUID(as_uuid=True), ForeignKey('users.id', ondelete='SET NULL'), nullable=True, index=True)
    requisite_id: Mapped[uuid.UUID|None]=mapped_column(UUID(as_uuid=True), ForeignKey('requisites.id', ondelete='SET NULL'), nullable=True, index=True)
    decision: Mapped[str]=mapped_column(String(64), index=True)
    decision_reason: Mapped[str]=mapped_column(Text)
    proofs_checked: Mapped[bool]=mapped_column(Boolean, default=False)
    proof_source: Mapped[str]=mapped_column(String(160), default='Telegram рабочая группа')
    proof_reference: Mapped[str|None]=mapped_column(String(500), nullable=True)
    reinstate_mode: Mapped[str|None]=mapped_column(String(32), nullable=True)
    old_status: Mapped[str|None]=mapped_column(String(32), nullable=True)
    new_status: Mapped[str|None]=mapped_column(String(32), nullable=True)
    risk_score_before: Mapped[int|None]=mapped_column(Integer, nullable=True)
    risk_score_after: Mapped[int|None]=mapped_column(Integer, nullable=True)
    limited_until: Mapped[datetime|None]=mapped_column(DateTime(timezone=True), nullable=True)
    max_active_payments: Mapped[int|None]=mapped_column(Integer, nullable=True)
    max_payment_amount: Mapped[Decimal|None]=mapped_column(Numeric(18,2), nullable=True)
    allow_high_amount: Mapped[bool]=mapped_column(Boolean, default=False)

class ReportExport(Base, TimestampMixin):
    __tablename__='report_exports'
    id: Mapped[uuid.UUID]=mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    actor_id: Mapped[uuid.UUID|None]=mapped_column(UUID(as_uuid=True), ForeignKey('users.id'), nullable=True, index=True)
    report_type: Mapped[str]=mapped_column(String(64), index=True)
    status: Mapped[str]=mapped_column(String(32), default='completed')
    file_path: Mapped[str|None]=mapped_column(String(500), nullable=True)
    filters: Mapped[dict]=mapped_column(JSON, default=dict)


def _encrypt_sensitive_attribute(target, value, oldvalue, initiator):
    del target, oldvalue, initiator
    return encrypt_secret(value)


for _sensitive_attribute in (
    User.twofa_secret,
    ApiKey.secret_hash,
    Requisite.value_encrypted,
    AggregatorAccount.secret_hash,
    AggregatorMerchant.merchant_secret_hash,
    KycProfile.tax_id_encrypted,
):
    event.listen(_sensitive_attribute, 'set', _encrypt_sensitive_attribute, retval=True)
