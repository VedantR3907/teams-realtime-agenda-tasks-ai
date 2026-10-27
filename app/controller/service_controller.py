import asyncio
from loguru import logger
from fastapi import APIRouter
from app.utils.response import error_response, success_response
from app.config import settings
from app.realtime.client_socketio.socket_connection import get_socketio_client
from app.utils.startup_fetcher import (
    call_bot_join_with_retries,
    fetch_touchpoint_id_from_meeting,
    fetch_and_sync_touchpoint_by_id,
    fetch_organization_profiles,
)

router = APIRouter()


@router.post("/started")
async def acknowledge_service_started():
    """
    Startup hook:
    - Fetch the touchpoint object using dynamic IDs (execution/sprint/item/touchpoint)
    - Store the full object globally in settings (TOUCHPOINT_DATA and mappings)
    - Optionally ensure Socket.IO connects and joins the room
    """
    # Validate required IDs
    missing = []
    if not settings.EXECUTION_ID:
        missing.append("execution_id")
    if not settings.SPRINT_ID:
        missing.append("sprint_id")
    if not settings.ITEM_ID:
        missing.append("item_id")
    if missing:
        return error_response(f"Missing required IDs in settings: {', '.join(missing)}", 400)

    try:
        # Step 1: Resolve TOUCHPOINT_ID from meeting-based endpoint
        await fetch_touchpoint_id_from_meeting()

        # Step 2: Fetch full touchpoint by TOUCHPOINT_ID and populate settings
        agendas_and_progress_items = await fetch_and_sync_touchpoint_by_id()

        # Fetch organization participants prior to injecting into other components
        organization_participants = await fetch_organization_profiles()

        # Ensure Socket.IO connects; join room will occur after TOUCHPOINT_ID is set
        try:
            client = await get_socketio_client()
            if client.is_connected():
                logger.info("✅ Socket.IO connected post-startup")
            else:
                await client.connect()
        except Exception as e:
            logger.error(f"⚠️ Socket.IO connection not established at startup: {e}")

        # After TOUCHPOINT_ID is resolved and details fetched, join the room now
        try:
            client = await get_socketio_client()
            await client.join_current_room()
        except Exception as e:
            logger.error(f"⚠️ Failed to join room after startup fetch: {e}")

        # Schedule local bot join after response returns
        # try:
        #     if settings.MEETING_JOIN_URL and settings.MEETING_ID:
        #         logger.info("🗓️ Scheduling bot join background task after /service/started")
        #         asyncio.create_task(
        #             call_bot_join_with_retries(
        #                 meeting_id=settings.MEETING_ID,
        #                 join_url=settings.MEETING_JOIN_URL,
        #                 initial_delay=2.0,
        #                 max_attempts=10,
        #                 backoff_multiplier=1.5,
        #             )
        #         )
        #     else:
        #         logger.warning("⚠️ Missing meeting join URL or meeting id; not scheduling bot join task")
        # except Exception as e:
        #     logger.error(f"❌ Failed to schedule bot join task: {e}")

        return success_response({
            "message": "Startup touchpoint fetched and stored",
            "meetingId": settings.MEETING_ID,
            "touchpointId": settings.TOUCHPOINT_ID,
            "agendaCount": len(settings.AGENDA_ITEMS_FULL or []),
            "previousItems": len(settings.PREVIOUS_PROGRESS_ITEMS or []),
            "joinUrl": settings.MEETING_JOIN_URL,
            "organizationParticipants": len(organization_participants),
        })
    except Exception as e:
        logger.error(f"Failed startup touchpoint fetch: {e}")
        return error_response(f"Failed to fetch touchpoint: {e}", 500)