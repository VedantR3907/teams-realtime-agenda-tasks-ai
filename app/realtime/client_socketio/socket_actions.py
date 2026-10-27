from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional

from loguru import logger

from app.config import settings
from app.realtime.client_socketio.socket_connection import get_socketio_client


async def send_agenda_status_update(
    agenda_item: str,
    new_state: str,
    timestamp: float,
    meeting_id: Optional[str] = None,
    agenda_id: Optional[str] = None,
) -> bool:
    """Send a single agenda status update to the client Socket.IO server.

    This shapes the payload, updates in-memory `TOUCHPOINT_DATA` timings, and
    delegates the actual emission to the underlying Socket.IO client.

    Args:
        agenda_item: Agenda title (or id if title cannot be resolved).
        new_state: One of [Not Started, In Progress, On Hold, Completed] or
            lower-cased client variants (remaining, in_discussion, hold, discussed).
        timestamp: Relative timestamp in seconds since meeting start.
        meeting_id: Optional touchpoint id override (defaults to settings.MEETING_ID).
        agenda_id: Optional agenda id override (defaults via title→id mapping).

    Returns:
        True if queued/sent successfully; False on failure.
    """
    state_mapping = {
        "Not Started": "remaining",
        "In Progress": "in_discussion",
        "On Hold": "hold",
        "Completed": "discussed",
        "Discussed": "discussed",
        "inprogress": "in_discussion",
        "completed": "discussed",
        "onhold": "hold",
        "not started": "remaining",
    }
    actual_meeting_id = meeting_id or settings.TOUCHPOINT_ID or "unknown"
    actual_timestamp_ms = int(timestamp * 1000)
    mapped_state = state_mapping.get(new_state, new_state.lower().replace(" ", "_"))
    actual_agenda_id = agenda_id or settings.AGENDA_TITLE_TO_ID.get(agenda_item, agenda_item)

    try:
        if isinstance(settings.TOUCHPOINT_DATA, dict):
            agenda_list = settings.TOUCHPOINT_DATA.setdefault("agenda", [])
            match = None
            for item in agenda_list:
                if item.get("id") == actual_agenda_id:
                    match = item
                    break
            if match is None:
                match = {"id": actual_agenda_id}
                agenda_list.append(match)
            match["status"] = mapped_state
            existing_timings = match.get("timings", [])
            if not isinstance(existing_timings, list):
                existing_timings = []
            existing_timings.append(actual_timestamp_ms)
            match["timings"] = existing_timings
    except Exception as e:
        logger.warning(f"⚠️ Failed to update TOUCHPOINT_DATA on send: {e}")

    payload = {
        "context": {
            "executionId": settings.EXECUTION_ID or "",
            "sprintId": settings.SPRINT_ID or "",
            "touchpointId": actual_meeting_id,
            "itemId": settings.ITEM_ID or "",
        },
        "agendaItems": [
            {
                "type": "update",
                "id": actual_agenda_id,
                "status": mapped_state,
                "timings": [],
            }
        ],
    }

    try:
        timings_for_agenda: List[int] = []
        if isinstance(settings.TOUCHPOINT_DATA, dict):
            for item in settings.TOUCHPOINT_DATA.get("agenda", []):
                if item.get("id") == actual_agenda_id:
                    timings_for_agenda = item.get("timings", []) or []
                    break
        payload["agendaItems"][0]["timings"] = timings_for_agenda
    except Exception:
        payload["agendaItems"][0]["timings"] = [actual_timestamp_ms]

    try:
        client = await get_socketio_client()
        await client.send_agenda_status_update(
            agenda_item,
            new_state,
            timestamp,
            meeting_id,
            agenda_id,
        )
        return True
    except Exception as e:
        logger.error(f"Failed to send agenda status update: {e}")
        return False


async def send_agenda_status_updates(
    updates: List[Dict[str, Any]],
    meeting_id: Optional[str] = None,
) -> bool:
    """Send a batch of agenda status updates in one payload.

    Each entry in `updates` should include keys like `agenda_item`/`agenda_id`,
    `new_state` (or `status`), `timestamp` (seconds), and optional `type`.

    Args:
        updates: List of agenda update dicts to batch.
        meeting_id: Optional touchpoint id override for the batch context.

    Returns:
        True if the batch was sent successfully; False otherwise.
    """
    try:
        client = await get_socketio_client()
        success = await client.send_agenda_status_update(
            batch_updates=updates, meeting_id=meeting_id
        )
        return success
    except Exception as e:
        logger.error(f"Failed to send batch agenda updates: {e}")
        return False


async def send_progress_item_update(
    progress_item: dict,
    meeting_id: Optional[str] = None,
) -> bool:
    """Create or update a single progress item via Socket.IO.

    Determines event type (create/update) by checking prior existence in
    `TOUCHPOINT_DATA` within the client and forwards the shaped payload.

    Args:
        progress_item: Progress item fields (id, description, type, assignee, dueDate, etc.).
        meeting_id: Optional touchpoint id override.

    Returns:
        True if sent successfully; False otherwise.
    """
    try:
        client = await get_socketio_client()
        success = await client.send_progress_item_update(progress_item, meeting_id)
        return success
    except Exception as e:
        logger.error(f"Failed to send progress item update: {e}")
        return False


async def send_progress_items_update(
    progress_items: List[dict],
    meeting_id: Optional[str] = None,
) -> bool:
    """Send multiple progress items in a single batch payload.

    This preserves ordering rules (assigns `order` for creates) and passes the
    full list to the client emitter for a single network call.

    Args:
        progress_items: List of progress item dicts to create or update.
        meeting_id: Optional touchpoint id override for the batch.

    Returns:
        True if the batch was sent successfully; False otherwise.
    """
    try:
        client = await get_socketio_client()
        success = await client.send_progress_item_update(
            progress_item={},
            meeting_id=meeting_id,
            progress_items=progress_items,
        )
        return success
    except Exception as e:
        logger.error(f"Failed to send batch progress items: {e}")
        return False


async def send_time_out_alert(
    agenda_item: str,
    meeting_id: Optional[str] = None,
    agenda_id: Optional[str] = None,
) -> None:
    """Send an overtime alert for a specific agenda item.

    Emits a `time_out_alert` event with the touchpoint and agenda id, typically
    triggered by the timer manager's overtime callback.

    Args:
        agenda_item: Agenda title used to resolve the agenda id mapping.
        meeting_id: Optional touchpoint id override.
        agenda_id: Optional direct agenda id; skips title mapping if provided.
    """
    try:
        client = await get_socketio_client()
        await client.send_time_out_alert(agenda_item, meeting_id, agenda_id)
    except Exception as e:
        logger.error(f"Failed to send time out alert: {e}")
