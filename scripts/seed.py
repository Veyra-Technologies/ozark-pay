import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import asyncio
import os
import secrets
from decimal import Decimal
import pyotp
from sqlalchemy import select
from app.db.session import AsyncSessionLocal
from app.models import ApiKey, Balance, FeeRule, KycProfile, LimitRule, Merchant, Requisite, User
from app.core.config import settings
from app.core.security import encrypt_secret, hash_password, is_unreadable_encrypted_text, reveal_text
from app.core.enums import Role

async def ensure_user(db, email: str, password: str, role: Role, twofa: bool = False, secret: str | None = None, sync_credentials: bool = False):
    user=(await db.execute(select(User).where(User.email==email))).scalar_one_or_none()
    if not user:
        user=User(email=email,password_hash=hash_password(password),role=role.value,twofa_secret=encrypt_secret(secret),twofa_enabled=twofa)
        db.add(user); await db.flush()
    elif sync_credentials:
        user.password_hash = hash_password(password)
        user.role = role.value
        user.twofa_secret = encrypt_secret(secret)
        user.twofa_enabled = twofa
        user.is_active = True
        user.is_locked = False
        user.failed_login_count = 0
    return user

async def ensure_demo_merchant(db, owner):
    if settings.is_production:
        raise RuntimeError('Demo merchants and sandbox API keys cannot be seeded in production')
    merchant=(await db.execute(select(Merchant).where(Merchant.owner_id==owner.id))).scalar_one_or_none()
    if not merchant:
        merchant=Merchant(owner_id=owner.id, name='Demo Casino Merchant', webhook_url=None, ip_whitelist=[], sandbox_mode=True)
        db.add(merchant); await db.flush()
    api_key=(await db.execute(select(ApiKey).where(ApiKey.merchant_id==merchant.id).limit(1))).scalar_one_or_none()
    if not api_key:
        demo_api_key = os.getenv('DEMO_MERCHANT_API_KEY', 'pk_demo_' + secrets.token_urlsafe(18))
        demo_secret = os.getenv('DEMO_MERCHANT_SECRET', secrets.token_urlsafe(32))
        db.add(ApiKey(merchant_id=merchant.id, api_key=demo_api_key, secret_hash=encrypt_secret(demo_secret), mode='sandbox'))
    balance=(await db.execute(select(Balance).where(Balance.merchant_id==merchant.id, Balance.currency=='RUB'))).scalar_one_or_none()
    if not balance:
        db.add(Balance(merchant_id=merchant.id, currency='RUB', available=Decimal('100000.00'), frozen=Decimal('0.00')))
    kyc=(await db.execute(select(KycProfile).where(KycProfile.merchant_id==merchant.id))).scalar_one_or_none()
    if not kyc:
        db.add(KycProfile(merchant_id=merchant.id, status='pending', legal_name='Demo Merchant LLC', country='RU', risk_level='standard'))
    if not (await db.execute(select(FeeRule).where(FeeRule.merchant_id==merchant.id).limit(1))).scalar_one_or_none():
        db.add(FeeRule(merchant_id=merchant.id, method='sbp', percent=Decimal('1.5000'), fixed=Decimal('0.00')))
        db.add(FeeRule(merchant_id=merchant.id, method='c2c', percent=Decimal('2.0000'), fixed=Decimal('10.00')))
        db.add(FeeRule(merchant_id=merchant.id, method='mobile', percent=Decimal('3.0000'), fixed=Decimal('0.00')))
    if not (await db.execute(select(LimitRule).where(LimitRule.merchant_id==merchant.id).limit(1))).scalar_one_or_none():
        db.add(LimitRule(merchant_id=merchant.id, method=None, min_amount=Decimal('100.00'), max_amount=Decimal('150000.00'), daily_amount=Decimal('1000000.00')))
    return merchant

async def normalize_requisites_plaintext(db):
    rows = (await db.execute(select(Requisite))).scalars().all()
    for req in rows:
        visible_value = reveal_text(req.value_encrypted).strip()
        if visible_value and visible_value != req.value_encrypted:
            req.value_encrypted = encrypt_secret(visible_value)
        elif is_unreadable_encrypted_text(req.value_encrypted):
            req.enabled = False
            if req.status != 'deleted':
                req.status = 'review'


async def ensure_demo_requisite(db, operator):
    requisite=(await db.execute(select(Requisite).where(Requisite.automation_id=='demo-auto-001'))).scalar_one_or_none()
    values = {
        'trader_id': operator.id,
        'owner_name': 'Demo Operator Receiver',
        'full_name': 'Ivanov Ivan Ivanovich',
        'method': 'sbp',
        'bank_name': 'Test Bank',
        'automation_id': 'demo-auto-001',
        'last4': '0000',
        'request_count': 100,
        'simultaneous_limit': 100,
        'daily_limit': Decimal('500000.00'),
        'operation_limit': 1000,
        'status': 'active',
        'enabled': True,
        'min_check': Decimal('100.00'),
        'max_check': Decimal('150000.00'),
        'value_encrypted': encrypt_secret('+79990000000'),
    }
    if not requisite:
        db.add(Requisite(**values))
        return
    for key, value in values.items():
        setattr(requisite, key, value)

async def main():
    async with AsyncSessionLocal() as db:
        await ensure_user(db, settings.SUPERADMIN_EMAIL, settings.SUPERADMIN_PASSWORD, Role.superadmin, True, settings.SUPERADMIN_2FA_SECRET, sync_credentials=True)
        await normalize_requisites_plaintext(db)
        if not settings.SEED_DEMO_DATA:
            await db.commit()
            return
        if settings.is_production:
            raise RuntimeError('SEED_DEMO_DATA must be false in production')
        await ensure_user(db, os.getenv('DEMO_ADMIN_EMAIL', 'admin@example.com'), os.getenv('DEMO_ADMIN_PASSWORD', secrets.token_urlsafe(24)), Role.admin, True, os.getenv('DEMO_ADMIN_2FA_SECRET', pyotp.random_base32()))
        await ensure_user(db, os.getenv('DEMO_SUPPORT_EMAIL', 'support@example.com'), os.getenv('DEMO_SUPPORT_PASSWORD', secrets.token_urlsafe(24)), Role.support, True, os.getenv('DEMO_SUPPORT_2FA_SECRET', pyotp.random_base32()))
        operator = await ensure_user(db, os.getenv('DEMO_OPERATOR_EMAIL', 'operator@example.com'), os.getenv('DEMO_OPERATOR_PASSWORD', secrets.token_urlsafe(24)), Role.operator, False, None)
        merchant_owner = await ensure_user(db, os.getenv('DEMO_MERCHANT_EMAIL', 'merchant@example.com'), os.getenv('DEMO_MERCHANT_PASSWORD', secrets.token_urlsafe(24)), Role.merchant, False, None)
        await ensure_demo_merchant(db, merchant_owner)
        await ensure_demo_requisite(db, operator)
        if not (await db.execute(select(Requisite).limit(1))).scalar_one_or_none():
            db.add(Requisite(
                trader_id=operator.id,
                owner_name='Demo Operator Receiver',
                full_name='Иванов Иван Иванович',
                method='sbp',
                value_encrypted=encrypt_secret('+79990000000'),
                bank_name='Test Bank',
                automation_id='demo-auto-001',
                last4='0000',
                daily_limit=Decimal('500000.00'),
                operation_limit=100,
                status='active',
                enabled=True,
            ))
        await db.commit()
if __name__ == '__main__':
    asyncio.run(main())
