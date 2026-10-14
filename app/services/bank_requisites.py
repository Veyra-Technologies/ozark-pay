"""Shared GE create/edit validation; bank values stay in the existing encrypted column."""
from app.core.ge_bank_requisites import masked_requisite, normalize_ge_requisite
from app.core.security import reveal_text
from app.models import CrossBorderOperation
from fastapi import HTTPException
from sqlalchemy import select


def validated_requisite_value(jurisdiction, value: str, last4: str | None = None) -> tuple[str, str | None]:
    if jurisdiction.country != 'GE':
        return value.strip(), last4 or None
    try:
        clean = normalize_ge_requisite(jurisdiction.method, value, jurisdiction.bank_id, last4)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return clean, clean[-4:]


def requisite_list_value(req) -> str:
    if req.country == 'GE':
        suffix = req.last4 or reveal_text(req.value_encrypted)[-4:]
        return masked_requisite(suffix)
    return reveal_text(req.value_encrypted)


async def check_requisite_identity_edit(db, req, jurisdiction, value: str) -> None:
    if req.country != 'GE':
        return
    changed = (req.method != jurisdiction.method or req.bank_id != jurisdiction.bank_id
               or reveal_text(req.value_encrypted) != value)
    if changed and await db.scalar(select(CrossBorderOperation.id).where(
            CrossBorderOperation.requisite_id == req.id).limit(1)):
        raise HTTPException(409, 'Реквизит используется в истории операций; для новых банковских данных создайте отдельный реквизит')
