"""Aggregates all v1 API routes into a single router."""
from fastapi import APIRouter

from .agents import router as agents_router
from .messages import router as messages_router
from .threads import router as threads_router
from .auth import router as auth_router
from .events import router as events_router
from .admin import router as admin_router
from .ws import router as ws_router

router = APIRouter()

router.include_router(agents_router)
router.include_router(messages_router)
router.include_router(threads_router)
router.include_router(auth_router)
router.include_router(events_router)
router.include_router(admin_router)
router.include_router(ws_router)
