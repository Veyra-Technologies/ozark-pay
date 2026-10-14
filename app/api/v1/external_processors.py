import uuid

from fastapi import APIRouter, Depends, Request
from fastapi.responses import ORJSONResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.client_ip import client_ip
from app.db.session import get_db
from app.services.external_processors import ExternalProcessorError, handle_tradespace_webhook


router = APIRouter(prefix='/external-processors', tags=['external-processors'])


@router.post('/tradespace/{connection_id}/webhook', include_in_schema=False)
async def tradespace_webhook(connection_id: str, request: Request, db: AsyncSession = Depends(get_db)):
    try:
        parsed_id = uuid.UUID(connection_id)
    except ValueError:
        return ORJSONResponse({'error': {'code': 'connection_not_found'}}, status_code=404)
    body = await request.body()
    try:
        status_code, _ = await handle_tradespace_webhook(
            db,
            parsed_id,
            headers=request.headers,
            body=body,
            source_ip=client_ip(request),
        )
    except ExternalProcessorError as exc:
        if db.in_transaction():
            await db.rollback()
        return ORJSONResponse(
            {'error': {'code': exc.code, 'message': str(exc)}},
            status_code=exc.status_code,
        )
    return Response(status_code=status_code)
