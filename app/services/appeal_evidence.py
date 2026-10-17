"""Common proof and ownership validation for web and REST appeal creation."""
import uuid
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select

from app.core.russian_banks import normalize_bank_name
from app.models import Appeal, AppealMessage, Deposit, Merchant, Requisite
from app.services.audit import audit
from app.services.crossborder_money import local_amount

UPLOAD_ROOT = Path('uploads/appeals')
MAX_UPLOAD_BYTES = 5 * 1024 * 1024
ALLOWED_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.pdf', '.webp'}


def magic_matches(ext, content):
    if ext == '.pdf':
        return content.startswith(b'%PDF-')
    if ext in {'.jpg', '.jpeg'}:
        return content.startswith(b'\xff\xd8\xff')
    if ext == '.png':
        return content.startswith(b'\x89PNG\r\n\x1a\n')
    return ext == '.webp' and len(content) >= 12 and content[:4] == b'RIFF' and content[8:12] == b'WEBP'


async def save_upload(upload, *, required=True, root=None):
    if not upload or not upload.filename:
        if required:
            raise ValueError('Приложите чек')
        return None
    filename = str(upload.filename)
    ext = Path(filename).suffix.lower()
    if any(part in filename for part in ('/', '\\', '..')) or ext not in ALLOWED_EXTENSIONS:
        raise ValueError('Разрешены только JPG, PNG, WEBP или PDF с безопасным именем')
    content = await upload.read(MAX_UPLOAD_BYTES + 1)
    if not content or len(content) > MAX_UPLOAD_BYTES or not magic_matches(ext, content):
        raise ValueError('Некорректный или слишком большой чек (максимум 5 МБ)')
    root = root or UPLOAD_ROOT
    root.mkdir(parents=True, exist_ok=True)
    name = f'{uuid.uuid4().hex}{ext}'
    (root / name).write_bytes(content)
    return {'path': name, 'original_name': filename[:180], 'size': len(content),
            'content_type': {'.pdf': 'application/pdf', '.png': 'image/png', '.webp': 'image/webp',
                             '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg'}[ext]}


def validate_receipt(receipt, *, root=None):
    if not isinstance(receipt, dict):
        raise ValueError('Требуется сохранённый чек')
    name = str(receipt.get('path') or '')
    if not name or Path(name).name != name or '/' in name or '\\' in name:
        raise ValueError('Некорректный чек')
    try:
        uuid.UUID(Path(name).stem)
    except ValueError as exc:
        raise ValueError('Некорректный идентификатор чека') from exc
    path = (root or UPLOAD_ROOT) / name
    if not path.is_file() or not 0 < path.stat().st_size <= MAX_UPLOAD_BYTES:
        raise ValueError('Чек отсутствует или повреждён')
    with path.open('rb') as stream:
        if not magic_matches(path.suffix.lower(), stream.read(16)):
            raise ValueError('Неверный формат чека')


def validated_fields(*, amount, requisite, recipient_bank, operation_date, receipt, root=None, country='RU'):
    amount = local_amount(amount)
    if not 4 <= len(requisite.strip()) <= 300:
        raise ValueError('Укажите реквизиты получателя')
    bank = (recipient_bank if recipient_bank in ('tj-dushanbe-city','tj-korti-milli') else '') if country == 'TJ' else normalize_bank_name(recipient_bank)
    if not bank:
        raise ValueError('Выберите банк из справочника')
    try:
        paid_at = datetime.fromisoformat(operation_date)
    except (ValueError, TypeError) as exc:
        raise ValueError('Укажите корректную дату операции') from exc
    if country == 'TJ' and (not paid_at.tzinfo or paid_at > datetime.now(timezone.utc)):
        raise ValueError('Укажите время поступления с часовым поясом, не в будущем')
    if country != 'TJ' and paid_at.date() > datetime.now(timezone.utc).date():
        raise ValueError('Дата операции не может быть в будущем')
    validate_receipt(receipt, root=root)
    return {'amount_claimed': str(amount), 'requisite': requisite.strip(), 'recipient_bank': bank,
            'operation_date': paid_at.isoformat(), 'receipt_file': receipt, 'validation_version': 1}


async def create_deposit_appeal(db, *, user, deposit_id, trader_id, amount, requisite,
                               recipient_bank, operation_date, receipt, message='', root=None):
    from app.services.appeals import default_deadline
    if not user or user.role != 'merchant':
        raise ValueError('Создавать апелляцию может мерчант по своей операции')
    # The same lock is acquired by both creation paths and resolution paths.
    dep = (await db.execute(select(Deposit).where(Deposit.id == deposit_id).with_for_update()
                           .execution_options(populate_existing=True))).scalar_one_or_none()
    merchant = await db.get(Merchant, dep.merchant_id) if dep else None
    if not merchant or merchant.owner_id != user.id:
        raise ValueError('Операция не найдена')
    if dep.currency == 'TJS':
        from app.services.local_deposit_policy import require_execution
        await require_execution(db)
        if dep.status in ('paid','refunded'):
            raise ValueError('Оплаченная или возвращённая операция не является неоплаченным пополнением')
    elif dep.currency != 'RUB':
        raise ValueError('Для валютной операции требуется ручное разбирательство без автоматического расчёта RUB')
    req = await db.get(Requisite, dep.requisites_id) if dep.requisites_id else None
    if not req or not req.trader_id or str(req.trader_id) != str(trader_id):
        raise ValueError('Трейдер должен совпадать с назначенным исполнителем сделки')
    if dep.currency == 'TJS':
        await require_tj_receipt_match(db,dep,amount=amount,requisite=requisite,
            recipient_bank=recipient_bank,operation_date=operation_date)
    duplicate = (await db.execute(select(Appeal.id).where(Appeal.operation_type == 'deposit',
        Appeal.operation_id == dep.id, Appeal.status.in_(('opened', 'in_review'))).limit(1))).scalar_one_or_none()
    if duplicate:
        raise ValueError('По этой сделке уже есть активная апелляция')
    fields = validated_fields(amount=amount, requisite=requisite, recipient_bank=recipient_bank,
                              operation_date=operation_date, receipt=receipt, root=root,
                              country='TJ' if dep.currency == 'TJS' else 'RU')
    previous = dep.status
    if dep.status != 'paid':
        dep.status = 'appeal_opened'
    appeal = Appeal(operation_type='deposit', operation_id=dep.id, created_by=user.id, status='opened',
        evidence_validated=True, metadata_json={**fields, 'merchant_id': str(merchant.id),
        'trader_id': str(req.trader_id), 'deadline_at': default_deadline().isoformat(),
        'previous_deposit_status': previous, 'source': 'merchant_validated'})
    db.add(appeal)
    await db.flush()
    db.add(AppealMessage(appeal_id=appeal.id, author_id=user.id,
                        message=message.strip()[:2000] or 'Мерчант создал апелляцию с чеком.'))
    await audit(db, 'merchant_appeal_created', 'appeal', user.id, appeal.id,
                details={'deposit_id': str(dep.id), 'trader_id': str(req.trader_id), 'validation_version': 1})
    return appeal


async def require_approval_evidence(db, appeal, dep):
    meta = appeal.metadata_json or {}
    if not appeal.evidence_validated or meta.get('validation_version') != 1 or dep.currency not in ('RUB','TJS'):
        raise ValueError('Апелляция требует проверки доказательств; финансовое одобрение заблокировано')
    validated_fields(amount=meta.get('amount_claimed'), requisite=meta.get('requisite') or '',
        recipient_bank=meta.get('recipient_bank'), operation_date=meta.get('operation_date'), receipt=meta.get('receipt_file'),
        country='TJ' if dep.currency == 'TJS' else 'RU')
    req = await db.get(Requisite, dep.requisites_id) if dep.requisites_id else None
    if not req or not req.trader_id or str(req.trader_id) != meta.get('trader_id'):
        raise ValueError('Исполнитель апелляции не совпадает с исполнителем сделки')
    if dep.currency == 'TJS':
        from app.services.local_deposit_policy import require_execution
        await require_execution(db)
        await require_tj_receipt_match(db,dep,amount=meta['amount_claimed'],requisite=meta['requisite'],
            recipient_bank=meta['recipient_bank'],operation_date=meta['operation_date'])


async def require_tj_receipt_match(db,dep,*,amount,requisite,recipient_bank,operation_date):
    from app.core.security import reveal_text
    from app.services.local_deposit_policy import snapshot
    fixed=(await snapshot(db,dep)).financial_context['requisite']
    paid_at=datetime.fromisoformat(operation_date)
    created_at=dep.created_at if dep.created_at.tzinfo else dep.created_at.replace(tzinfo=timezone.utc)
    if (not paid_at.tzinfo or not created_at <= paid_at <= datetime.now(timezone.utc)
            or local_amount(amount) != dep.amount or recipient_bank != fixed['bank_id']
            or ''.join(str(requisite).split()) != ''.join(reveal_text(fixed['value_encrypted']).split())):
        raise ValueError('Сумма, время и банковские реквизиты TJ должны совпадать с принятой заявкой')
