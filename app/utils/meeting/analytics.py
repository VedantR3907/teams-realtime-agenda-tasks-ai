import asyncio
import json
import os
import time
from datetime import datetime
from loguru import logger
from typing import Dict, Any, List, Optional, TYPE_CHECKING
from dataclasses import dataclass
from app.config import settings

if TYPE_CHECKING:
    from .agenda_timer_manager import AgendaTimerManager


@dataclass
class StateChange:
    agenda_item: str
    from_state: str
    to_state: str
    relative_time: str
    absolute_time: str


class MeetingAnalytics:
    """
    Handles all meeting analytics tracking and reporting.
    
    This class manages:
    - Meeting metadata and timing
    - Participant tracking and statistics
    - Agenda item state changes
    - LLM call history and performance
    - Analytics file generation
    - Socket.IO agenda updates
    - Agenda timer coordination
    """
    
    def __init__(self):
        self.analytics = {
            "meeting_id": None,
            "total_llm_calls": 0,
            "participants": {},
            "agenda_items": {},
            "state_changes": [],
            "llm_call_history": [],
            "session_start": datetime.now().strftime("%Y-%m-%d %I:%M:%S %p")
        }
        self.timer_manager: Optional['AgendaTimerManager'] = None

    def set_timer_manager(self, timer_manager: 'AgendaTimerManager'):
        """Set the agenda timer manager for state change integration"""
        self.timer_manager = timer_manager
        logger.info("🕒 Timer manager integration enabled for analytics")

    def _find_matching_agenda_title(self, agenda_item: str) -> Optional[str]:
        """Find the best matching agenda title for timer operations"""
        if not self.timer_manager:
            return None
            
        available_timers = list(self.timer_manager.agenda_timers.keys())
        
        # Direct match
        if agenda_item in available_timers:
            return agenda_item
            
        # Check if it's an agenda ID that needs to be converted to title
        if agenda_item in settings.AGENDA_ID_TO_TITLE:
            title = settings.AGENDA_ID_TO_TITLE[agenda_item]
            if title in available_timers:
                return title
                
        # Check for partial matches (case-insensitive)
        agenda_lower = agenda_item.lower()
        for timer_title in available_timers:
            if agenda_lower in timer_title.lower() or timer_title.lower() in agenda_lower:
                logger.info(f"🔗 Fuzzy agenda match: '{agenda_item}' → '{timer_title}'")
                return timer_title
                
        # No match found
        logger.debug(f"❌ No matching timer found for '{agenda_item}' in {available_timers}")
        return None

    def initialize_meeting(self, meeting_id: str, agenda_items: Dict[str, str]):
        """Initialize analytics for a new meeting"""
        self.analytics.update({
            "meeting_id": meeting_id,
            "agenda_items": {title: {
                "description": desc,
                "current_state": "Not Started",
                "state_history": [],
                "first_mentioned": None,
                "last_updated": None
            } for title, desc in agenda_items.items()}
        })
        self._write_file_safe()
        
        # Send initial agenda sync to Socket.IO server
        self._send_agenda_sync()

    def add_participant(self, email: str, display_name: str):
        """Add or update participant information"""
        join_time = time.time()
        self.analytics["participants"][email] = {
            "display_name": display_name,
            "joined_at": datetime.fromtimestamp(join_time).strftime("%I:%M:%S %p"),
            "utterance_count": 0
        }

    def update_participant_utterance(self, email: str):
        """Update participant's utterance count and last spoke time"""
        if email in self.analytics["participants"]:
            now = time.time()
            self.analytics["participants"][email]["utterance_count"] += 1
            self.analytics["participants"][email]["last_spoke"] = datetime.fromtimestamp(now).strftime("%I:%M:%S %p")

    def update_agenda_state(self, agenda_item: str, new_state: str, 
                           timestamp: float):
        """Update agenda item state and track the change"""
        # Initialize agenda item if not exists
        if agenda_item not in self.analytics["agenda_items"]:
            self.analytics["agenda_items"][agenda_item] = {
                "current_state": "Not Started",
                "state_history": [],
                "first_mentioned": timestamp,
                "last_updated": timestamp
            }
        
        item_data = self.analytics["agenda_items"][agenda_item]
        old_state = item_data["current_state"]
        
        # Compute relative and absolute datetimes
        current_time = time.time()
        dt_abs = datetime.fromtimestamp(current_time)
        formatted_abs = dt_abs.strftime("%I:%M:%S %p")
        rel_td = time.gmtime(timestamp)
        formatted_rel = time.strftime("%H:%M:%S", rel_td)

        state_change = StateChange(
            agenda_item=agenda_item,
            from_state=old_state,
            to_state=new_state,
            relative_time=formatted_rel,
            absolute_time=formatted_abs
        )
        
        # Update current state
        item_data["current_state"] = new_state
        item_data["last_updated"] = timestamp
        item_data["state_history"].append({
            "state": new_state,
            "timestamp": timestamp,
            "from_state": old_state
        })
        
        # Add to global state changes
        self.analytics["state_changes"].append(state_change.__dict__)
        
        # Handle timer state changes
        if self.timer_manager:
            try:
                # Find the matching agenda title for timer operations
                timer_title = self._find_matching_agenda_title(agenda_item)
                if timer_title is None:
                    logger.debug(f"⏭️ Skipping timer action for '{agenda_item}' - no matching timer found")
                else:
                    if new_state == "In Progress":
                        # Start timer for this agenda from zero (current time)
                        logger.info(f"🚀 Starting timer for agenda '{timer_title}' - state change to 'In Progress'")
                        self.timer_manager.start_agenda_timer(timer_title)  # No timestamp = start from now
                        
                        # Get the actual timer start timestamp from the timer session
                        timer = self.timer_manager.agenda_timers.get(timer_title)
                        if timer and timer.current_session:
                            actual_start_timestamp = timer.current_session.start_wall
                            timestamp = actual_start_timestamp  # Use actual timer start time for agenda status update
                            logger.info(f"⏰ Timer started for '{timer_title}' @ {actual_start_timestamp:.3f} (epoch seconds)")
                            logger.info(f"📤 Will send agenda status update with timestamp: {actual_start_timestamp:.3f}")
                        else:
                            logger.warning(f"⚠️ Could not get actual timer start timestamp for '{timer_title}'")
                        
                    elif new_state == "On Hold":
                        # Pause timer for this agenda (can be resumed later)
                        self.timer_manager.pause_agenda_timer(timer_title)  # No timestamp = use current time
                    elif new_state == "Completed":
                        # Complete timer for this agenda (no more timing needed)
                        # We still call pause to finalize the timing, but it's really completion
                        if timer_title == self.timer_manager.current_active_agenda:
                            self.timer_manager.pause_agenda_timer(timer_title)  # No timestamp = use current time
                            logger.info(f"✅ Completed timer for '{timer_title}' (total: {self.timer_manager.agenda_timers[timer_title].total_time_spent:.1f}s)")
                    # Note: "Not Started" doesn't require timer action
                    
                    logger.debug(f"🕒 Timer action for '{agenda_item}' → '{timer_title}': {old_state} → {new_state}")
            except Exception as e:
                logger.error(f"❌ Timer manager error for '{agenda_item}': {e}")
        
        # Send agenda status update to Socket.IO server (uses actual timer timestamp for "In Progress")
        self._send_agenda_status_update(agenda_item, new_state, timestamp)
        
        # Write to file after each update
        self._write_file_safe()

    def update_agenda_state_manual(self, agenda_item: str, new_state: str, timestamp: float):
        """
        Update agenda item state from manual Teams frontend changes without re-emitting.
        This prevents infinite loops when receiving manual updates from the client.
        
        Parameters:
        -----------
        agenda_item: str
            The name or ID of the agenda item
        new_state: str  
            The new state (hold, in_discussion, discussed, remaining)
        timestamp: float
            Timestamp in seconds since meeting start
        """
        # Map client states back to internal states
        state_mapping = {
            "remaining": "Not Started",
            "in_discussion": "In Progress",
            "hold": "On Hold", 
            "discussed": "Completed"
        }
        
        internal_state = state_mapping.get(new_state, new_state)
        
        # Check if this is an agenda_id or title
        agenda_title = agenda_item
        if agenda_item in settings.AGENDA_TITLE_TO_ID.values():
            # This is an agenda_id, find the corresponding title
            for title, aid in settings.AGENDA_TITLE_TO_ID.items():
                if aid == agenda_item:
                    agenda_title = title
                    break
        
        # Initialize agenda item if not exists
        if agenda_title not in self.analytics["agenda_items"]:
            self.analytics["agenda_items"][agenda_title] = {
                "description": "",
                "current_state": "Not Started",
                "state_history": [],
                "first_mentioned": timestamp,
                "last_updated": timestamp
            }
        
        item_data = self.analytics["agenda_items"][agenda_title]
        old_state = item_data["current_state"]
        
        # Only update if state actually changed
        if old_state == internal_state:
            logger.debug(f"📋 Manual update ignored - state unchanged: {agenda_title} = {internal_state}")
            return
        
        # Update current state
        item_data["current_state"] = internal_state
        item_data["last_updated"] = timestamp
        item_data["state_history"].append({
            "state": internal_state,
            "timestamp": timestamp,
            "from_state": old_state,
            "source": "manual"  # Mark as manual update
        })
        
        # Handle timer state changes without emitting socket events
        if self.timer_manager:
            try:
                # Find the matching agenda title for timer operations
                timer_title = self._find_matching_agenda_title(agenda_title)
                if timer_title is None:
                    logger.debug(f"⏭️ Skipping manual timer action for '{agenda_title}' - no matching timer found")
                else:
                    if internal_state == "In Progress":
                        # Start timer for this agenda
                        self.timer_manager.start_agenda_timer(timer_title)
                        
                        # Get the actual timer start timestamp from the timer session for manual updates
                        timer = self.timer_manager.agenda_timers.get(timer_title)
                        if timer and timer.current_session:
                            actual_start_timestamp = timer.current_session.start_wall
                            # Update the analytics timestamp to match actual timer start
                            item_data["last_updated"] = actual_start_timestamp
                            # Update the latest state history entry timestamp too
                            if item_data["state_history"]:
                                item_data["state_history"][-1]["timestamp"] = actual_start_timestamp
                            logger.debug(f"🕒 Updated manual timer timestamp to actual start time {actual_start_timestamp:.3f}")
                        
                    elif internal_state == "On Hold":
                        # Pause timer for this agenda (can be resumed later)
                        self.timer_manager.pause_agenda_timer(timer_title)
                    elif internal_state == "Completed":
                        # NEW: finish timer & alert
                        self.timer_manager.complete_agenda_timer(timer_title)  # No timestamp = use current time
                        logger.info(
                            f"✅ Completed timer for '{timer_title}' via manual update "
                            f"(total: {self.timer_manager.agenda_timers[timer_title].total_time_spent:.1f}s)"
                        )

                        # one-off alert (non-blocking)
                        try:

                            from app.realtime.client_socketio.socket_actions import send_time_out_alert
                            from app.realtime.client_socketio.socket_connection import get_socketio_loop

                            try:
                                loop = asyncio.get_running_loop()
                                loop.create_task(send_time_out_alert(timer_title))
                            except RuntimeError:
                                client_loop = get_socketio_loop()
                                if client_loop and not client_loop.is_closed():
                                    asyncio.run_coroutine_threadsafe(
                                        send_time_out_alert(timer_title), client_loop
                                    )
                                else:
                                    asyncio.run(send_time_out_alert(timer_title))
                        except Exception as e:
                            logger.error(f"❌ Failed to send timeout alert for '{timer_title}': {e}")
                    # Note: "Not Started" doesn't require timer action
                    
                    logger.info(f"🕒 Manual timer action for '{agenda_title}' → '{timer_title}': {old_state} → {internal_state}")
            except Exception as e:
                logger.error(f"❌ Timer manager error during manual update for '{agenda_title}': {e}")
        
        # Write to file
        self._write_file_safe()
        
        # NOTE: Do not send confirmation back to server for manual updates
        # The server already knows about this change (it sent it to us)
        # Sending confirmation would create unnecessary round-trips
        
        logger.info(f"📋 Manual agenda update applied: {agenda_title} ({old_state} → {internal_state}) @ {timestamp:.1f}s")

    def _send_agenda_sync(self):
        """Send agenda sync to Socket.IO server"""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return

        if self.analytics["agenda_items"]:
            agenda_items_with_timers = {}
            for title, item_data in self.analytics["agenda_items"].items():
                agenda_items_with_timers[title] = item_data.copy()

                if self.timer_manager:
                    try:
                        timer_status = self.timer_manager.get_timer_status(title)
                        if timer_status:
                            agenda_items_with_timers[title]["timer"] = {
                                "allocated_time": timer_status["allocated_time"],
                                "allocated_minutes": timer_status["allocated_minutes"],
                                "remaining_time": timer_status["remaining_time"],
                                "remaining_minutes": timer_status["remaining_minutes"],
                                "time_spent": timer_status["time_spent"],
                                "time_spent_minutes": timer_status["time_spent_minutes"],
                                "percentage_used": timer_status["percentage_used"],
                                "is_overtime": timer_status["is_overtime"]
                            }
                    except Exception as e:
                        logger.warning(f"Failed to get timer status for '{title}': {e}")

            logger.debug("Agenda sync skipped - now handled via REST API")
        else:
            logger.debug("Agenda sync skipped - no agenda items")

    def _send_agenda_status_update(self, agenda_item: str, new_state: str, timestamp: float):
        """Send agenda state update to Socket.IO server"""
        try:
            import asyncio
            from app.realtime.client_socketio.socket_actions import send_agenda_status_update
            
            # Get description if available
            description = ""
            if agenda_item in self.analytics["agenda_items"]:
                description = self.analytics["agenda_items"][agenda_item].get("description", "")
            
            # Get timer information if available
            timer_info = {}
            if self.timer_manager:
                try:
                    timer_status = self.timer_manager.get_timer_status(agenda_item)
                    if timer_status:
                        timer_info = {
                            "allocated_time": timer_status["allocated_time"],
                            "allocated_minutes": timer_status["allocated_minutes"],
                            "remaining_time": timer_status["remaining_time"],
                            "remaining_minutes": timer_status["remaining_minutes"],
                            "time_spent": timer_status["time_spent"],
                            "time_spent_minutes": timer_status["time_spent_minutes"],
                            "percentage_used": timer_status["percentage_used"],
                            "is_overtime": timer_status["is_overtime"]
                        }
                except Exception as e:
                    logger.warning(f"Failed to get timer status for '{agenda_item}': {e}")
            
            # Ensure we pass the timestamp correctly
            # The timestamp should be in seconds, and send_agenda_status_update will convert to ms
            actual_timestamp = timestamp
            
            try:
                loop = asyncio.get_running_loop()
                
                # Create task and handle potential send failures
                async def send_with_error_handling():
                    try:
                        success = await send_agenda_status_update(agenda_item, new_state, actual_timestamp)
                        if success:
                            logger.info(f"🚀 SENT: Agenda update '{agenda_item}' → {new_state} @ {actual_timestamp:.1f}s")
                        else:
                            logger.error(f"🚫 FAILED: Could not send agenda update '{agenda_item}' → {new_state} (disconnected)")
                    except Exception as e:
                        logger.error(f"🚫 ERROR: Failed to send agenda update '{agenda_item}' → {new_state}: {e}")
                
                loop.create_task(send_with_error_handling())
            except RuntimeError:
                from app.realtime.client_socketio.socket_connection import get_socketio_loop

                client_loop = get_socketio_loop()
                if client_loop and not client_loop.is_closed():  # VALIDATE LOOP IS STILL RUNNING
                    # Create coroutine with error handling for thread-safe execution
                    async def thread_safe_send():
                        try:
                            success = await send_agenda_status_update(agenda_item, new_state, actual_timestamp)
                            if success:
                                logger.info(f"🚀 SENT (thread-safe): Agenda update '{agenda_item}' → {new_state} @ {actual_timestamp:.1f}s")
                            else:
                                logger.error(f"🚫 FAILED (thread-safe): Could not send agenda update '{agenda_item}' → {new_state} (disconnected)")
                        except Exception as e:
                            logger.error(f"🚫 ERROR (thread-safe): Failed to send agenda update '{agenda_item}' → {new_state}: {e}")
                    
                    asyncio.run_coroutine_threadsafe(thread_safe_send(), client_loop)
                else:
                    # Create a new event loop for this operation with error handling
                    async def immediate_send():
                        try:
                            success = await send_agenda_status_update(agenda_item, new_state, actual_timestamp)
                            if success:
                                logger.info(f"🚀 SENT (immediate): Agenda update '{agenda_item}' → {new_state} @ {actual_timestamp:.1f}s")
                            else:
                                logger.error(f"🚫 FAILED (immediate): Could not send agenda update '{agenda_item}' → {new_state} (disconnected)")
                        except Exception as e:
                            logger.error(f"🚫 ERROR (immediate): Failed to send agenda update '{agenda_item}' → {new_state}: {e}")
                    
                    asyncio.run(immediate_send())
        except Exception as e:
            logger.error(f"Failed to schedule agenda state update: {e}")

    def record_llm_call_start(self, incremental_payload: str, participant_count: int) -> Dict[str, Any]:
        """Record the start of an LLM call and return call record"""
        call_start_time = time.time()
        self.analytics["total_llm_calls"] += 1
        
        # Format times
        dt_call = datetime.fromtimestamp(call_start_time)
        formatted_call = dt_call.strftime("%I:%M:%S %p")
        # Use a simple relative timestamp based on session start
        session_start_dt = datetime.strptime(self.analytics["session_start"], "%Y-%m-%d %I:%M:%S %p")
        relative_timestamp = call_start_time - session_start_dt.timestamp()
        rel_td = time.gmtime(relative_timestamp)
        formatted_rel = time.strftime("%H:%M:%S", rel_td)

        llm_call_record = {
            "call_number": self.analytics["total_llm_calls"],
            "started_at": formatted_call,
            "relative_time": formatted_rel,
            "transcript_length": len(incremental_payload),
            "participant_count": participant_count,
            "payload": incremental_payload,
            "status": "started",
            "start_timestamp": call_start_time
        }
        
        return llm_call_record

    def record_llm_call_completion(self, call_record: Dict[str, Any], 
                                 changes: List[Any], success: bool = True, error: str | None = None):
        """Record the completion of an LLM call"""
        start_time = call_record.get("start_timestamp", time.time())  # Fallback if missing
        duration = time.time() - start_time
        
        if success:
            call_record.update({
                "status": "completed",
                "duration": f"{duration:.1f}s",
                "state_changes_detected": len(changes),
                "changes": [
                    {
                        "agenda_item": ch.agenda_item,
                        "new_state": ch.new_state
                    } for ch in changes
                ]
            })
        else:
            call_record.update({
                "status": "failed",
                "error": error,
                "duration": f"{duration:.1f}s"
            })
        
        # Remove start_timestamp before storing
        call_record.pop("start_timestamp", None)
        
        # Add call record to history
        self.analytics["llm_call_history"].append(call_record)
        
        # Write updated analytics
        self._write_file_safe()

    def finalize_meeting(self):
        """Finalize meeting analytics when meeting ends"""
        end_time = time.time()
        formatted_end = datetime.fromtimestamp(end_time).strftime("%I:%M:%S %p")
        
        # Calculate session duration
        session_start_dt = datetime.strptime(self.analytics["session_start"], "%Y-%m-%d %I:%M:%S %p")
        duration = end_time - session_start_dt.timestamp()
        
        self.analytics.update({
            "ended_at": formatted_end,
            "session_duration": f"{duration:.1f}s",
            "total_participants": len(self.analytics["participants"]),
            "final_agenda_states": {
                item: v["current_state"]
                for item, v in self.analytics["agenda_items"].items()
            }
        })
        
        self._write_file_safe()
        logger.info(f"Meeting ended - Final analytics: {self.analytics['total_llm_calls']} LLM calls, {len(self.analytics['state_changes'])} state changes")

    def write_to_file(self):
        """Write current analytics to JSON file (synchronous version)"""
        try:
            os.makedirs("analytics", exist_ok=True)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"analytics/meeting_analytics_{timestamp}.json"
            
            # Use meeting_id in filename if available
            if self.analytics["meeting_id"]:
                filename = f"analytics/meeting_{self.analytics['meeting_id']}_analytics.json"
            
            with open(filename, "w", encoding="utf-8") as f:
                json.dump(self.analytics, f, indent=2, ensure_ascii=False)
            
            logger.debug(f"Analytics written to {filename}")
        except Exception as e:
            logger.error(f"Failed to write analytics: {e}")

    async def write_to_file_async(self):
        """Write current analytics to JSON file (async version)"""
        await asyncio.to_thread(self.write_to_file)

    def _write_file_safe(self):
        """Safe file write that works from both event loop and thread pool contexts"""
        import threading
        main_thread = threading.main_thread()
        current_thread = threading.current_thread()
        
        # If we're not in the main thread, use synchronous write to avoid event loop issues
        if current_thread != main_thread:
            self.write_to_file()
        else:
            # We're in the main thread, try async
            try:
                asyncio.get_running_loop()
                # We have a loop, so schedule the async write
                asyncio.create_task(self.write_to_file_async())
            except RuntimeError:
                # No running event loop, use synchronous version
                self.write_to_file()

    def get_analytics(self) -> Dict[str, Any]:
        """Get current analytics data"""
        return self.analytics.copy() 