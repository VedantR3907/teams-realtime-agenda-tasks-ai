import base64
import json
import os
import asyncio
import time
import httpx
from typing import Optional
from loguru import logger
from app.config import settings
from app.ProgressAnalyzer.processors import LLMProcessor, ProgressLLMProcessor
from app.utils.meeting.analytics import MeetingAnalytics
from app.ProgressAnalyzer.processors.minutes_processor import MinutesProcessor
# from app.utils.meeting.active_speaker_manager import ActiveSpeakerManager  # DISABLED: Current speaker tracking no longer needed
from app.utils.meeting.llm_coordinator import LLMCoordinator
from app.utils.meeting.participant_manager import ParticipantManager
from app.realtime.client_socketio.socket_connection import cleanup_socketio_client, get_socketio_client
from app.realtime.client_socketio.client_websocket import generate_app_specific_token  # send_active_speakers, - DISABLED: Current speaker tracking no longer needed
from app.utils.token import get_api_token
from app.utils.meeting.agenda_timer_manager import AgendaTimerManager
from app.ProgressAnalyzer.transcript_processor import TranscriptProcessor


def convert_id_to_name(assignee_id: str) -> str:
    """Convert participant ID back to name using the stored mapping"""
    if not assignee_id:
        return assignee_id
    
    # Search through the name-to-id mapping to find the reverse
    for stored_name, stored_id in settings.PARTICIPANT_NAME_TO_ID.items():
        if stored_id == assignee_id:
            return stored_name
    
    # If no match found, log warning and return original ID as fallback
    logger.warning(f"⚠️ No name found for participant ID: '{assignee_id}', using ID as fallback")
    return assignee_id


class WebSocketEventHandler:
    """
    Handles different types of WebSocket events for meeting management.
    
    This class manages:
    - Meeting lifecycle events (start, end)
    - Audio processing and transcription coordination
    - Event routing and validation
    - Resource cleanup and error handling
    - Agenda timing and warnings
    """
    
    def __init__(self, processor: TranscriptProcessor, analytics: MeetingAnalytics,
                 llm_coordinator: LLMCoordinator, participant_manager: ParticipantManager):
        self.processor = processor
        self.analytics = analytics
        self.llm_coordinator = llm_coordinator
        self.participant_manager = participant_manager
        # Initialize the active speaker manager
        # self.active_speaker_manager = ActiveSpeakerManager() # DISABLED: Current speaker tracking no longer needed
        self._speaker_notifier_task = None
        # Initialize agenda timer manager (will be properly set up on meeting start)
        self.agenda_timer_manager: Optional[AgendaTimerManager] = None

    # NOTE: Reset API removed per backend changes
 
    # Organization profiles and agenda data now fetched during service startup

    def _prepare_timer_agenda_list(self, fetched_agenda_list, fallback_agenda_list):
        """
        Prepare agenda list for timer initialization by converting the REST API format
        to the format expected by the timer manager.
        Now calculates individual durations from agendaStartTime/agendaEndTime.
        """
        if fetched_agenda_list:
            # Convert REST API format to timer format
            timer_list = []
            
            for i, item in enumerate(fetched_agenda_list):
                timer_item = {
                    "title": item.get("title", ""),
                    "description": item.get("description", ""),
                    "index": i + 1,  # Use array index + 1 as agenda number
                    "assignee": item.get("owner", "")  # Owner is now a UUID string
                }
                
                # Calculate individual agenda duration from start/end times or timeSlot
                agenda_duration_sec = 300  # Default 5 minutes
                
                agenda_start = item.get("agendaStartTime")
                agenda_end = item.get("agendaEndTime")
                time_slot_minutes = item.get("timeSlot")
                
                if agenda_start and agenda_end:
                    try:
                        from datetime import datetime
                        
                        # Parse ISO datetime strings
                        start_dt = datetime.fromisoformat(agenda_start)
                        end_dt = datetime.fromisoformat(agenda_end)
                        
                        # Calculate duration in seconds
                        duration_seconds = (end_dt - start_dt).total_seconds()
                        agenda_duration_sec = int(duration_seconds)
                        
                        logger.info(f"📅 '{timer_item['title']}': {agenda_duration_sec}s ({agenda_duration_sec/60:.1f} min) [{agenda_start} → {agenda_end}]")
                        
                    except Exception as e:
                        logger.warning(f"⚠️ Failed to parse agenda times for '{timer_item['title']}': {e}")
                        logger.info(f"📅 '{timer_item['title']}': Using default {agenda_duration_sec}s")
                elif time_slot_minutes:
                    # Use timeSlot field (in minutes) from API
                    try:
                        agenda_duration_sec = int(time_slot_minutes) * 60
                        logger.info(f"📅 '{timer_item['title']}': {agenda_duration_sec}s ({agenda_duration_sec/60:.1f} min) [from timeSlot: {time_slot_minutes} min]")
                    except (ValueError, TypeError):
                        logger.warning(f"⚠️ Invalid timeSlot for '{timer_item['title']}': {time_slot_minutes}")
                        logger.info(f"📅 '{timer_item['title']}': Using default {agenda_duration_sec}s")
                else:
                    logger.info(f"📅 '{timer_item['title']}': No start/end times or timeSlot, using default {agenda_duration_sec}s")
                
                # Set the calculated duration
                timer_item["time"] = agenda_duration_sec
                timer_list.append(timer_item)
                
            total_duration_sec = sum(item["time"] for item in timer_list)
            logger.info(f"🔄 Converted {len(timer_list)} agenda items with individual timings (Total: {total_duration_sec}s = {total_duration_sec/60:.1f} min)")
            return timer_list
        else:
            # Use fallback agenda list (from WebSocket meeting_details)
            logger.info(f"🔄 Using fallback agenda list for timer ({len(fallback_agenda_list)} items)")
            return fallback_agenda_list

    async def handle_meeting_details(self, data):
        """Handle meeting details received from .NET backend - only extracts meetingId"""
        # Extract and store meeting ID (only field we receive from .NET)
        meeting_id = data.get("meetingId")
        if meeting_id:
            # settings.MEETING_ID = meeting_id
            # logger.info(f"📋 Updated meeting ID from payload: {settings.MEETING_ID}")
            pass
        else:
            logger.warning("⚠️ No meetingId found in meeting_details payload")
        
        # Fetch agenda data and prepare for initialization
        try:
            # Organization participants should already be fetched during service startup
            organization_participants = settings.ORGANIZATION_PARTICIPANTS or []
            if not organization_participants:
                logger.warning("⚠️ No organization participants preloaded; ensure /service/started ran organization fetch")

            # Touchpoint is now fetched in /service/started; skip here
            
            # Initialize agenda timer manager (no more reference time needed)
            self.agenda_timer_manager = AgendaTimerManager()
            
            # Set up overtime callback for automatic alerts
            async def overtime_alert_callback(agenda_title: str):
                """Callback to send overtime alerts automatically"""
                try:
                    from app.realtime.client_socketio.socket_actions import send_time_out_alert
                    await send_time_out_alert(agenda_title)
                    logger.error(f"🚨 Automatic overtime alert sent for: {agenda_title}")
                except Exception as e:
                    logger.error(f"❌ Failed to send automatic overtime alert: {e}")
            
            self.agenda_timer_manager.set_overtime_callback(overtime_alert_callback)
            
            # Prepare timer agenda list with equal time division
            timer_agenda_list = self._prepare_timer_agenda_list(settings.AGENDA_ITEMS_FULL, [])
            
            # Initialize timers
            self.agenda_timer_manager.initialize_agenda_timers(timer_agenda_list)
            
            # Start background monitoring for automatic overtime alerts
            await self.agenda_timer_manager.start_background_monitoring(check_interval=1.0)
            
            # Initialize LLM processors
            if settings.AGENDA_ITEMS:
                llm_processor = LLMProcessor(agenda_items=settings.AGENDA_ITEMS)
            else:
                llm_processor = LLMProcessor(agenda_list=[])
            
            # Map API status values to internal LLM processor format
            api_to_internal_state = {
                "in_discussion": "In Progress",
                "discussed": "Completed",
                "not_started": "Not Started",
                "on_hold": "On Hold",
            }
            
            # Build current agenda status from settings (fetched from API)
            current_agenda_status = {}
            active_agenda_from_api = None
            for title in settings.AGENDA_ITEMS.keys():
                api_status = settings.AGENDA_TITLE_TO_STATE.get(title, "not_started")
                internal_status = api_to_internal_state.get(api_status, "Not Started")
                current_agenda_status[title] = internal_status
                # Track which agenda is currently "In Progress" to sync with timer manager
                if internal_status == "In Progress":
                    active_agenda_from_api = title
            
            # Sync timer manager's current active agenda with API state
            if active_agenda_from_api and self.agenda_timer_manager:
                self.agenda_timer_manager.current_active_agenda = active_agenda_from_api
                logger.info(f"🎯 Synced timer manager active agenda from API: '{active_agenda_from_api}'")
            
            # Sync LLM processor states with API-fetched states
            if current_agenda_status:
                llm_processor.agenda_states = current_agenda_status.copy()
                # Rebuild system prompt with correct states
                from app.ProgressAnalyzer.prompts.agenda_prompt import get_agenda_tracking_prompt
                llm_processor.system_prompt = get_agenda_tracking_prompt(
                    llm_processor.agenda_titles,
                    llm_processor.agenda_descriptions,
                    llm_processor.agenda_states
                )
            
            logger.info(f"🔄 Initializing ProgressLLMProcessor with {len(settings.PREVIOUS_PROGRESS_ITEMS)} previous meeting items")
            logger.info(f"🎯 Current agenda status: {current_agenda_status}")
            progress_processor = ProgressLLMProcessor(
                agendas=list(settings.AGENDA_ITEMS.keys()),
                previous_meeting_items=settings.PREVIOUS_PROGRESS_ITEMS,
                meeting_participants=organization_participants,
                agenda_status=current_agenda_status
            )

            # Initialize LLM coordinator with both processors, timer manager, and participants
            self.llm_coordinator.initialize(
                llm_processor,
                self.processor,
                self.analytics,
                self.agenda_timer_manager,
                progress_processor=progress_processor,
                meeting_participants=organization_participants,
            )
            
            # Reset incremental transcript tracking for new meeting
            self.llm_coordinator.reset_for_new_meeting()
            
            # Initialize analytics with meeting metadata
            self.analytics.initialize_meeting(
                meeting_id=settings.MEETING_ID,
                agenda_items=settings.AGENDA_ITEMS
            )

            # Ensure Socket.IO client is connected for this meeting
            try:
                client = await get_socketio_client()
                if client.is_connected():
                    logger.info("✅ Socket.IO client connected for meeting after initialization")
                else:
                    logger.warning("⚠️ Socket.IO client not connected after initialization")
            except Exception as e:
                logger.error(f"❌ Failed to ensure Socket.IO connection after meeting init: {e}")
            
            # Note: Participants are now tracked via audio events, not meeting_details

            # Set the callback function for the speaker manager
            # self.active_speaker_manager.set_callback(send_active_speakers) # DISABLED: Current speaker tracking no longer needed

            # Start the background task to notify about active speakers
            if self._speaker_notifier_task:
                self._speaker_notifier_task.cancel()
            # self._speaker_notifier_task = asyncio.create_task(self.active_speaker_manager.run_notifier()) # DISABLED: Current speaker tracking no longer needed
            logger.info("Active speaker tracking disabled - no notifier task started.")
            
            logger.info(f"✅ Meeting details processed successfully with all components initialized")
            
        except Exception as e:
            logger.error(f"❌ Error processing meeting details: {e}")

    async def handle_audio(self, data: dict):
        """Handle audio event"""
        # Process each chunk to extract participant info
        for chunk in data.get("chunks", []):
            email = chunk.get("email")
            display_name = chunk.get("displayName")
            
            if not email:
                logger.warning("No email found in audio chunk, skipping")
                continue
            
            # Update the active speaker manager with the latest speaker info
            if display_name:
                # await self.active_speaker_manager.update_speaker(email, display_name) # DISABLED: Current speaker tracking no longer needed
                pass
            
            # Update participant info if we have display name
            if display_name:
                self.participant_manager.update_participant_name(email, display_name)
                # Ensure analytics tracks the participant even without a join event
                self.analytics.add_participant(email, display_name)
                logger.debug(f"Updated participant from chunk: {display_name} (Email: {email})")
            
            # Create callback function for this participant's transcriber
            def create_transcript_callback(participant_email: str):
                def _on_transcript_for_meeting(text: str, is_final: bool, start: float, end: float):
                    if is_final:
                        # Use display name if available, otherwise fall back to email
                        display_name = self.participant_manager.get_participant_display_name(participant_email)
                        self.processor.add_utterance(display_name, start, end, text)
                        
                        # Track utterance in analytics
                        self.analytics.update_participant_utterance(participant_email)
                        
                        # (re)start our debounce timer for LLM invocation
                        # Only schedule if LLM coordinator is properly initialized
                        if (self.llm_coordinator.llm_processor and 
                            self.llm_coordinator.transcript_processor and 
                            self.llm_coordinator.analytics):
                            self.llm_coordinator.schedule_llm_call()
                        else:
                            logger.debug("LLM coordinator not yet initialized, skipping transcript scheduling")
                
                return _on_transcript_for_meeting
            
            # Get or create transcriber for this participant
            transcriber = self.participant_manager.get_or_create_transcriber(
                email, 
                create_transcript_callback(email)
            )
            
            if not transcriber:
                logger.error(f"Failed to get transcriber for {email}")
                continue
            
            # Extract speaker timing information (in seconds)
            speak_start = 0.0
            speak_end = 0.0
            
            if "speakStartTime" in chunk:
                try:
                    speak_start = float(chunk["speakStartTime"]) / 1000  # Convert ms to seconds
                except (ValueError, TypeError):
                    logger.warning(f"Invalid speakStartTime in chunk: {chunk.get('speakStartTime')}")
                    speak_start = 0.0
            
            if "speakEndTime" in chunk:
                try:
                    speak_end = float(chunk["speakEndTime"]) / 1000  # Convert ms to seconds
                except (ValueError, TypeError):
                    logger.warning(f"Invalid speakEndTime in chunk: {chunk.get('speakEndTime')}")
                    speak_end = 0.0
                    
            # Log speaker timing information
            if speak_start > 0 or speak_end > 0:
                pass
            
            # Send audio data to transcriber
            pcm = base64.b64decode(chunk["buffer"])
            self.participant_manager.send_audio_to_participant(email, pcm, speak_start, speak_end)

    async def handle_meeting_ended(self, data: dict):
        """Handle meeting_ended event"""
        
        # CRITICAL: Flush all pending operations before cleanup to prevent data loss
        logger.info("🔄 Flushing pending operations before meeting cleanup...")
        try:
            await self.llm_coordinator.flush_pending_operations(timeout=5.0)
        except Exception as e:
            logger.error(f"❌ Error during flush operations: {e}")
        
        # Immediately stop all Deepgram transcribers to close live sessions and threads
        try:
            self.participant_manager.stop_all_transcribers()
        except Exception as e:
            logger.error(f"❌ Error stopping transcribers on meeting end: {e}")

        # After flushing, generate meeting minutes and then refetch the touchpoint and send filtered tasks to external API
        try:
            # Consolidate per-speaker transcripts into a single meeting-level JSON for MinutesProcessor
            try:
                ordered_lines = self.processor.get_ordered_transcript()
                transcripts_json = []
                for line in ordered_lines:
                    try:
                        # Expect format: [HH:MM:SS] [Speaker] Text
                        ts_part = line.split("]")[0].replace("[", "")
                        h, m, s = [int(x) for x in ts_part.split(":", 2)]
                        ts_seconds = h * 3600 + m * 60 + s
                    except Exception:
                        ts_seconds = 0
                    transcripts_json.append({
                        "timestamp": ts_seconds,
                        "formatted_line": line
                    })

                # Determine output path consistent with MinutesProcessor default
                try:
                    output_dir = os.path.dirname(settings.TRANSCRIPT_FILE_PATH) or "output"
                    os.makedirs(output_dir, exist_ok=True)
                    meeting_json_path = os.path.join(output_dir, "meeting_transcript.json")
                    payload = {
                        "meeting_info": {
                            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                        },
                        "transcripts": transcripts_json
                    }
                    with open(meeting_json_path, "w", encoding="utf-8") as f:
                        json.dump(payload, f, indent=2, ensure_ascii=False)
                    logger.info(f"📄 Consolidated meeting transcript written: {meeting_json_path} ({len(transcripts_json)} lines)")
                except Exception as e:
                    logger.error(f"Failed writing consolidated meeting transcript: {e}")
            except Exception as e:
                logger.error(f"Failed consolidating transcripts for minutes: {e}")

            # Build dynamic API URL using IDs from settings
            if not (settings.EXECUTION_ID and settings.SPRINT_ID and settings.ITEM_ID and settings.TOUCHPOINT_ID):
                logger.warning("⚠️ Missing one or more IDs; skipping send-tasks-assigned call")
            else:
                url = (
                    f"{settings.SOCKET_URL}/api/"
                    f"organization/{settings.ORGANIZATION_ID}/"
                    f"execution/{settings.EXECUTION_ID}/"
                    f"sprint/{settings.SPRINT_ID}/"
                    f"item/{settings.ITEM_ID}/"
                    f"touchpoint/{settings.TOUCHPOINT_ID}/query_touchpoint"
                )

                bearer_token = get_api_token()

                touchpoint_payload = None
                try:
                    async with httpx.AsyncClient(timeout=10.0, verify=False) as client:
                        logger.info(f"🔍 Refetching touchpoint on meeting end: {url}")
                        resp = await client.get(
                            url,
                            headers={
                                "Authorization": f"Bearer {bearer_token}",
                                "python-server": "true",
                            },
                        )
                        resp.raise_for_status()
                        touchpoint_payload = resp.json()
                except Exception as e:
                    logger.error(f"❌ Failed to refetch touchpoint on meeting end: {e}")

                if isinstance(touchpoint_payload, dict):
                    # -------- Prepare data for minutes (current meeting only) --------
                    try:
                        agendas_from_api = []
                        try:
                            # Extract agenda titles from API payload
                            agendas_from_api = [a.get("title") for a in touchpoint_payload.get("agenda", []) if isinstance(a, dict) and a.get("title")]
                        except Exception:
                            agendas_from_api = []

                        top_meeting_date = touchpoint_payload.get("meetingDate")
                        all_items = touchpoint_payload.get("progressItems", []) or []

                        # Filter progress items for minutes:
                        #  - resolved == True
                        #  - meetingDate == current meetingDate
                        minutes_progress_items = []
                        for item in all_items:
                            try:
                                if (
                                    item.get("resolved", False) is True
                                    and (not top_meeting_date or item.get("meetingDate") == top_meeting_date)
                                ):
                                    minutes_progress_items.append(item)
                            except Exception:
                                continue

                        # Generate minutes using API-provided agendas and filtered progress items
                        try:
                            minutes_processor = MinutesProcessor()
                            minutes_data = await asyncio.to_thread(
                                minutes_processor.generate_meeting_minutes,
                                None,
                                agendas_from_api,
                                minutes_progress_items
                            )
                            if minutes_data.get("success"):
                                await asyncio.to_thread(
                                    minutes_processor.save_meeting_minutes, minutes_data
                                )
                            else:
                                logger.warning(f"Minutes generation returned unsuccessfully: {minutes_data.get('error')}")
                        except Exception as e:
                            logger.error(f"Failed to generate meeting minutes: {e}")
                    except Exception as e:
                        logger.error(f"Failed preparing minutes data: {e}")

                    # Build payload for external API from the refetched touchpoint
                    top_meeting_date = touchpoint_payload.get("meetingDate")
                    all_items = touchpoint_payload.get("progressItems", []) or []

                    # Filter unresolved and same-day items for external payload
                    # Only include status approved (default approved if missing)
                    filtered_items = []
                    for item in all_items:
                        try:
                            # Only include unresolved items for the prompt
                            if item.get("resolved", False) is True:
                                continue
                            if top_meeting_date and item.get("meetingDate") != top_meeting_date:
                                continue
                            status_val = (item.get("status") or "").strip().lower()
                            if status_val in ("rejected", "pending"):
                                continue
                            # Treat missing/empty status as approved
                            filtered_items.append(item)
                        except Exception:
                            continue

                    external_payload = {
                        # Direct fields from the first API
                        "itemTitle": touchpoint_payload.get("itemTitle"),
                        "itemDescription": touchpoint_payload.get("itemDescription"),
                        "minutes": touchpoint_payload.get("minutes", []),
                        "id": touchpoint_payload.get("id"),
                        "title": touchpoint_payload.get("title"),
                        "description": touchpoint_payload.get("description"),
                        "status": touchpoint_payload.get("status"),
                        "touchpointType": touchpoint_payload.get("touchpointType"),
                        "startTime": touchpoint_payload.get("startTime"),
                        "endTime": touchpoint_payload.get("endTime"),
                        "meetingDate": touchpoint_payload.get("meetingDate"),
                        "meetingLink": touchpoint_payload.get("meetingLink"),
                        "meetingMinutes": touchpoint_payload.get("meetingMinutes", []),
                        "owner": touchpoint_payload.get("owner"),
                        "team": touchpoint_payload.get("team", []),
                        # Filtered progress items for this meeting date
                        "progressItems": filtered_items,
                    }

                    # Transform assignee ensuring object shape {id,email,userName}
                    transformed_items = []
                    for item in filtered_items:
                        try:
                            new_item = dict(item)
                            a = new_item.get("assignee")
                            if isinstance(a, dict):
                                assignee_obj = {
                                    "id": a.get("id") or None,
                                    "email": a.get("email") or None,
                                    "userName": a.get("userName") or None,
                                }
                            elif isinstance(a, str):
                                assignee_obj = {"id": a, "email": None, "userName": None}
                            else:
                                assignee_obj = {"id": None, "email": None, "userName": None}
                            new_item["assignee"] = assignee_obj
                            transformed_items.append(new_item)
                        except Exception:
                            transformed_items.append(item)

                    # Send to external API
                    external_url = settings.ADAPTIVE_CARD_SERVER_URL
                    try:
                        async with httpx.AsyncClient(timeout=10.0, verify=False) as client:
                            logger.info(
                                f"📤 Posting {len(transformed_items)} filtered progress items to send-tasks-assigned"
                            )
                            adaptive_token = get_api_token()
                            post_resp = await client.post(
                                external_url,
                                json={**external_payload, "progressItems": transformed_items},
                                headers={
                                    "Authorization": f"Bearer {adaptive_token}",
                                    "Content-Type": "application/json",
                                },
                            )
                            if post_resp.status_code >= 400:
                                logger.error(
                                    f"❌ send-tasks-assigned returned {post_resp.status_code}: {post_resp.text[:500]}"
                                )
                            else:
                                logger.info("✅ send-tasks-assigned call successful")
                    except Exception as e:
                        logger.error(f"❌ Failed calling send-tasks-assigned API: {e}")
                else:
                    logger.warning("⚠️ No valid touchpoint payload; skipping external tasks send")
        except Exception as e:
            logger.error(f"❌ Unexpected error during tasks send workflow: {e}")

        # Cancel any remaining pending LLM calls (should be none after flush)
        self.llm_coordinator.cancel_pending_calls()
        
        # Cleanup agenda timer manager
        if self.agenda_timer_manager:
            await self.agenda_timer_manager.cleanup()
            self.agenda_timer_manager = None
        
        # Finalize analytics
        self.analytics.finalize_meeting()
        
        # Stop the speaker notifier task and clear the final state
        if self._speaker_notifier_task:
            self._speaker_notifier_task.cancel()
            self._speaker_notifier_task = None
            logger.info("Cancelled active speaker notifier task.")
        # await self.active_speaker_manager.clear() # DISABLED: Current speaker tracking no longer needed
        
        # Cleanup Socket.IO client connection
        await cleanup_socketio_client()
        
        # Clear the transcript processor for the next meeting
        self.processor.clear()
        logger.info("Meeting ended - transcript processor cleared")

    async def handle_ping(self, websocket):
        """Handle ping event"""
        await websocket.send_json({"type": "pong"})

    async def handle_event(self, event_type: str, data: dict, websocket=None):
        """Route events to appropriate handlers"""
        try:
            if event_type == "meeting_details":
                await self.handle_meeting_details(data)
            elif event_type == "audio":
                await self.handle_audio(data)
            elif event_type == "meeting_ended":
                await self.handle_meeting_ended(data)
            elif event_type == "ping":
                await self.handle_ping(websocket)
            else:
                logger.warning(f"Unknown event type: {event_type}")
        except Exception as e:
            logger.error(f"Error handling event {event_type}: {e}")
            logger.error(f"Event data type: {type(data)}, Event data: {data}")
            # Don't re-raise to keep the WebSocket connection alive

    def cleanup(self):
        """Cleanup resources when WebSocket connection closes"""
        logger.info("Cleaning up WebSocket event handler resources")
        self.participant_manager.stop_all_transcribers()
        self.llm_coordinator.cancel_pending_calls()

        # Ensure the background task is stopped on cleanup
        if self._speaker_notifier_task:
            self._speaker_notifier_task.cancel()
            self._speaker_notifier_task = None

    def get_status(self) -> dict:
        """Get comprehensive status of all components"""
        return {
            "participant_manager": {
                "participant_count": self.participant_manager.get_participant_count(),
                "transcriber_status": self.participant_manager.get_transcriber_status()
            },
            "llm_coordinator": self.llm_coordinator.get_status(),
            "analytics": {
                "total_llm_calls": self.analytics.get_analytics()["total_llm_calls"],
                "total_participants": len(self.analytics.get_analytics()["participants"])
            }
        }

    async def handle_agenda_status_update(self, data):
        """Handle agenda status updates and manage timers"""
        try:
            agenda_id = data.get('id')
            status = data.get('status')
            
            if not agenda_id or not status:
                logger.error(f"❌ Missing id or status in agenda update: {data}")
                return
            
            # Get the title from agenda_id using settings data
            title = settings.AGENDA_ID_TO_TITLE.get(agenda_id, agenda_id)
            
            logger.info(f"📋 Agenda status update: {title} → {status}")
            
            # Update timer based on status (no timestamp provided, so use current time)
            if self.agenda_timer_manager and title:
                if status == "in_discussion":
                    self.agenda_timer_manager.start_agenda_timer(title)
                else:
                    self.agenda_timer_manager.pause_agenda_timer(title)
            
        except Exception as e:
            logger.error(f"❌ Error handling agenda status update: {e}")

    async def handle_manual_agenda_update(self, agenda_id: str, new_status: str):
        """Handle manual agenda status updates and manage timers"""
        try:
            # Get the title from agenda_id using settings data
            title = settings.AGENDA_ID_TO_TITLE.get(agenda_id, agenda_id)
            
            logger.info(f"🖱️ Manual agenda update: {title} → {new_status}")
            
            # Update timer based on status
            if self.agenda_timer_manager and title:
                if new_status == "in_discussion":
                    self.agenda_timer_manager.start_agenda_timer(title)  # Start from zero (current time)
                else:
                    self.agenda_timer_manager.pause_agenda_timer(title)  # Pause at current time
        
        except Exception as e:
            logger.error(f"❌ Error handling manual agenda update: {e}")


