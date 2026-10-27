import asyncio
import json
import socketio
import time
from typing import Any, Dict, List, Optional
from loguru import logger
from datetime import datetime, timedelta, timezone
from jose import jwt
from app.config import settings

# convert_name_to_id function removed - conversion now handled in llm_coordinator.py

async def apply_agenda_update(meeting_id: str, agenda_id: str, new_status: str, timestamp_sec: float):
    """
    Handle manual agenda status updates from Teams frontend.
    Updates local analytics and timers without re-emitting the change.
    
    This function:
    1. Maps agenda_id back to agenda titles if needed
    2. Updates analytics state without emitting socket events  
    3. Updates timer manager state based on status changes
    4. Updates LLM processor agenda states for future prompts
    5. Prevents infinite loops from manual updates
    """
    try:
        # Import here to avoid circular imports
        from app.realtime.bot_websocket.websocket import event_handler
        
        logger.info(f"📨 Agenda update received: meeting={meeting_id}, agenda={agenda_id}, status={new_status}, time={timestamp_sec:.1f}s")
        
        # Validate required components are available
        if not event_handler or not event_handler.analytics:
            logger.error("❌ Event handler or analytics not available for manual update")
            return
        
        # Map client states to internal states for consistency
        state_mapping = {
            "remaining": "Not Started",
            "in_discussion": "In Progress",
            "hold": "On Hold", 
            "discussed": "Completed"
        }
        internal_state = state_mapping.get(new_status, new_status)
        
        # Get agenda title from ID mapping if available
        agenda_title = agenda_id
        if agenda_id in settings.AGENDA_ID_TO_TITLE:
            agenda_title = settings.AGENDA_ID_TO_TITLE[agenda_id]
        
        # Update analytics state without emitting socket events
        event_handler.analytics.update_agenda_state_manual(
            agenda_item=agenda_id,  # Could be agenda_id or title
            new_state=new_status,   # Client state (hold, in_discussion, etc.)
            timestamp=timestamp_sec # Timestamp in seconds since meeting start
        )
        
        # Update timer manager with simple wall-clock approach
        if event_handler.agenda_timer_manager and agenda_title:
            # grab timer object
            timer = event_handler.agenda_timer_manager.agenda_timers.get(agenda_title)
            prev_spent = timer.total_time_spent if timer else 0.0

            if new_status == "in_discussion":
                logger.info(f"🕒 Manual RESUME at payload ts={timestamp_sec:.1f}s (rel).  Prev spent={prev_spent:.1f}s")
                event_handler.agenda_timer_manager.start_agenda_timer(agenda_title, at_time=timestamp_sec)
                # after resume, still no extra time counted
                if timer:
                    logger.info(
                        f"    → total_spent remains {prev_spent:.1f}s, remaining {timer.remaining_time:.1f}s"
                    )
            elif new_status == "discussed":
                # Finish timer and fire alert once
                event_handler.agenda_timer_manager.complete_agenda_timer(
                    agenda_title, at_time=timestamp_sec
                )
                if event_handler.agenda_timer_manager._overtime_callback:
                    await event_handler.agenda_timer_manager._overtime_callback(agenda_title)
            else:
                logger.info(f"🕒 Manual PAUSE  at payload ts={timestamp_sec:.1f}s (rel).  Prev spent={prev_spent:.1f}s")
                event_handler.agenda_timer_manager.pause_agenda_timer(agenda_title, at_time=timestamp_sec)
                # fetch new status
                new_status_info = event_handler.agenda_timer_manager.get_timer_status(agenda_title)
                if new_status_info:
                    added = new_status_info["time_spent"] - prev_spent
                    logger.info(
                        f"    → added {added:.1f}s; total_spent={new_status_info['time_spent']:.1f}s; "
                        f"remaining={new_status_info['remaining_time']:.1f}s"
                    )
        
        # Update LLM processor agenda states to keep them in sync
        if hasattr(event_handler, 'llm_coordinator') and event_handler.llm_coordinator and event_handler.llm_coordinator.llm_processor:
            try:
                # Update LLM processor's agenda_states dict
                if agenda_title in event_handler.llm_coordinator.llm_processor.agenda_states:
                    old_state = event_handler.llm_coordinator.llm_processor.agenda_states[agenda_title]
                    event_handler.llm_coordinator.llm_processor.agenda_states[agenda_title] = internal_state
                    logger.info(f"🧠 Updated LLM processor state: '{agenda_title}' {old_state} → {internal_state}")
                else:
                    # Try to find agenda by ID or partial match
                    for title in event_handler.llm_coordinator.llm_processor.agenda_states.keys():
                        if (agenda_id.lower() in title.lower() or 
                            title.lower() in agenda_id.lower() or
                            settings.AGENDA_TITLE_TO_ID.get(title) == agenda_id):
                            old_state = event_handler.llm_coordinator.llm_processor.agenda_states[title]
                            event_handler.llm_coordinator.llm_processor.agenda_states[title] = internal_state
                            logger.info(f"🧠 Updated LLM processor state (matched): '{title}' {old_state} → {internal_state}")
                            break
                    else:
                        logger.warning(f"⚠️ Could not find '{agenda_title}' in LLM processor agenda_states")
                
                # Flush chat history and regenerate system prompt (same as LLM-detected changes)
                logger.info("🧹 Manual state change detected → flushing LLM chat history for consistency")
                
                # Regenerate system prompt with updated states
                from app.ProgressAnalyzer.prompts.agenda_prompt import get_agenda_tracking_prompt
                event_handler.llm_coordinator.llm_processor.system_prompt = get_agenda_tracking_prompt(
                    event_handler.llm_coordinator.llm_processor.agenda_titles,
                    event_handler.llm_coordinator.llm_processor.agenda_descriptions,
                    event_handler.llm_coordinator.llm_processor.agenda_states
                )
                
                # Flush chat history to only keep system prompt (similar to LLM-detected changes)
                event_handler.llm_coordinator.llm_processor.messages = [
                    {"role": "system", "content": event_handler.llm_coordinator.llm_processor.system_prompt}
                ]
                
                logger.info(f"✅ Flushed chat history and updated system prompt for manual agenda change")
                
            except Exception as e:
                logger.error(f"❌ Error updating LLM processor states: {e}")
        
        # Optional: Update agenda _id mapping if this is a new mapping
        if agenda_id not in settings.AGENDA_TITLE_TO_ID.values():
            # Try to find if this agenda_id (_id) corresponds to an existing title
            # This helps build the mapping for future use
            for title in settings.AGENDA_ITEMS.keys():
                if agenda_id.lower() in title.lower() or title.lower() in agenda_id.lower():
                    settings.AGENDA_TITLE_TO_ID[title] = agenda_id
                    settings.AGENDA_ID_TO_TITLE[agenda_id] = title
                    logger.debug(f"🔗 Mapped agenda title '{title}' to _id '{agenda_id}'")
                    break
        
        logger.info(f"✅ Agenda update processed successfully: {agenda_id} → {new_status}")
        
    except Exception as e:
        logger.error(f"❌ Error handling manual agenda update: {e}")
        # Don't re-raise to prevent disrupting the socket connection

def generate_app_specific_token() -> str:
    """
    Generate an app-specific JWT token for Socket.IO authentication
    using the shared secret key from the client server.
    """
    if not settings.SECRET_KEY:
        raise ValueError("SECRET_KEY not configured. Please set the shared secret key in environment variables.")
    
    # Create token payload - now primarily using meeting_id
    payload = {
        "iat": datetime.now(timezone.utc),
        "exp": datetime.now(timezone.utc) + timedelta(hours=1),  # 1 hour expiration
        "app": "meeting-agenda-tasks-ai",
        "meeting_id": settings.MEETING_ID or ""
    }
    
    # Generate JWT token using the shared secret key
    token = jwt.encode(payload, settings.SECRET_KEY, algorithm="HS256")
    logger.info("Generated app-specific JWT token for Socket.IO authentication")
    return token

class MeetingSocketIOClient:
    """
    Socket.IO client for the meeting AI to send active speaker updates
    and agenda state updates to the client Socket.IO server.
    """
    
    def __init__(self, server_url: Optional[str] = None):
        self.server_url = server_url or settings.SOCKET_URL
        # Use built-in Socket.IO reconnection with backoff
        self.sio = socketio.AsyncClient(
            reconnection=True,
            reconnection_attempts=settings.SOCKET_MAX_RETRY_ATTEMPTS,  # Use configurable max attempts
            reconnection_delay=settings.SOCKET_RETRY_DELAY,            # Use configurable initial delay
            reconnection_delay_max=60.0,
            randomization_factor=0.5,
        )
        self.connected = False
        self.connection_attempts = 0
        self.max_retries = settings.SOCKET_MAX_RETRY_ATTEMPTS  # Use configurable retry attempts
        self.retry_delay = settings.SOCKET_RETRY_DELAY  # Use configurable retry delay
        self.backoff_multiplier = settings.SOCKET_RETRY_BACKOFF_MULTIPLIER  # Use configurable backoff
        self.last_meeting_id = None  # Track meeting_id changes
        self._connect_lock = asyncio.Lock()
        self._destroyed = False
        # self.previous_speakers_count = -1  # DISABLED: Track previous speakers array length - no longer needed
        
        self.setup_event_handlers()

    def get_socketio_sid(self, namespace: str = "/") -> Optional[str]:
        """Return the Socket.IO SID for the given namespace if available."""
        try:
            # get_sid returns the Socket.IO namespace-level SID, which matches server socket.id
            return self.sio.get_sid(namespace)  # type: ignore[attr-defined]
        except Exception:
            return None
        
    async def _wait_until_connected(self, timeout: float = 5.0) -> bool:
        """Wait up to timeout seconds for the client to be connected."""
        try:
            loop = asyncio.get_running_loop()
            start = loop.time()
            while not self.is_connected() and (loop.time() - start) < timeout:
                await asyncio.sleep(0.1)
            return self.is_connected()
        except RuntimeError:
            # If no running loop, just return current state
            return self.is_connected()

    async def _emit_with_reconnect(self, event: str, payload: Any, wait_timeout: float = 5.0) -> None:
        """Emit with a short wait for reconnection and one hard-reset retry on failure."""
        # Ensure connection (may kick off auto reconnect)
        await self.ensure_connected()
        if not await self._wait_until_connected(timeout=wait_timeout):
            raise ConnectionError("Socket.IO not connected after wait")

        try:
            await self.sio.emit(event, payload)
            return
        except Exception as e:
            logger.warning(f"Emit '{event}' failed ({e}); attempting hard reset and retry")
            try:
                await self._hard_reset()
                await self.connect()
                if not await self._wait_until_connected(timeout=wait_timeout):
                    raise ConnectionError("Socket.IO not connected after hard reset")
                await self.sio.emit(event, payload)
            except Exception as e2:
                # Re-raise so callers can handle and log precise failure
                raise e2

    def setup_event_handlers(self):
        """Setup Socket.IO event handlers"""
        
        @self.sio.event
        async def connect():
            """Handle successful connection to external server"""
            self.connected = True
            self.connection_attempts = 0  # Reset retry counter on successful connection
            logger.info(f"✅ Connected to client Socket.IO server: {self.server_url}")
            try:
                socketio_sid = self.get_socketio_sid("/")
                if socketio_sid:
                    logger.info(f"🔗 Socket.IO SID: {socketio_sid}")
                else:
                    logger.info("🔗 Socket.IO SID: unknown")
            except Exception:
                logger.info("🔗 Socket.IO SID: unknown")
            
            # Join the room with touchpointId if available; otherwise, wait for startup fetch
            if settings.TOUCHPOINT_ID:
                await self._join_room()
            else:
                logger.warning("⚠️ TOUCHPOINT_ID not set on connect; will join room after startup fetch")
            
            logger.info(f"🔍 Ready to receive broadcast_agenda events...")
            
        @self.sio.event
        async def disconnect():
            """Handle disconnection from external server"""
            self.connected = False
            if self._destroyed:
                logger.warning("⚠️ Disconnect ignored: client destroyed")
                return
            try:
                socketio_sid = self.get_socketio_sid("/")
                if socketio_sid:
                    logger.warning(f"❌ Disconnected from client Socket.IO server: {self.server_url} (socketio_sid={socketio_sid})")
                else:
                    logger.warning(f"❌ Disconnected from client Socket.IO server: {self.server_url}")
            except Exception:
                logger.warning(f"❌ Disconnected from client Socket.IO server: {self.server_url}")
            
        @self.sio.event
        async def connect_error(data):
            """Handle connection error"""
            self.connected = False
            self.connection_attempts += 1
            logger.error(f"❌ Failed to connect to client Socket.IO server (attempt {self.connection_attempts}/{self.max_retries}): {data}")

        

        @self.sio.event
        async def broadcast_agenda(data):
            """
            New broadcast for agenda updates. Payload shape:
            {
              context: { executionId, sprintId, touchpointId, itemId },
              agendaItems: [{ type, id, title, description, status, timings: number[], timeSlot, eventStatus, ... }]
            }
            """
            try:
                parsed = json.loads(data) if isinstance(data, str) else data
                try:
                    pretty = json.dumps(parsed, indent=2, ensure_ascii=False)
                    logger.info(f"📡 RECEIVED broadcast_agenda (parsed):\n{pretty}")
                except Exception:
                    logger.info(f"📡 RECEIVED broadcast_agenda (raw): {data}")
                context = parsed.get("context", {}) if isinstance(parsed, dict) else {}
                agenda_items = []
                if isinstance(parsed, dict):
                    agenda_items = parsed.get("agendaItems") or parsed.get("agendas") or []

                # Validate touchpoint match (server now sends touchpointId)
                touchpoint_id_payload = context.get("touchpointId")
                if touchpoint_id_payload and settings.TOUCHPOINT_ID and touchpoint_id_payload != settings.TOUCHPOINT_ID:
                    logger.info(f"Skipping broadcast for different touchpointId: {touchpoint_id_payload}")
                    return

                # Process each agenda update
                for agenda in agenda_items:
                    try:
                        event_status = agenda.get("eventStatus")
                        if event_status and str(event_status).lower() != "success":
                            logger.info(f"Skipping agenda item due to eventStatus='{event_status}'")
                            continue

                        agenda_id = agenda.get("id")
                        evt_type = agenda.get("type")
                        new_status = agenda.get("status")
                        title = agenda.get("title") or settings.AGENDA_ID_TO_TITLE.get(agenda_id) or agenda_id
                        description = agenda.get("description") or ""
                        timings = agenda.get("timings", []) or []
                        time_slot_minutes = agenda.get("timeSlot")

                        if not agenda_id:
                            logger.warning(f"⚠️ Skipping agenda missing id: {agenda}")
                            continue

                        # Determine latest timestamp in seconds from timings (ms)
                        ts_epoch = time.time()
                        if isinstance(timings, list) and timings:
                            last_ts = timings[-1]
                            try:
                                last_ts = float(last_ts)
                                ts_epoch = last_ts / 1000.0 if last_ts > 1e12 else last_ts
                            except Exception:
                                ts_epoch = time.time()

                        # Normalize status default when absent on create
                        if not new_status:
                            new_status = "remaining"

                        if evt_type == "delete":
                            # Remove from TOUCHPOINT_DATA agenda list
                            if isinstance(settings.TOUCHPOINT_DATA, dict):
                                original = settings.TOUCHPOINT_DATA.get("agenda", []) or []
                                filtered = [x for x in original if x.get("id") != agenda_id]
                                settings.TOUCHPOINT_DATA["agenda"] = filtered
                            # Remove from mappings and timer/LLM
                            old_title = settings.AGENDA_ID_TO_TITLE.pop(agenda_id, None)
                            if old_title:
                                settings.AGENDA_TITLE_TO_ID.pop(old_title, None)
                                settings.AGENDA_ITEMS.pop(old_title, None)
                                try:
                                    settings.AGENDA_ITEMS_FULL = [x for x in settings.AGENDA_ITEMS_FULL if x.get("id") != agenda_id]
                                except Exception:
                                    pass
                                try:
                                    from app.realtime.bot_websocket.websocket import event_handler
                                    if event_handler and event_handler.agenda_timer_manager:
                                        if old_title == event_handler.agenda_timer_manager.current_active_agenda:
                                            event_handler.agenda_timer_manager.pause_agenda_timer(old_title)
                                        if old_title in event_handler.agenda_timer_manager.agenda_timers:
                                            del event_handler.agenda_timer_manager.agenda_timers[old_title]
                                    if event_handler and event_handler.llm_coordinator and event_handler.llm_coordinator.llm_processor:
                                        lp = event_handler.llm_coordinator.llm_processor
                                        if old_title in lp.agenda_titles:
                                            lp.agenda_titles = [t for t in lp.agenda_titles if t != old_title]
                                            lp.agenda_descriptions.pop(old_title, None)
                                            lp.agenda_states.pop(old_title, None)
                                            from app.ProgressAnalyzer.prompts.agenda_prompt import get_agenda_tracking_prompt
                                            lp.system_prompt = get_agenda_tracking_prompt(lp.agenda_titles, lp.agenda_descriptions, lp.agenda_states)
                                            lp.messages = [{"role": "system", "content": lp.system_prompt}]
                                except Exception:
                                    pass
                            continue

                        if evt_type == "create":
                            # Ensure mappings
                            settings.AGENDA_ID_TO_TITLE[agenda_id] = title
                            settings.AGENDA_TITLE_TO_ID[title] = agenda_id

                            # Update TOUCHPOINT_DATA
                            if isinstance(settings.TOUCHPOINT_DATA, dict):
                                agenda_list = settings.TOUCHPOINT_DATA.setdefault("agenda", [])
                                exists = any(x.get("id") == agenda_id for x in agenda_list)
                                if not exists:
                                    agenda_obj = {"id": agenda_id, "title": title, "status": new_status, "timings": timings[:]}
                                    # Optionally store description/timeSlot for richer UI
                                    agenda_obj["description"] = description
                                    agenda_obj["timeSlot"] = time_slot_minutes
                                    agenda_list.append(agenda_obj)

                            # Update AGENDA_ITEMS and FULL list
                            settings.AGENDA_ITEMS[title] = description
                            try:
                                if not any(x.get("id") == agenda_id for x in settings.AGENDA_ITEMS_FULL):
                                    settings.AGENDA_ITEMS_FULL.append({
                                        "id": agenda_id,
                                        "title": title,
                                        "description": description,
                                        "status": new_status,
                                        "timeSlot": time_slot_minutes,
                                    })
                            except Exception:
                                pass

                            # Add to LLM and timers
                            try:
                                from app.realtime.bot_websocket.websocket import event_handler
                                if event_handler and event_handler.llm_coordinator and event_handler.llm_coordinator.llm_processor:
                                    lp = event_handler.llm_coordinator.llm_processor
                                    if title not in lp.agenda_titles:
                                        lp.agenda_titles.append(title)
                                    lp.agenda_descriptions[title] = description
                                    map_state = {
                                        "remaining": "Not Started",
                                        "in_discussion": "In Progress",
                                        "hold": "On Hold",
                                        "discussed": "Completed",
                                    }
                                    lp.agenda_states[title] = map_state.get(new_status, "Not Started")
                                    from app.ProgressAnalyzer.prompts.agenda_prompt import get_agenda_tracking_prompt
                                    lp.system_prompt = get_agenda_tracking_prompt(lp.agenda_titles, lp.agenda_descriptions, lp.agenda_states)
                                    lp.messages = [{"role": "system", "content": lp.system_prompt}]
                                if event_handler and event_handler.agenda_timer_manager:
                                    seconds = 300
                                    try:
                                        if isinstance(time_slot_minutes, (int, float)) and time_slot_minutes > 0:
                                            seconds = int(time_slot_minutes * 60)
                                    except Exception:
                                        pass
                                    event_handler.agenda_timer_manager.initialize_agenda_timers([
                                        {"title": title, "time": seconds}
                                    ])
                            except Exception:
                                pass

                        if evt_type == "update":
                            # Update TOUCHPOINT_DATA
                            try:
                                if isinstance(settings.TOUCHPOINT_DATA, dict):
                                    agenda_list = settings.TOUCHPOINT_DATA.setdefault("agenda", [])
                                    match = None
                                    for item in agenda_list:
                                        if item.get("id") == agenda_id:
                                            match = item
                                            break
                                    if match is None:
                                        match = {"id": agenda_id}
                                        agenda_list.append(match)
                                    # Keep track of previous title for mapping updates
                                    prev_title = match.get("title")
                                    match["title"] = title
                                    match["status"] = new_status or match.get("status")
                                    match["description"] = description
                                    match["timeSlot"] = time_slot_minutes
                                    # Merge timings (ensure ms)
                                    existing_timings = match.get("timings", []) or []
                                    if timings:
                                        for t in timings:
                                            try:
                                                t_int = int(t)
                                            except Exception:
                                                continue
                                            if not existing_timings or existing_timings[-1] != t_int:
                                                existing_timings.append(t_int)
                                    match["timings"] = existing_timings

                                    # Update title/id mappings if title changed
                                    if prev_title and prev_title != title:
                                        settings.AGENDA_TITLE_TO_ID.pop(prev_title, None)
                                    settings.AGENDA_TITLE_TO_ID[title] = agenda_id
                                    settings.AGENDA_ID_TO_TITLE[agenda_id] = title
                            except Exception as e:
                                logger.warning(f"⚠️ Failed to update TOUCHPOINT_DATA on update: {e}")

                            # Update AGENDA_ITEMS and FULL list
                            try:
                                settings.AGENDA_ITEMS[title] = description
                                found = False
                                for item in settings.AGENDA_ITEMS_FULL:
                                    if item.get("id") == agenda_id:
                                        item.update({
                                            "title": title,
                                            "description": description,
                                            "status": new_status,
                                            "timeSlot": time_slot_minutes,
                                        })
                                        found = True
                                        break
                                if not found:
                                    settings.AGENDA_ITEMS_FULL.append({
                                        "id": agenda_id,
                                        "title": title,
                                        "description": description,
                                        "status": new_status,
                                        "timeSlot": time_slot_minutes,
                                    })
                            except Exception:
                                pass

                            # Update LLM state and handle title changes
                            try:
                                from app.realtime.bot_websocket.websocket import event_handler
                                if event_handler and event_handler.llm_coordinator and event_handler.llm_coordinator.llm_processor:
                                    lp = event_handler.llm_coordinator.llm_processor
                                    # If title changed, migrate keys
                                    matching_title = None
                                    if title in lp.agenda_titles:
                                        matching_title = title
                                    else:
                                        # Try previous title via mapping
                                        old_title = settings.AGENDA_ID_TO_TITLE.get(agenda_id)
                                        if old_title and old_title in lp.agenda_titles:
                                            matching_title = old_title
                                    if matching_title and matching_title != title:
                                        # rename in lists/dicts
                                        lp.agenda_titles = [t if t != matching_title else title for t in lp.agenda_titles]
                                        lp.agenda_descriptions[title] = description
                                        lp.agenda_descriptions.pop(matching_title, None)
                                        lp.agenda_states[title] = lp.agenda_states.pop(matching_title, "Not Started")
                                    else:
                                        if title not in lp.agenda_titles:
                                            lp.agenda_titles.append(title)
                                        lp.agenda_descriptions[title] = description
                                    map_state = {
                                        "remaining": "Not Started",
                                        "in_discussion": "In Progress",
                                        "hold": "On Hold",
                                        "discussed": "Completed",
                                    }
                                    lp.agenda_states[title] = map_state.get(new_status or "remaining", lp.agenda_states.get(title, "Not Started"))
                                    from app.ProgressAnalyzer.prompts.agenda_prompt import get_agenda_tracking_prompt
                                    lp.system_prompt = get_agenda_tracking_prompt(lp.agenda_titles, lp.agenda_descriptions, lp.agenda_states)
                                    lp.messages = [{"role": "system", "content": lp.system_prompt}]
                            except Exception:
                                pass

                        # Update timers/LLM through common helper for status/timing alignment
                        await apply_agenda_update(settings.TOUCHPOINT_ID or "", agenda_id, new_status, ts_epoch)
                    except Exception as e:
                        logger.warning(f"⚠️ Failed to process agenda item '{agenda}': {e}")

                logger.info("✅ Processed broadcast_agenda")
            except Exception as e:
                logger.error(f"❌ Error handling broadcast_agenda: {e}")

        @self.sio.event
        async def broadcast_progress_item(data):
            """Handle server broadcast for progress item updates."""
            try:
                parsed = json.loads(data) if isinstance(data, str) else data
                try:
                    pretty = json.dumps(parsed, indent=2, ensure_ascii=False)
                    logger.info(f"📡 RECEIVED broadcast_progress_item (parsed):\n{pretty}")
                except Exception:
                    logger.info(f"📡 RECEIVED broadcast_progress_item (raw): {data}")
                context = parsed.get("context", {}) if isinstance(parsed, dict) else {}
                items = []
                if isinstance(parsed, dict):
                    items = parsed.get("progressItems") or parsed.get("progressItem") or []

                touchpoint_id_payload = context.get("touchpointId")
                if touchpoint_id_payload and settings.TOUCHPOINT_ID and touchpoint_id_payload != settings.TOUCHPOINT_ID:
                    logger.info(f"Skipping progress broadcast for different touchpointId: {touchpoint_id_payload}")
                    return

                # Ensure storage
                if isinstance(settings.TOUCHPOINT_DATA, dict):
                    settings.TOUCHPOINT_DATA.setdefault("progressItems", [])

                for it in items:
                    # Only process successful events (skip errors)
                    try:
                        es = it.get("eventStatus")
                        if es and str(es).lower() != "success":
                            logger.info(f"Skipping progress item due to eventStatus='{es}' (id={it.get('id')})")
                            continue
                    except Exception:
                        pass

                    pid = it.get("id")
                    status = it.get("status")
                    evt_type = it.get("type")
                    if isinstance(settings.TOUCHPOINT_DATA, dict):
                        prog_list = settings.TOUCHPOINT_DATA.setdefault("progressItems", [])
                        # Find existing
                        match = None
                        for existing in prog_list:
                            if existing.get("id") == pid:
                                match = existing
                                break
                        if match:
                            match.update(it)
                        else:
                            prog_list.append(it)

                        # If status is rejected → keep in TOUCHPOINT_DATA but remove from LLM prompt immediately
                        if status == "rejected":
                            try:
                                from app.realtime.bot_websocket.websocket import event_handler
                                if event_handler and hasattr(event_handler, 'llm_coordinator') and event_handler.llm_coordinator:
                                    event_handler.llm_coordinator.remove_progress_item(pid)
                            except Exception:
                                pass

                        # If delete → keep in TOUCHPOINT_DATA but remove from LLM prompt
                        if evt_type == "delete":
                            try:
                                from app.realtime.bot_websocket.websocket import event_handler
                                if event_handler and hasattr(event_handler, 'llm_coordinator') and event_handler.llm_coordinator:
                                    event_handler.llm_coordinator.remove_progress_item(pid)
                            except Exception:
                                pass
                logger.info("✅ Processed broadcast_progress_item")
            except Exception as e:
                logger.error(f"❌ Error handling broadcast_progress_item: {e}")

        @self.sio.event
        async def pong(data):
            """Handle pong response from server"""
            logger.debug(f"📡 Received pong from client server: {data}")

    async def _join_room(self):
        """Join the room with the current touchpoint id"""
        try:
            touchpoint_id = settings.TOUCHPOINT_ID
            if not touchpoint_id:
                logger.warning("⚠️ No touchpointId available, cannot join room")
                return
            
            payload = json.dumps({"touchpointId": touchpoint_id})
            await self.sio.emit('join_room', payload)
            logger.info(f"🏠 Joined room with touchpointId: {touchpoint_id}")
        except Exception as e:
            logger.error(f"❌ Failed to join room: {e}")

    async def join_current_room(self):
        """Public method to join the room for the current meeting id (idempotent)."""
        await self._join_room()

    async def _hard_reset(self):
        """Hard reset the underlying Socket.IO client when state is inconsistent."""
        payload = None
        mapped_state = None
        try:
            if self.sio.connected:
                try:
                    await self.sio.disconnect()
                except Exception:
                    pass
            # Recreate the AsyncClient with auto reconnection and rebind handlers
            self.sio = socketio.AsyncClient(
                reconnection=True,
                reconnection_attempts=settings.SOCKET_MAX_RETRY_ATTEMPTS,  # Use configurable max attempts
                reconnection_delay=settings.SOCKET_RETRY_DELAY,            # Use configurable initial delay
                reconnection_delay_max=60.0,
                randomization_factor=0.5,
            )
            self.connected = False
            self.setup_event_handlers()
            logger.info("🧼 Hard reset Socket.IO AsyncClient instance")
        except Exception as e:
            logger.error(f"❌ Hard reset failed: {e}")

    async def ensure_connected(self) -> bool:
        """Ensure the client is connected; trigger a connect once if not."""
        if self.connected and self.sio.connected:
            return True
        # If flags disagree, do a quick hard reset to realign
        if self.connected != bool(self.sio.connected):
            logger.warning("⚠️ Inconsistent connection state detected; performing hard reset")
            await self._hard_reset()
        return await self.connect()
        
    async def connect(self):
        """Connect to the external Socket.IO server with JWT authentication"""
        async with self._connect_lock:
            if self.connected and self.sio.connected:
                logger.debug("Already connected to client Socket.IO server")
                return True
            # Check if meeting_id changed while connected
            current_meeting_id = settings.MEETING_ID
            if self.last_meeting_id and self.last_meeting_id != current_meeting_id and self.sio.connected:
                logger.info(f"🔄 Meeting ID changed while connected, reconnecting...")
                await self.disconnect()
            
            try:
                # Update tracked meeting_id
                self.last_meeting_id = settings.MEETING_ID
                
                # Generate fresh JWT token with current meeting id
                token = generate_app_specific_token()
                
                # Set up authentication headers
                headers = {
                    'Authorization': f'Bearer {token}'
                }
                
                logger.info(f"🔐 Attempting connection with JWT authentication...")
                logger.info(f"🔍 Meeting ID: {self.last_meeting_id}")
                self.connection_attempts += 1
                
                # Connect with authentication headers, restrict to default namespace only
                await self.sio.connect(
                    self.server_url,
                    headers=headers,
                    namespaces=['/'],
                )
                
                # Connection success is handled by the connect event handler
                return True
                
            except Exception as e:
                # If default namespace is actually connected, treat as success
                try:
                    if self.sio.connected or self.connected:
                        logger.warning(
                            f"Partial namespace connection failure ignored (default namespace connected): {e}"
                        )
                        return True
                except Exception:
                    pass
                logger.error(f"Failed to connect to client Socket.IO server: {e}")
                self.connected = False
                return False

    async def disconnect(self):
        """Disconnect from the external Socket.IO server"""
        try:
            if self.sio.connected:
                await self.sio.disconnect()
        finally:
            self.connected = False
            logger.info(f"🔌 Disconnected from client Socket.IO server: {self.server_url}")

    def is_connected(self) -> bool:
        """Check if currently connected"""
        return self.connected and self.sio.connected

    # DISABLED: Current speaker tracking functionality - no longer needed
    # async def send_active_speakers_update(self, speakers: list):
    #     """
    #     Send current speaker update to the client Socket.IO server
    #     
    #     Parameters:
    #     -----------
    #     speakers: List[Dict]
    #         A list of active speakers
    #         Example: [{"displayName": "Jane Doe", "email": "jane@example.com"}]
    #     """
    #     if not self.connected:
    #         logger.warning("Not connected to client server. Cannot send current speaker update.")
    #         return False
    #         
    #     try:
    #         # Check if speakers array length has changed from previous send
    #         current_speakers_count = len(speakers)
    #         if current_speakers_count == self.previous_speakers_count:
    #             logger.debug(f"📡 Speakers count unchanged ({current_speakers_count}), skipping update")
    #             return True
    #             
    #         # Update the previous count
    #         self.previous_speakers_count = current_speakers_count
    #             
    #         # Try to get current agenda from timer manager if available
    #         current_agenda_id = "current"  # Default fallback
    #         try:
    #             from app.realtime.bot_websocket.websocket import event_handler
    #             if event_handler and event_handler.agenda_timer_manager:
    #                 current_agenda = event_handler.agenda_timer_manager.current_active_agenda
    #                 if current_agenda:
    #                     # Use agenda title to _id mapping if available (now uses _id instead of agenda_id)
    #                     current_agenda_id = settings.AGENDA_TITLE_TO_ID.get(current_agenda, current_agenda)
    #         except Exception:
    #             pass  # Use default if timer manager not available
    #         
    #         # Build speakers array with speaker_name and speaker_email
    #         speakers_array = []
    #         for speaker in speakers:
    #             speaker_obj = {
    #                 "speaker_name": speaker.get('displayName', 'Unknown Speaker'),
    #                 "speaker_email": speaker.get('email', 'unknown@example.com')
    #             }
    #             speakers_array.append(speaker_obj)
    #             
    #         payload = {
    #             "meeting_id": settings.MEETING_ID or "unknown",
    #             "agenda_id": current_agenda_id,
    #             "speakers": speakers_array
    #         }
    #         
    #         await self.sio.emit('current_speaker_update', json.dumps(payload))
    #         logger.debug(f"📡 Sent current speaker update (stringified): {len(speakers_array)} speakers, agenda_id={current_agenda_id}")
    #         return True
    #     except Exception as e:
    #         logger.error(f"Failed to send current speaker update: {e}")
    #         return False

    async def send_agenda_status_update(self, agenda_item: str = "", new_state: str = "", 
                                       timestamp: float = 0.0, meeting_id: Optional[str] = None,
                                       agenda_id: Optional[str] = None,
                                       batch_updates: Optional[List[Dict[str, Any]]] = None):
        """
        Send agenda status update to the client Socket.IO server using new payload shape
        
        Parameters:
        -----------
        agenda_item: str
            The name of the agenda item (used to lookup agenda_id)
        new_state: str
            The new state (hold, in_discussion, discussed, remaining)
        timestamp: float
            Timestamp in seconds since meeting start (not used for client timestamp)
        meeting_id: str
            Optional meeting ID (uses settings if not provided)
        agenda_id: str
            Optional agenda ID (uses agenda_item if not provided)
        """
        if not await self.ensure_connected():
            logger.error(f"🚫 SEND FAILED: Not connected to client server. Cannot send agenda status update for '{agenda_item}'")
            return False
            
        try:
            # Support passing a list via agenda_item for backward compat
            if batch_updates is None and isinstance(agenda_item, list):
                batch_updates = agenda_item  # type: ignore[assignment]

            # Map internal states to client API states
            state_mapping = {
                "Not Started": "remaining",
                "In Progress": "in_discussion",
                "On Hold": "hold",
                "Completed": "discussed",
                "Discussed": "discussed",
                # Additional mappings for common variations
                "inprogress": "in_discussion",
                "completed": "discussed",
                "onhold": "hold",
                "not started": "remaining"
            }

            actual_meeting_id = meeting_id or settings.TOUCHPOINT_ID or "unknown"

            # Batch mode
            if batch_updates:
                payload = None
                agenda_items_payload: List[Dict[str, Any]] = []

                # Prepare TOUCHPOINT_DATA container
                try:
                    if isinstance(settings.TOUCHPOINT_DATA, dict):
                        settings.TOUCHPOINT_DATA.setdefault("agenda", [])
                except Exception:
                    pass

                for upd in batch_updates:
                    try:
                        agenda_title = upd.get("agenda_item")
                        provided_agenda_id = upd.get("agenda_id")
                        upd_state = upd.get("new_state") or upd.get("status")
                        ts_seconds = float(upd.get("timestamp", time.time()))
                        ts_ms = int(ts_seconds * 1000)
                        evt_type = upd.get("type") or "update"

                        mapped_state = state_mapping.get(upd_state, str(upd_state).lower().replace(" ", "_"))
                        actual_agenda_id = provided_agenda_id or settings.AGENDA_TITLE_TO_ID.get(agenda_title, agenda_title or "")

                        # Update TOUCHPOINT_DATA per agenda timings and status
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
                                existing_timings.append(ts_ms)
                                match["timings"] = existing_timings
                        except Exception as e:
                            logger.warning(f"⚠️ Failed to update TOUCHPOINT_DATA on batch agenda send: {e}")

                        # Fetch timings for payload
                        timings_for_agenda: List[int] = []
                        try:
                            if isinstance(settings.TOUCHPOINT_DATA, dict):
                                for item in settings.TOUCHPOINT_DATA.get("agenda", []):
                                    if item.get("id") == actual_agenda_id:
                                        timings_for_agenda = item.get("timings", []) or []
                                        break
                        except Exception:
                            timings_for_agenda = [ts_ms]

                        agenda_items_payload.append({
                            "type": evt_type,
                            "id": actual_agenda_id,
                            "status": mapped_state,
                            "timings": timings_for_agenda,
                        })
                    except Exception as inner_e:
                        logger.warning(f"⚠️ Skipping one agenda update due to error: {inner_e}")

                payload = {
                    "context": {
                        "executionId": settings.EXECUTION_ID or "",
                        "sprintId": settings.SPRINT_ID or "",
                        "touchpointId": actual_meeting_id,
                        "itemId": settings.ITEM_ID or "",
                    },
                    "agendaItems": agenda_items_payload,
                }

                try:
                    socketio_sid = self.get_socketio_sid("/")
                except Exception:
                    socketio_sid = None
                logger.info("📤 Sending agenda batch payload:")
                logger.info(f"  └─ touchpointId: '{actual_meeting_id}'  items: {len(agenda_items_payload)}  socketio_sid: {socketio_sid if socketio_sid else 'unknown'}")
                try:
                    pretty = json.dumps(payload, indent=2)
                    logger.info(f"📦 agenda batch payload JSON:\n{pretty}")
                except Exception:
                    pass

                await self._emit_with_reconnect('agenda', json.dumps(payload))
                logger.info("✅ Successfully sent agenda batch updates")
                return True

            # Single item mode
            # Convert timestamp seconds to ms for timings array
            actual_timestamp_ms = int(timestamp * 1000)

            logger.info(f"📋 send_agenda_status_update called:")
            logger.info(f"  └─ agenda_item: '{agenda_item}'")
            logger.info(f"  └─ new_state: '{new_state}'")
            logger.info(f"  └─ timestamp (input): {timestamp:.3f} seconds")
            logger.info(f"  └─ timestamp (converted): {actual_timestamp_ms} milliseconds")

            mapped_state = state_mapping.get(new_state, new_state.lower().replace(" ", "_"))

            # Lookup IDs
            actual_agenda_id = agenda_id or settings.AGENDA_TITLE_TO_ID.get(agenda_item, agenda_item)

            # Update TOUCHPOINT_DATA timings and status for this agenda
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
                        "timings": settings.TOUCHPOINT_DATA.get("agenda", []) if isinstance(settings.TOUCHPOINT_DATA, dict) else None
                    }
                ]
            }
            # Ensure timings in payload is correct for this agenda id only
            try:
                timings_for_agenda = []
                if isinstance(settings.TOUCHPOINT_DATA, dict):
                    for item in settings.TOUCHPOINT_DATA.get("agenda", []):
                        if item.get("id") == actual_agenda_id:
                            timings_for_agenda = item.get("timings", []) or []
                            break
                payload["agendaItems"][0]["timings"] = timings_for_agenda
            except Exception:
                payload["agendaItems"][0]["timings"] = [actual_timestamp_ms]

            logger.info(f"📤 Sending agenda payload:")
            logger.info(f"  └─ touchpointId: '{actual_meeting_id}'")
            logger.info(f"  └─ agenda_id: '{actual_agenda_id}'")
            logger.info(f"  └─ status: '{mapped_state}' (mapped from '{new_state}')")
            logger.info(f"  └─ timestamp: {actual_timestamp_ms} ms (epoch)")
            try:
                socketio_sid = self.get_socketio_sid("/")
                logger.info(f"  └─ socketio_sid: {socketio_sid if socketio_sid else 'unknown'}")
            except Exception:
                logger.info("  └─ socketio_sid: unknown")

            try:
                pretty = json.dumps(payload, indent=2, ensure_ascii=False)
                logger.info(f"📤 OUTGOING agenda payload (single):\n{pretty}")
            except Exception:
                logger.info(f"📤 OUTGOING agenda payload (single raw): {payload}")
            await self._emit_with_reconnect('agenda', json.dumps(payload))
            logger.info(f"✅ Successfully sent agenda status update for '{agenda_item}' → '{mapped_state}'")
            return True
        except Exception as e:
            logger.error(f"Failed to send agenda status update: {e}")
            # Retry once after ensure_connected
            try:
                payload  # type: ignore  # ensure variable considered bound
                mapped_state  # type: ignore
            except NameError:
                payload = None
                mapped_state = None
            if payload is not None and mapped_state is not None and await self.ensure_connected():
                try:
                    try:
                        pretty2 = json.dumps(payload, indent=2, ensure_ascii=False)
                        logger.info(f"📤 OUTGOING agenda payload (retry):\n{pretty2}")
                    except Exception:
                        logger.info(f"📤 OUTGOING agenda payload (retry raw): {payload}")
                    await self._emit_with_reconnect('agenda', json.dumps(payload))
                    logger.info(f"✅ Successfully re-sent agenda status update for '{agenda_item}' → '{mapped_state}'")
                    return True
                except Exception as e2:
                    logger.error(f"Failed to re-send agenda status update: {e2}")
            return False


    async def send_progress_item_update(self, progress_item: dict, meeting_id: Optional[str] = None, progress_items: Optional[List[dict]] = None):
        """
        Send progress item updates with a single function supporting both single and batch.
        Rules:
        - type: 'create' if item doesn't exist, else 'update'
        - status: default 'pending' for new; retain existing if present in TOUCHPOINT_DATA
        - agendaItem: include current in-discussion agenda id; otherwise null
        - itemRelation, touchPointOrigin: send keys (null for itemRelation, origin id for touchPointOrigin)
        """
        if not await self.ensure_connected():
            logger.error(f"🚫 SEND FAILED: Not connected to client server. Cannot send progress item update for '{progress_item.get('id', 'unknown') if isinstance(progress_item, dict) else 'batch'}'")
            return False

        payload = None
        try:
            actual_meeting_id = meeting_id or settings.TOUCHPOINT_ID or "unknown"

            # Support passing a list via progress_item for backward compat
            if progress_items is None and isinstance(progress_item, list):
                progress_items = progress_item  # type: ignore[assignment]

            # Batch mode
            if progress_items is not None:
                # Prepare existing list
                existing_items: List[dict] = []
                if isinstance(settings.TOUCHPOINT_DATA, dict):
                    existing_items = settings.TOUCHPOINT_DATA.get("progressItems", []) or []

                # Compute highest existing order (start at -1 so first create → 0)
                highest_order = -1
                try:
                    for it in existing_items:
                        try:
                            ord_val = int(it.get("order", 0) or 0)
                            if ord_val > highest_order:
                                highest_order = ord_val
                        except Exception:
                            continue
                except Exception:
                    highest_order = -1

                outgoing_list: List[dict] = []

                for incoming in progress_items:
                    try:
                        progress_id = incoming.get("id")
                        existing = None
                        if progress_id:
                            for it in existing_items:
                                if it.get("id") == progress_id:
                                    existing = it
                                    break

                        event_type = "update" if existing else "create"
                        status = incoming.get("status") if event_type == "update" else "pending"
                        if event_type == "update" and not status:
                            status = (existing or {}).get("status") or "pending"

                        resolved = bool(incoming.get("resolved", False)) if event_type == "update" else False

                        # Determine agendaItem id based on current active agenda
                        agenda_item_id: Optional[str] = None
                        try:
                            from app.realtime.bot_websocket.websocket import event_handler  # late import to avoid cycles
                            if event_handler and event_handler.agenda_timer_manager:
                                current_agenda = event_handler.agenda_timer_manager.current_active_agenda
                                if current_agenda:
                                    ag_id = settings.AGENDA_TITLE_TO_ID.get(current_agenda)
                                    if ag_id:
                                        agenda_item_id = ag_id
                        except Exception:
                            pass

                        item_relation_id = None
                        touchpoint_origin: Optional[Any] = None
                        # For updates, prefer per-item touchPointOrigin and meetingDate from the item itself
                        if event_type == "update":
                            try:
                                tpo = incoming.get("touchPointOrigin")
                                if isinstance(tpo, dict) and tpo.get("id"):
                                    touchpoint_origin = {
                                        "id": tpo.get("id"),
                                        "title": tpo.get("title"),
                                        "sprintId": tpo.get("sprintId"),
                                        "itemId": tpo.get("itemId"),
                                    }
                                elif isinstance(tpo, str) and tpo:
                                    touchpoint_origin = tpo
                                elif existing and isinstance(existing.get("touchPointOrigin"), (dict, str)):
                                    touchpoint_origin = existing.get("touchPointOrigin")
                            except Exception:
                                pass
                        # For creates, set itemRelation to the current ITEM_ID
                        if event_type == "create":
                            try:
                                item_relation_id = settings.ITEM_ID or None
                            except Exception:
                                item_relation_id = None
                        # For creates, or if missing above, fall back to top-level TOUCHPOINT_DATA id
                        if touchpoint_origin is None:
                            try:
                                if isinstance(settings.TOUCHPOINT_DATA, dict):
                                    tp_id = settings.TOUCHPOINT_DATA.get("id")
                                    if tp_id:
                                        touchpoint_origin = tp_id
                            except Exception:
                                pass

                        description_value = incoming.get("description", "")
                        if isinstance(description_value, dict):
                            description_value = ""

                        outgoing_item = {
                            "status": status,
                            "type": event_type,
                            "id": progress_id,
                            "title": "",
                            "description": description_value,
                            "progressItemType": (incoming.get("progressItemType", "agreement") or "agreement").lower(),
                            "assignee": incoming.get("assignee"),
                            "dueDate": incoming.get("dueDate"),
                            "resolved": resolved,
                            "meetingDate": incoming.get("meetingDate") or (existing or {}).get("meetingDate") or settings.MEETING_DATE or "",
                            "agendaItem": agenda_item_id,
                            "itemRelation": item_relation_id,
                            "touchPointOrigin": touchpoint_origin or actual_meeting_id,
                        }

                        try:
                            if event_type == "create":
                                # First create when none exist → order 0
                                outgoing_item["order"] = highest_order + 1
                                highest_order = outgoing_item["order"]
                            else:
                                if "order" in incoming and incoming.get("order") is not None:
                                    outgoing_item["order"] = int(incoming.get("order"))
                                elif existing and existing.get("order") is not None:
                                    outgoing_item["order"] = existing.get("order")
                        except Exception:
                            pass

                        outgoing_list.append(outgoing_item)

                        # Update TOUCHPOINT_DATA immediately
                        try:
                            if isinstance(settings.TOUCHPOINT_DATA, dict):
                                prog_list = settings.TOUCHPOINT_DATA.setdefault("progressItems", [])
                                if existing:
                                    existing.update(outgoing_item)
                                else:
                                    prog_list.append(outgoing_item)
                        except Exception as e:
                            logger.warning(f"⚠️ Failed to update TOUCHPOINT_DATA on batch progress send: {e}")
                    except Exception as inner_e:
                        logger.warning(f"⚠️ Skipping one progress item due to error: {inner_e}")

                payload = {
                    "context": {
                        "executionId": settings.EXECUTION_ID or "",
                        "sprintId": settings.SPRINT_ID or "",
                        "touchpointId": actual_meeting_id,
                        "itemId": settings.ITEM_ID or "",
                    },
                    "progressItems": outgoing_list,
                }

                try:
                    socketio_sid = self.get_socketio_sid("/")
                except Exception:
                    socketio_sid = None
                logger.info("📤 Sending progress batch payload:")
                logger.info(f"  └─ touchpointId: '{actual_meeting_id}'  items: {len(outgoing_list)}  socketio_sid: {socketio_sid if socketio_sid else 'unknown'}")
                try:
                    pretty = json.dumps(payload, indent=2)
                    logger.debug(f"📦 progress batch payload JSON:\n{pretty}")
                except Exception:
                    pass

                await self._emit_with_reconnect('progress_item', json.dumps(payload))
                logger.info("✅ Successfully sent batch progress items")
                return True

            # Single item mode
            # Determine existence in TOUCHPOINT_DATA
            existing_items = []
            if isinstance(settings.TOUCHPOINT_DATA, dict):
                existing_items = settings.TOUCHPOINT_DATA.get("progressItems", []) or []
            progress_id = progress_item.get("id")
            existing = None
            if progress_id:
                for it in existing_items:
                    if it.get("id") == progress_id:
                        existing = it
                        break

            event_type = "update" if existing else "create"

            # Compute highest order across all progress items (start at -1 so first create → 0)
            highest_order = -1
            try:
                for it in existing_items:
                    try:
                        ord_val = int(it.get("order", 0) or 0)
                        if ord_val > highest_order:
                            highest_order = ord_val
                    except Exception:
                        continue
            except Exception:
                highest_order = -1

            # Determine status: create → pending always; update → keep provided or existing
            if event_type == "create":
                status = "pending"
            else:
                status = progress_item.get("status") or (existing or {}).get("status") or "pending"

            # Determine resolved
            resolved = bool(progress_item.get("resolved", False)) if event_type == "update" else False

            # Determine agendaItem id if current agenda is in discussion
            agenda_item_id: Optional[str] = None
            try:
                from app.realtime.bot_websocket.websocket import event_handler  # late import to avoid cycles
                if event_handler and event_handler.agenda_timer_manager:
                    current_agenda = event_handler.agenda_timer_manager.current_active_agenda
                    if current_agenda:
                        agenda_id = settings.AGENDA_TITLE_TO_ID.get(current_agenda)
                        if agenda_id:
                            agenda_item_id = agenda_id
            except Exception:
                pass

            # itemRelation default and touchPointOrigin as id string only
            item_relation_id = None
            # Determine touchPointOrigin and meetingDate
            touchpoint_origin: Optional[Any] = None
            meeting_date_value: Optional[str] = None
            try:
                if event_type == "update":
                    # Prefer fields from the provided progress_item first
                    tpo = progress_item.get("touchPointOrigin")
                    if isinstance(tpo, dict) and tpo.get("id"):
                        touchpoint_origin = {
                            "id": tpo.get("id"),
                            "title": tpo.get("title"),
                            "sprintId": tpo.get("sprintId"),
                            "itemId": tpo.get("itemId"),
                        }
                    elif isinstance(tpo, str) and tpo:
                        touchpoint_origin = tpo
                    elif existing and isinstance(existing.get("touchPointOrigin"), (dict, str)):
                        touchpoint_origin = existing.get("touchPointOrigin")
                    meeting_date_value = progress_item.get("meetingDate") or (existing or {}).get("meetingDate")
                # For creates, or if missing above, fall back to top-level TOUCHPOINT_DATA id
                if touchpoint_origin is None and isinstance(settings.TOUCHPOINT_DATA, dict):
                    tp_id = settings.TOUCHPOINT_DATA.get("id")
                    if tp_id:
                        touchpoint_origin = tp_id
            except Exception:
                pass
            if not meeting_date_value:
                meeting_date_value = settings.MEETING_DATE or ""

            # For creates, set itemRelation to the current ITEM_ID
            if event_type == "create":
                try:
                    item_relation_id = settings.ITEM_ID or None
                except Exception:
                    item_relation_id = None

            # Build outgoing item
            outgoing_item = {
                "status": status,
                "type": event_type,
                "id": progress_id,
                "title": "",
                "description": progress_item.get("description", ""),
                "progressItemType": (progress_item.get("progressItemType", "agreement") or "agreement").lower(),
                "assignee": progress_item.get("assignee"),  # already an ID or None
                "dueDate": progress_item.get("dueDate"),    # YYYY-MM-DD or None
                "resolved": resolved,
                # Prefer per-item meetingDate on updates else fallback to existing/global
                "meetingDate": meeting_date_value,
                "agendaItem": agenda_item_id,
                "itemRelation": item_relation_id,
                # Handle both object or id string
                "touchPointOrigin": touchpoint_origin or actual_meeting_id,
            }

            # Assign order: create → highest+1; update → keep provided or existing
            try:
                if event_type == "create":
                    # First create when none exist → order 0
                    outgoing_item["order"] = highest_order + 1
                else:
                    if "order" in progress_item and progress_item.get("order") is not None:
                        outgoing_item["order"] = int(progress_item.get("order"))
                    elif existing and existing.get("order") is not None:
                        outgoing_item["order"] = existing.get("order")
            except Exception:
                # Best-effort; skip setting order if conversion fails
                pass

            payload = {
                "context": {
                    "executionId": settings.EXECUTION_ID or "",
                    "sprintId": settings.SPRINT_ID or "",
                    "touchpointId": actual_meeting_id,
                    "itemId": settings.ITEM_ID or "",
                },
                "progressItems": [outgoing_item],
            }

            # Update TOUCHPOINT_DATA local store immediately
            try:
                if isinstance(settings.TOUCHPOINT_DATA, dict):
                    prog_list = settings.TOUCHPOINT_DATA.setdefault("progressItems", [])
                    if existing:
                        existing.update(outgoing_item)
                    else:
                        prog_list.append(outgoing_item)
            except Exception as e:
                logger.warning(f"⚠️ Failed to update TOUCHPOINT_DATA on progress send: {e}")

            # Log and emit
            try:
                socketio_sid = self.get_socketio_sid("/")
            except Exception:
                socketio_sid = None

            logger.info("📤 Sending progress_item payload:")
            logger.info(f"  └─ touchpointId: '{actual_meeting_id}'")
            logger.info(f"  └─ itemId: '{settings.ITEM_ID or ''}'")
            logger.info(f"  └─ type: '{event_type}'  id: '{progress_id}'  status: '{status}'  order: '{outgoing_item.get('order')}'")
            logger.info(f"  └─ socketio_sid: {socketio_sid if socketio_sid else 'unknown'}")
            try:
                pretty = json.dumps(payload, indent=2)
                logger.debug(f"📦 progress_item payload JSON:\n{pretty}")
            except Exception:
                logger.debug(f"📦 progress_item payload (raw): {payload}")

            try:
                pretty = json.dumps(payload, indent=2, ensure_ascii=False)
                logger.info(f"📤 OUTGOING progress_item payload (single):\n{pretty}")
            except Exception:
                logger.info(f"📤 OUTGOING progress_item payload (single raw): {payload}")
            await self._emit_with_reconnect('progress_item', json.dumps(payload))
            logger.info("✅ Successfully sent progress_item")
            return True
        except Exception as e:
            logger.error(f"Failed to send progress item update: {e}")
            if payload is not None and await self.ensure_connected():
                try:
                    try:
                        pretty2 = json.dumps(payload, indent=2, ensure_ascii=False)
                        logger.info(f"📤 OUTGOING progress_item payload (retry):\n{pretty2}")
                    except Exception:
                        logger.info(f"📤 OUTGOING progress_item payload (retry raw): {payload}")
                    await self._emit_with_reconnect('progress_item', json.dumps(payload))
                    logger.info("✅ Successfully re-sent progress_item")
                    return True
                except Exception as e2:
                    logger.error(f"Failed to re-send progress item update: {e2}")
            return False

    async def send_time_out_alert(self, agenda_item: str, meeting_id: Optional[str] = None, agenda_id: Optional[str] = None):
        """
        Send time out alert to the client Socket.IO server
        
        Parameters:
        -----------
        agenda_item: str
            The name of the agenda item
        meeting_id: str
            Optional meeting ID (uses settings if not provided)
        agenda_id: str
            Optional agenda ID (uses agenda_item if not provided)
        """
        if not await self.ensure_connected():
            logger.error(f"🚫 SEND FAILED: Not connected to client server. Cannot send time out alert for '{agenda_item}'")
            return False
            
        alert_timestamp = None
        payload = None
        try:
            # Get current epoch timestamp when sending the alert
            alert_timestamp = time.time()
            alert_timestamp_ms = int(alert_timestamp * 1000)
            
            logger.warning(f"🚨 send_time_out_alert called:")
            logger.warning(f"  └─ agenda_item: '{agenda_item}'")
            logger.warning(f"  └─ alert_timestamp (epoch): {alert_timestamp:.3f} seconds")
            logger.warning(f"  └─ alert_timestamp (ms): {alert_timestamp_ms} milliseconds")
            
            # Get the actual agenda _id from the mapping
            actual_agenda_id = agenda_id or settings.AGENDA_TITLE_TO_ID.get(agenda_item, agenda_item)
            actual_meeting_id = meeting_id or settings.MEETING_ID or "unknown"
            
            payload = {
                "touchpointId": actual_meeting_id,
                "id": actual_agenda_id
            }
            
            logger.warning(f"🚨 Sending time_out_alert payload:")
            logger.warning(f"  └─ touchpointId: '{actual_meeting_id}'")
            logger.warning(f"  └─ agenda_id: '{actual_agenda_id}'")
            logger.warning(f"  └─ sent_at_epoch: {alert_timestamp:.3f} seconds ({alert_timestamp_ms} ms)")
            try:
                socketio_sid = self.get_socketio_sid("/")
                logger.warning(f"  └─ socketio_sid: {socketio_sid if socketio_sid else 'unknown'}")
            except Exception:
                logger.warning("  └─ socketio_sid: unknown")
            
            try:
                pretty = json.dumps(payload, indent=2, ensure_ascii=False)
                logger.info(f"📤 OUTGOING time_out_alert payload:\n{pretty}")
            except Exception:
                logger.info(f"📤 OUTGOING time_out_alert payload (raw): {payload}")
            await self._emit_with_reconnect('time_out_alert', json.dumps(payload))
            logger.warning(f"🚨 Successfully sent overtime alert for '{agenda_item}' at epoch {alert_timestamp:.3f}")
            return True
        except Exception as e:
            logger.error(f"Failed to send time out alert: {e}")
            if payload is not None and alert_timestamp is not None and await self.ensure_connected():
                try:
                    await self._emit_with_reconnect('time_out_alert', json.dumps(payload))
                    logger.warning(f"🚨 Successfully re-sent overtime alert for '{agenda_item}' at epoch {alert_timestamp:.3f}")
                    return True
                except Exception as e2:
                    logger.error(f"Failed to re-send time out alert: {e2}")
            return False
