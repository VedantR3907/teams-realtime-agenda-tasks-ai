import asyncio
from typing import Optional
import httpx
from loguru import logger

from app.config import settings
from app.realtime.client_socketio.socket_connection import get_socketio_client
from app.utils.token import get_api_token


async def call_bot_join_with_retries(
    meeting_id: str,
    join_url: str,
    initial_delay: float = 2.0,
    max_attempts: int = 10,
    backoff_multiplier: float = 1.5,
) -> bool:
    """Call the local bot join API after a short delay, with retries/backoff."""
    try:
        await asyncio.sleep(initial_delay)

        short_meeting_id = (meeting_id or "")[:10]
        if not short_meeting_id:
            logger.warning("⚠️ Empty meeting_id provided to bot join; using empty short id")
        join_api = f"{settings.BOT_JOIN_URL}/{short_meeting_id}/Calls"
        timeout_config = httpx.Timeout(10.0, connect=5.0)

        for attempt in range(1, max_attempts + 1):
            try:
                async with httpx.AsyncClient(timeout=timeout_config, trust_env=False, verify=False) as client:
                    logger.info(
                        f"📞 Calling local bot join API (attempt {attempt}/{max_attempts}): {join_api}"
                    )
                    logger.info(f"📋 Request body: {{'joinURL': '<redacted>'}}")
                    resp = await client.post(
                        join_api,
                        json={"joinURL": join_url},
                        headers={"Content-Type": "application/json"},
                    )
                    logger.info(f"🤖 Bot join response: {resp.json()}")

                    if resp.status_code >= 400:
                        body_preview = resp.text[:500]
                        logger.error(
                            f"❌ Bot API returned error status: {resp.status_code}; body: {body_preview}"
                        )
                    else:
                        logger.info("✅ Bot join API call successful")
                        return True
            except (httpx.ConnectError, httpx.TimeoutException, httpx.RequestError) as e:
                logger.error(
                    f"📡 Bot join attempt {attempt}/{max_attempts} failed: {type(e).__name__}: {e}"
                )

            if attempt < max_attempts:
                delay = initial_delay * (backoff_multiplier ** (attempt - 1))
                logger.info(f"⏳ Retrying bot API call in {delay:.1f} seconds...")
                await asyncio.sleep(delay)

        logger.error(f"❌ Bot join failed after {max_attempts} attempts")
        return False
    except Exception as e:
        logger.error(f"❌ Unexpected error in bot join background task: {e}")
        return False


async def fetch_organization_profiles() -> list[str]:
    """Fetch organization member profiles and populate settings caches."""
    try:
        if not settings.ORGANIZATION_ID:
            logger.warning("⚠️ No ORGANIZATION_ID available, skipping members fetch")
            settings.PARTICIPANT_NAME_TO_ID = {}
            settings.PARTICIPANT_ID_TO_PROFILE = {}
            settings.ORGANIZATION_PARTICIPANTS = []
            return []

        url = f"{settings.SOCKET_URL}/api/organization/{settings.ORGANIZATION_ID}/members"

        bearer_token = get_api_token()

        max_attempts = 3
        response = None
        for attempt in range(1, max_attempts + 1):
            try:
                async with httpx.AsyncClient(timeout=10.0, verify=False) as client:
                    logger.info(
                        f"🔍 Fetching organization members (attempt {attempt}/{max_attempts})..."
                    )
                    response = await client.get(
                        url,
                        headers={"Authorization": f"Bearer {bearer_token}", "python-server": "true"},
                    )
                    response.raise_for_status()
                    break
            except httpx.HTTPStatusError as e:
                status = e.response.status_code if e.response else "unknown"
                body = e.response.text if e.response else ""
                logger.error(
                    f"❌ Members fetch HTTP {status} (attempt {attempt}/{max_attempts}): {body}"
                )
                if status == 401 and attempt < max_attempts:
                    try:
                        bearer_token = get_api_token()
                    except Exception as te:
                        logger.error(
                            f"❌ Token refresh failed during members retry: {te}"
                        )
                if attempt == max_attempts:
                    raise
            except Exception as e:
                logger.error(
                    f"❌ Members fetch failed (attempt {attempt}/{max_attempts}): {e}"
                )
                if attempt == max_attempts:
                    raise

        if response is None:
            logger.error("❌ Members fetch failed: no response after retries")
            settings.PARTICIPANT_NAME_TO_ID = {}
            settings.PARTICIPANT_ID_TO_PROFILE = {}
            settings.ORGANIZATION_PARTICIPANTS = []
            return []

        try:
            data = response.json()
        except Exception:
            raw = response.text if hasattr(response, "text") else "<no text>"
            logger.error(f"❌ Members API returned non-JSON: {raw[:500]}")
            settings.PARTICIPANT_NAME_TO_ID = {}
            settings.PARTICIPANT_ID_TO_PROFILE = {}
            settings.ORGANIZATION_PARTICIPANTS = []
            return []

        # Handle both new format (direct array) and old format (dict with profiles/members key)
        if isinstance(data, list):
            members = data
        elif isinstance(data, dict):
            members = data.get("profiles") or data.get("members", [])
        else:
            members = []

        user_names: list[str] = []
        user_name_to_id_mapping: dict[str, str] = {}
        participant_id_to_profile: dict[str, dict[str, str | None]] = {}

        for profile in members or []:
            if not isinstance(profile, dict):
                continue
            user_id = profile.get("id")
            # New format uses "name", old format used "userName"
            user_name = profile.get("name") or profile.get("userName") or profile.get("email")
            user_email = profile.get("email")

            if user_id:
                participant_id_to_profile[user_id] = {
                    "id": user_id,
                    "email": user_email or None,
                    "userName": user_name if (profile.get("name") or profile.get("userName")) else None,
                }

            if user_name and user_id:
                user_names.append(user_name)
                user_name_to_id_mapping[user_name] = user_id
            if user_email and user_id:
                user_name_to_id_mapping[user_email] = user_id

        settings.PARTICIPANT_NAME_TO_ID = user_name_to_id_mapping
        settings.PARTICIPANT_ID_TO_PROFILE = participant_id_to_profile
        settings.ORGANIZATION_PARTICIPANTS = user_names

        logger.info(
            f"✅ Successfully fetched {len(user_names)} organization members with name-to-ID mapping"
        )
        logger.debug(f"📋 Member names: {user_names[:5]}...")
        try:
            logger.debug(
                f"📋 Name-to-ID mapping sample: {dict(list(user_name_to_id_mapping.items())[:3])}"
            )
        except Exception:
            pass

        return user_names

    except Exception as e:
        logger.error(f"❌ Failed to fetch organization members: {e}")
        logger.warning("⚠️ Will use fallback participant list for name correction")
        settings.PARTICIPANT_NAME_TO_ID = {}
        settings.PARTICIPANT_ID_TO_PROFILE = {}
        settings.ORGANIZATION_PARTICIPANTS = []
        return []

async def fetch_touchpoint_id_from_meeting() -> Optional[str]:
    """Fetch using meetingId URL to resolve and set only TOUCHPOINT_ID.

    Does not populate any other settings fields; strictly extracts the id
    from the response body and assigns it to settings.TOUCHPOINT_ID.
    """
    url = (
        f"{settings.SOCKET_URL}/api/"
        f"organization/{settings.ORGANIZATION_ID}/"
        f"execution/{settings.EXECUTION_ID}/"
        f"sprint/{settings.SPRINT_ID}/"
        f"item/{settings.ITEM_ID}/"
        f"meetingId/{settings.MEETING_ID}/query_touchpoint"
    )

    bearer_token = get_api_token()
    max_attempts = 3
    response = None
    for attempt in range(1, max_attempts + 1):
        try:
            async with httpx.AsyncClient(verify=False) as client:
                logger.info(
                    f"🔍 Resolving touchpoint id from meetingId (attempt {attempt}/{max_attempts}): {url}"
                )
                resp = await client.get(
                    url,
                    headers={"Authorization": f"Bearer {bearer_token}", "python-server": "true"},
                )
                resp.raise_for_status()
                response = resp.json()
                break
        except Exception as e:
            logger.error(
                f"❌ Touchpoint id resolve failed (attempt {attempt}/{max_attempts}): {e}"
            )
            if isinstance(e, httpx.HTTPStatusError):
                status = e.response.status_code if e.response else None
                if status == 401 and attempt < max_attempts:
                    try:
                        bearer_token = get_api_token()
                    except Exception as refresh_error:
                        logger.error(
                            f"❌ Token refresh failed during id-resolve retry: {refresh_error}"
                        )
            if attempt == max_attempts:
                return None

    if not isinstance(response, dict):
        logger.error("❌ Unexpected response format when resolving touchpoint id")
        return None

    tp_id = response.get("id")
    if not tp_id:
        logger.error("❌ No id found in touchpoint response while resolving touchpoint id")
        return None

    settings.TOUCHPOINT_ID = tp_id
    logger.info(f"✅ Resolved and set TOUCHPOINT_ID: {settings.TOUCHPOINT_ID}")
    return tp_id


async def fetch_and_sync_touchpoint_by_id() -> Optional[dict]:
    """Fetch full touchpoint using TOUCHPOINT_ID and populate settings data."""
    if not settings.TOUCHPOINT_ID:
        logger.warning("⚠️ No TOUCHPOINT_ID set; cannot fetch touchpoint details by id")
        return None

    url = (
        f"{settings.SOCKET_URL}/api/"
        f"organization/{settings.ORGANIZATION_ID}/"
        f"execution/{settings.EXECUTION_ID}/"
        f"sprint/{settings.SPRINT_ID}/"
        f"item/{settings.ITEM_ID}/"
        f"touchpoint/{settings.TOUCHPOINT_ID}/query_touchpoint"
    )

    bearer_token = get_api_token()

    max_attempts = 3
    payload = None
    for attempt in range(1, max_attempts + 1):
        try:
            async with httpx.AsyncClient(verify=False) as client:
                logger.info(
                    f"🔍 Fetching touchpoint (by id) at startup (attempt {attempt}/{max_attempts}): {url}"
                )
                resp = await client.get(
                    url, headers={"Authorization": f"Bearer {bearer_token}", "python-server": "true"}
                )
                resp.raise_for_status()
                payload = resp.json()
                break
        except Exception as e:
            logger.error(
                f"❌ Touchpoint fetch by id failed (attempt {attempt}/{max_attempts}): {e}"
            )
            if isinstance(e, httpx.HTTPStatusError):
                status = e.response.status_code if e.response else None
                if status == 401 and attempt < max_attempts:
                    try:
                        bearer_token = get_api_token()
                    except Exception as refresh_error:
                        logger.error(
                            f"❌ Token refresh failed during touchpoint-by-id retry: {refresh_error}"
                        )
            if attempt == max_attempts:
                raise

    settings.TOUCHPOINT_DATA = payload
    if isinstance(payload, dict):
        # Set top-level id as TOUCHPOINT_ID (authoritative)
        if payload.get("id"):
            settings.TOUCHPOINT_ID = payload["id"]

        # Meeting metadata fields
        settings.MEETING_START_TIME = payload.get("startTime", "")
        settings.MEETING_END_TIME = payload.get("endTime", "")
        settings.MEETING_DATE = payload.get("meetingDate", "")

        # Used only for bot join; store join URL
        settings.MEETING_JOIN_URL = payload.get("meetingLink", "")
        # if settings.MEETING_JOIN_URL in ["", None, "To be defined"]:

        # Agenda
        agenda_list = payload.get("agenda", []) or []
        settings.AGENDA_ITEMS_FULL = agenda_list
        settings.AGENDA_TITLE_TO_ID = {
            item["title"]: item["id"]
            for item in agenda_list
            if isinstance(item, dict) and item.get("title") and item.get("id")
        }
        settings.AGENDA_ID_TO_TITLE = {v: k for k, v in settings.AGENDA_TITLE_TO_ID.items()}
        settings.AGENDA_ITEMS = {
            item.get("title", ""): item.get("description", "")
            for item in agenda_list
            if isinstance(item, dict) and item.get("title")
        }
        # Agenda state and timings mappings (by ID)
        settings.AGENDA_ID_TO_STATE = {
            item["id"]: item.get("status", "not_started")
            for item in agenda_list
            if isinstance(item, dict) and item.get("id")
        }
        settings.AGENDA_ID_TO_TIMINGS = {
            item["id"]: item.get("timings", []) or []
            for item in agenda_list
            if isinstance(item, dict) and item.get("id")
        }
        # Agenda state and timings mappings (by title)
        settings.AGENDA_TITLE_TO_STATE = {
            item["title"]: item.get("status", "not_started")
            for item in agenda_list
            if isinstance(item, dict) and item.get("title")
        }
        settings.AGENDA_TITLE_TO_TIMINGS = {
            item["title"]: item.get("timings", []) or []
            for item in agenda_list
            if isinstance(item, dict) and item.get("title")
        }

        # Previous progress items (unresolved only)
        prev_items = payload.get("progressItems", []) or []
        filtered_prev = []
        for item in prev_items:
            try:
                if item.get("resolved", False) is True:
                    continue

                assignee_display = ""
                assignee_obj = item.get("assignee")
                if isinstance(assignee_obj, dict):
                    user_name = assignee_obj.get("userName") or ""
                    email = assignee_obj.get("email") or ""
                    assignee_id = assignee_obj.get("id") or ""
                    if user_name:
                        assignee_display = user_name
                    elif email:
                        assignee_display = email
                    elif assignee_id:
                        assignee_display = assignee_id
                elif isinstance(assignee_obj, str):
                    assignee_display = assignee_obj

                # Normalize progress type for LLM processor expectations
                try:
                    _ptype_raw = item.get("progressItemType") or "agreement"
                    _ptype = str(_ptype_raw).lower()
                    _ptype_title = {
                        "agreement": "Agreement",
                        "issue": "Issue",
                        "decision": "Decision",
                    }.get(_ptype, _ptype_raw)
                except Exception:
                    _ptype_title = "Agreement"

                filtered_prev.append(
                    {
                        "id": item.get("id"),
                        "description": item.get("description", ""),
                        # Keep original API field for completeness
                        "progressItemType": (item.get("progressItemType") or "agreement"),
                        # Provide LLM-friendly key used by ProgressLLMProcessor logs/prompts
                        "progress_type": _ptype_title,
                        "assignee": assignee_display,
                        "dueDate": item.get("dueDate", ""),
                        "resolved": item.get("resolved", False),
                        "meetingDate": item.get("meetingDate", ""),
                    }
                )
            except Exception:
                continue
        settings.PREVIOUS_PROGRESS_ITEMS = filtered_prev

    return payload