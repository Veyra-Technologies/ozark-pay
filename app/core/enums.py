from enum import StrEnum


class Role(StrEnum):
    superadmin = 'superadmin'
    admin = 'admin'
    support = 'support'
    merchant = 'merchant'
    aggregator = 'aggregator'
    operator = 'operator'
    # Backward compatibility with older demo builds where operators were named traders.
    trader = 'trader'


class DepositStatus(StrEnum):
    created = 'created'
    pending = 'pending'
    paid = 'paid'
    failed = 'failed'
    expired = 'expired'
    cancelled = 'cancelled'
    appeal_opened = 'appeal_opened'


class PayoutStatus(StrEnum):
    created = 'created'
    pending = 'pending'
    processing = 'processing'
    completed = 'completed'
    failed = 'failed'
    cancelled = 'cancelled'
    rejected = 'rejected'
    appeal_opened = 'appeal_opened'


class PaymentMethod(StrEnum):
    sbp = 'sbp'
    c2c = 'c2c'
    mobile_commerce = 'mobile_commerce'
    mobile = 'mobile'


class AppealStatus(StrEnum):
    opened = 'opened'
    in_review = 'in_review'
    approved = 'approved'
    rejected = 'rejected'
    returned_to_processing = 'returned_to_processing'
    closed = 'closed'


class LedgerType(StrEnum):
    credit = 'credit'
    debit = 'debit'
    hold = 'hold'
    release = 'release'
    fee = 'fee'


class BlacklistKind(StrEnum):
    ip = 'ip'
    requisite = 'requisite'
    card = 'card'
    phone = 'phone'
    merchant = 'merchant'


class RiskDecision(StrEnum):
    allow = 'allow'
    review = 'review'
    deny = 'deny'


class TrafficStatus(StrEnum):
    active = 'active'
    auto_paused = 'auto_paused'
    manual_paused = 'manual_paused'
    under_review = 'under_review'
    reinstated_limited = 'reinstated_limited'
    reinstated_full = 'reinstated_full'
    blocked = 'blocked'


class RiskSeverity(StrEnum):
    warning = 'warning'
    medium = 'medium'
    high = 'high'
    critical = 'critical'


class RiskAutoAction(StrEnum):
    none = 'none'
    reduce_traffic = 'reduce_traffic'
    pause_requisite = 'pause_requisite'
    pause_trader = 'pause_trader'
    freeze_withdrawals = 'freeze_withdrawals'


class KycStatus(StrEnum):
    not_started = 'not_started'
    pending = 'pending'
    in_review = 'in_review'
    approved = 'approved'
    rejected = 'rejected'
    resubmit_required = 'resubmit_required'
