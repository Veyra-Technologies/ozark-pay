from fastapi import APIRouter
from app.api.v1 import auth, admin, merchant, cabinet, aggregator, crossborder, external_processors
from app.api.v1 import crossborder_live
from app.api.v1 import merchant_settlements
from app.api.v1 import tj
api_router=APIRouter(prefix='/api/v1')
api_router.include_router(auth.router)
api_router.include_router(admin.router)
api_router.include_router(merchant.router)
api_router.include_router(aggregator.router)
api_router.include_router(cabinet.router)
api_router.include_router(crossborder.router)
api_router.include_router(crossborder_live.router)
api_router.include_router(external_processors.router)
api_router.include_router(merchant_settlements.router)
api_router.include_router(tj.router)
