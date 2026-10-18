import uuid

from fastapi import APIRouter, Depends, Request
from fastapi.responses import ORJSONResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.client_ip import client_ip
from app.db.session import get_db
from app.models import ExternalProcessorConnection
from app.services.external_processors import ExternalProcessorError, handle_processor_webhook


router = APIRouter(prefix='/external-processors', tags=['external-processors'])


@router.post('/{protocol}/{connection_id}/webhook', include_in_schema=False)
async def external_processor_webhook(protocol: str, connection_id: str, request: Request, db: AsyncSession = Depends(get_db)):
    try:
        parsed_id = uuid.UUID(connection_id)
    except ValueError:
        return ORJSONResponse({'error': {'code': 'connection_not_found'}}, status_code=404)
    connection = await db.get(ExternalProcessorConnection, parsed_id)
    # An existing integration retains its persisted wire-header dialect and URL.
    # New integrations use the generic protocol and header names.
    if not connection or protocol not in ('merchant-v2', connection.webhook_header_prefix.removeprefix('X-').lower()):
        return ORJSONResponse({'error': {'code': 'connection_not_found'}}, status_code=404)
    body = await request.body()
    try:
        status_code, _ = await handle_processor_webhook(
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
