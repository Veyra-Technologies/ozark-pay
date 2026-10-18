"""Provider callbacks are separate from incoming-partner and Merchant APIs."""
from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import IntegrityError
from app.db.session import get_db
from app.services.aggregator_execution import receive_webhook

router = APIRouter(prefix='/external-aggregators', tags=['External aggregator callbacks'])


@router.post('/{version_id}/webhook')
async def provider_webhook(version_id: UUID, request: Request, db: AsyncSession = Depends(get_db)):
    try:
        result = await receive_webhook(db, version_id=version_id, body=await request.body(),
            headers=dict(request.headers), source_ip=request.client.host if request.client else '')
        await db.commit()
        return {'status': result}
    except (ValueError, TypeError, KeyError, ArithmeticError, IntegrityError):
        await db.rollback()
        raise HTTPException(403, 'provider notification rejected') from None
