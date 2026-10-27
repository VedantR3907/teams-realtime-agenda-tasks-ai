import threading
import time
import asyncio
from loguru import logger
from typing import Callable, Optional, TYPE_CHECKING, Dict, List
from app.ProgressAnalyzer.processors import LLMProcessor, ProgressLLMProcessor
from app.ProgressAnalyzer.transcript_processor import TranscriptProcessor
from .analytics import MeetingAnalytics
from datetime import datetime

def convert_name_to_id(name: str) -> str:
    """Convert participant name to ID using the stored mapping"""
    from app.config import settings
    
    if not name:
        return name
    
    mapping = settings.PARTICIPANT_NAME_TO_ID or {}
    # Try exact match first (supports full name or email)
    participant_id = mapping.get(name)
    if participant_id:
        return participant_id

    lowered = name.lower()
    # Try case-insensitive match across both names and emails
    for stored_key, stored_id in mapping.items():
        try:
            if isinstance(stored_key, str) and stored_key.lower() == lowered:
                return stored_id
        except Exception:
            continue
    
    # If no match found, log warning and return original name as fallback
    logger.warning(f"⚠️ No ID found for participant name: '{name}', using name as fallback")
    return name

if TYPE_CHECKING:
    from .agenda_timer_manager import AgendaTimerManager

class LLMCoordinator:
    """
    Coordinates LLM processing with debouncing and immediate socket emissions.
    
    Key Features:
    - Debounced LLM call scheduling to reduce API costs
    - Incremental transcript processing (only new content since last call)
    - Immediate socket emissions (don't wait for both LLMs)
    - Thread-safe timer management
    - Integration with analytics tracking
    """
    
    def __init__(self, debounce_interval: float = 0.25):
        self.debounce_interval = debounce_interval
        self._task: Optional[asyncio.Task] = None
        self._last_scheduled_at: Optional[float] = None
        self._lock = threading.Lock()
        self.llm_processor: Optional[LLMProcessor] = None
        self.progress_processor: Optional[ProgressLLMProcessor] = None
        self.transcript_processor: Optional[TranscriptProcessor] = None
        self.analytics: Optional[MeetingAnalytics] = None
        self.timer_manager: Optional['AgendaTimerManager'] = None
        self._last_processed_utterance_count = 0
        
        # Track sent progress items to avoid duplicates
        self._sent_progress_items: Dict[str, Dict] = {}
        
        # Meeting participants - will be provided during initialization
        self.meeting_participants: List[str] = []

    def initialize(self, llm_processor: Optional[LLMProcessor], transcript_processor: TranscriptProcessor,
                  analytics: MeetingAnalytics, timer_manager: Optional['AgendaTimerManager'] = None,
                  progress_processor: Optional[ProgressLLMProcessor] = None,
                  meeting_participants: Optional[List[str]] = None):
        """Initialize the coordinator with required components and meeting participants"""
        self.llm_processor = llm_processor
        self.progress_processor = progress_processor
        self.transcript_processor = transcript_processor
        self.analytics = analytics
        self.timer_manager = timer_manager
        self._last_processed_utterance_count = 0
        
        # Use provided participants or fallback to default list
        if meeting_participants:
            self.meeting_participants = meeting_participants
            logger.info(f"📋 Using provided meeting participants ({len(meeting_participants)} participants)")
        else:
            self.meeting_participants = []
            logger.warning(f"⚠️ No participants provided, using defaults")
        
        logger.info(f"LLM Coordinator initialized with {len(self.meeting_participants)} participants")

    def reset_for_new_meeting(self):
        """Reset incremental transcript tracking for new meeting"""
        self._last_processed_utterance_count = 0
        self._sent_progress_items.clear()
        logger.info("LLM Coordinator reset for new meeting")

    def remove_progress_item(self, item_id: str):
        """Remove a progress item from local tracking so it won't be included in prompts."""
        if not item_id:
            return
        # Remove from sent cache
        if item_id in self._sent_progress_items:
            del self._sent_progress_items[item_id]
        # Remove from progress processor state
        if self.progress_processor:
            self.progress_processor.items_missing_details = [p for p in self.progress_processor.items_missing_details if p.get("id") != item_id]
            self.progress_processor.completed_items = [p for p in self.progress_processor.completed_items if p.get("id") != item_id]
            # Prune conversation history for this item id
            try:
                if hasattr(self.progress_processor, 'prune_history_for_item'):
                    self.progress_processor.prune_history_for_item(item_id)
            except Exception as e:
                logger.error(f"Failed to prune conversation history for item '{item_id}': {e}")
        logger.info(f"🗑️ Removed progress item '{item_id}' from coordinator state")

    def schedule_llm_call(self):
        """
        Schedule an LLM call using asyncio for the debounce logic.
        Only the last scheduled call after debounce_interval will execute.
        """
        if not (self.llm_processor or self.progress_processor) or not self.transcript_processor or not self.analytics:
            logger.warning("LLM Coordinator not properly initialized, skipping call schedule")
            return

        with self._lock:
            now = time.time()

            # Cancel any existing task
            if self._task is not None:
                self._task.cancel()

            self._last_scheduled_at = now

            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                # If no loop is running, fall back to a new loop in a background thread
                def run_sync():
                    asyncio.run(self._run_llm_async(now))
                threading.Thread(target=run_sync, daemon=True).start()
                return

            # Schedule async debounce task
            self._task = loop.create_task(self._run_llm_async(now))

    def cancel_pending_calls(self):
        """Cancel any pending LLM calls"""
        with self._lock:
            if self._task:
                self._task.cancel()
                self._task = None
                logger.info("Cancelled pending LLM call")

    async def flush_pending_operations(self, timeout: float = 5.0):
        """
        Flush any pending LLM operations to prevent data loss during cleanup.
        
        Parameters:
        -----------
        timeout: float
            Maximum time to wait for operations to complete (seconds)
        """
        logger.info(f"🔄 Flushing pending operations (timeout: {timeout}s)")
        
        # Wait for any active LLM task to complete
        with self._lock:
            task = self._task
        
        if task and not task.done():
            logger.info("⏳ Waiting for active LLM task to complete...")
            try:
                await asyncio.wait_for(task, timeout=timeout)
                logger.info("✅ LLM task completed successfully")
            except asyncio.TimeoutError:
                logger.warning(f"⚠️ LLM task timed out after {timeout}s, cancelling...")
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            except Exception as e:
                logger.error(f"❌ Error waiting for LLM task: {e}")
        
        # Small delay to allow any final SocketIO requests to complete
        logger.info("⏳ Allowing 1s for final SocketIO requests to complete...")
        await asyncio.sleep(1.0)
        
        logger.info("✅ All pending operations flushed")

    def _get_incremental_transcript(self) -> str:
        """Get only the new transcript content since the last LLM call"""
        if not self.transcript_processor:
            return ""

        # Get all current transcript lines
        all_lines = self.transcript_processor.get_ordered_transcript()
        
        # Get only the new lines since last processing
        new_lines = all_lines[self._last_processed_utterance_count:]
        
        # Update the counter for next time
        self._last_processed_utterance_count = len(all_lines)
        
        # Return the incremental content
        incremental_payload = "\n".join(new_lines)
        
        logger.info(f"Incremental transcript: {len(new_lines)} new utterances (total: {len(all_lines)})")
        
        return incremental_payload

    async def _run_llm_async(self, scheduled_at: float):
        """Async wrapper that waits for the debounce interval then runs the blocking LLM call."""
        try:
            await asyncio.sleep(self.debounce_interval)
            await asyncio.to_thread(self._run_llm, scheduled_at)
        except asyncio.CancelledError:
            logger.debug("LLM call task cancelled before execution")

    def _run_llm(self, scheduled_at: float):
        """Called after debounce_interval of no new utterances."""
        # If this run is *not* the most recently scheduled, ignore it
        with self._lock:
            if scheduled_at != self._last_scheduled_at:
                logger.debug(f"Ignoring stale LLM run scheduled at {scheduled_at}")
                return

        # Get only incremental transcript content (new since last LLM call)
        incremental_payload = self._get_incremental_transcript()
        
        # Skip LLM call if no new content
        if not incremental_payload.strip():
            logger.info("No new transcript content since last LLM call, skipping")
            return

        # Record LLM call start
        if self.analytics:
            participant_count = len(self.analytics.get_analytics()["participants"])
            call_record = self.analytics.record_llm_call_start(incremental_payload, participant_count)
        else:
            call_record = {"call_number": 0}
        logger.info(f"📞 Starting LLM Call #{call_record['call_number']} with {len(incremental_payload)} characters")
        
        try:
            # 🚀 SPEED OPTIMIZATION: Process results immediately as they become available
            if self.llm_processor and self.progress_processor:
                from concurrent.futures import ThreadPoolExecutor, as_completed
                
                with ThreadPoolExecutor(max_workers=2) as ex:
                    fut_agenda = ex.submit(self.llm_processor.process_transcript, incremental_payload)
                    fut_progress = ex.submit(self.progress_processor.process_transcript, incremental_payload)
                    
                    # Process results immediately as they become available
                    agenda_changes = []
                    progress_item = None
                    
                    for future in as_completed([fut_agenda, fut_progress]):
                        if future == fut_agenda:
                            agenda_changes = future.result()
                            # 🚀 IMMEDIATE EMISSION: Send agenda changes as soon as ready
                            self._process_agenda_changes_immediately(agenda_changes, call_record)
                        elif future == fut_progress:
                            progress_item = future.result()
                            # 🚀 IMMEDIATE EMISSION: Send progress items as soon as ready
                            self._process_progress_items_immediately(progress_item)

            elif self.llm_processor:
                agenda_changes = self.llm_processor.process_transcript(incremental_payload)
                self._process_agenda_changes_immediately(agenda_changes, call_record)
            elif self.progress_processor:
                progress_item = self.progress_processor.process_transcript(incremental_payload)
                self._process_progress_items_immediately(progress_item)
            
            logger.info("✅ All LLM responses processed and emitted immediately")
                
        except Exception as e:
            # Record failed completion
            if self.analytics:
                self.analytics.record_llm_call_completion(call_record, [], success=False, error=str(e))
            logger.error(f"LLM Call #{call_record['call_number']} failed: {e}")

    def _process_agenda_changes_immediately(self, agenda_changes, call_record):
        """
        Process and emit agenda changes immediately when LLM responds (SPEED OPTIMIZATION).
        
        Note: agenda_changes are already validated by LLM processor - invalid transitions 
        have been filtered out and only valid state changes are received here.
        """
        if not agenda_changes:
            logger.debug("No agenda changes to process immediately")
            return
            
        logger.info(f"🚀 IMMEDIATE: Processing {len(agenda_changes)} pre-validated agenda changes")
        
        # Record successful completion for agenda changes (already validated by LLM processor)
        if self.analytics:
            self.analytics.record_llm_call_completion(call_record, agenda_changes, success=True)
        
        # Sort agenda changes to ensure "in_discussion" and "in_progress" items are sent last
        if len(agenda_changes) > 1:
            def sort_key(change):
                if change.new_state in ["in_discussion", "in_progress"]:
                    return 1  # Send these last
                else:
                    return 0  # Send these first
            agenda_changes = sorted(agenda_changes, key=sort_key)
        
        # Process validated agenda changes (LLM processor already updated its own states)
        for i, ch in enumerate(agenda_changes):
            # Add 200ms delay between multiple agenda updates (but not before the first one)
            if len(agenda_changes) > 1 and i > 0:
                logger.debug(f"Adding 200ms delay before processing agenda {i+1}/{len(agenda_changes)}")
                time.sleep(0.2)  # 200ms delay
            
            # Pass the actual speaker timestamp (in seconds) to analytics
            if self.analytics:
                self.analytics.update_agenda_state(ch.agenda_item, ch.new_state, time.time())
            
            # Sync agenda status with progress processor for better DAI prompt context
            if self.progress_processor and hasattr(self.progress_processor, 'agenda_status'):
                self.progress_processor.agenda_status[ch.agenda_item] = ch.new_state
                logger.debug(f"✅ Synced agenda status: {ch.agenda_item} -> {ch.new_state}")
            
            # Update timer manager with LLM-detected state changes
            if hasattr(self, 'timer_manager') and self.timer_manager:
                if ch.new_state == "In Progress":
                    self.timer_manager.start_agenda_timer(ch.agenda_item)
                    logger.info(f"🕒 Started timer for '{ch.agenda_item}' (LLM-detected)")
                else:
                    self.timer_manager.pause_agenda_timer(ch.agenda_item)
                    logger.info(f"⏸️ Paused timer for '{ch.agenda_item}' (LLM-detected)")
    
    def _process_progress_items_immediately(self, progress_item):
        """Process and emit progress items immediately when LLM responds (SPEED OPTIMIZATION)"""
        if progress_item is None:
            logger.debug("No progress items to process immediately")
            return
            
        logger.info(f"🚀 IMMEDIATE: Processing progress items")
        
        # Handle progress item updates - send both partial and completed items immediately
        items_sent_count = 0
        if self.progress_processor and (self.progress_processor.completed_items or self.progress_processor.items_missing_details):
            # Combine all items (completed + partial) for immediate sending
            all_items = list(self.progress_processor.completed_items) + list(self.progress_processor.items_missing_details)
            
            if all_items:
                # Filter to only new or changed items
                items_to_send = []
                for item in all_items:
                    item_id = item.get('id')
                    if not item_id:
                        continue
                    
                    assignee = item.get('assignee', '')
                    due_date = item.get('due_date_iso', '')
                    status = item.get('status', 'remaining')
                    description = item.get('description', '')
                    
                    # Check if this item has changed compared to what we last sent
                    last_sent = self._sent_progress_items.get(item_id, {})
                    if (last_sent.get('assignee') != assignee or 
                        last_sent.get('due_date_iso') != due_date or 
                        last_sent.get('status') != status or
                        last_sent.get('description') != description):
                        items_to_send.append(item)
                
                if items_to_send:
                    complete_count = sum(1 for item in items_to_send if bool(item.get('assignee')) and bool(item.get('due_date_iso')))
                    partial_count = len(items_to_send) - complete_count
                    logger.info(f"🚀 IMMEDIATE: Sending {len(items_to_send)} items ({complete_count} complete, {partial_count} partial)")
                    
                    from app.realtime.client_socketio.socket_actions import send_progress_item_update
                    
                    for item in items_to_send:
                        item_id = item.get('id')
                        assignee = item.get('assignee', '')
                        due_date = item.get('due_date_iso', '')
                        
                        progress_item_data = {
                            "id": item_id,
                            "description": item.get('description', ''),
                            "progressItemType": item.get('progress_type', 'agreement').lower(),
                            "assignee": convert_name_to_id(assignee) if assignee else '',  # Convert to ID or empty string
                            "dueDate": due_date,  # Empty string if not set
                            "resolved": item.get('status') == 'completed'  # Map status to resolved
                        }
                        
                        is_complete = bool(assignee) and bool(due_date)
                        logger.info(f"🚀 IMMEDIATE: Sending {'complete' if is_complete else 'partial'} item: {item_id}")
                        
                        try:
                            loop = asyncio.get_running_loop()
                            
                            # Create task with error handling
                            async def send_with_error_handling():
                                try:
                                    success = await send_progress_item_update(progress_item_data)
                                    if success:
                                        logger.info(f"🚀 SENT: Progress item '{item_id}'")
                                    else:
                                        logger.error(f"🚫 FAILED: Could not send progress item '{item_id}' (disconnected)")
                                except Exception as e:
                                    logger.error(f"🚫 ERROR: Failed to send progress item '{item_id}': {e}")
                            
                            loop.create_task(send_with_error_handling())
                        except RuntimeError:
                            # Try to use the SocketIO loop if available and still running
                            from app.realtime.client_socketio.socket_connection import get_socketio_loop
                            client_loop = get_socketio_loop()
                            if client_loop and not client_loop.is_closed():
                                async def thread_safe_send():
                                    try:
                                        success = await send_progress_item_update(progress_item_data)
                                        if success:
                                            logger.info(f"🚀 SENT (thread-safe): Progress item '{item_id}'")
                                        else:
                                            logger.error(f"🚫 FAILED (thread-safe): Could not send progress item '{item_id}' (disconnected)")
                                    except Exception as e:
                                        logger.error(f"🚫 ERROR (thread-safe): Failed to send progress item '{item_id}': {e}")
                                
                                asyncio.run_coroutine_threadsafe(thread_safe_send(), client_loop)
                            else:
                                async def immediate_send():
                                    try:
                                        success = await send_progress_item_update(progress_item_data)
                                        if success:
                                            logger.info(f"🚀 SENT (immediate): Progress item '{item_id}'")
                                        else:
                                            logger.error(f"🚫 FAILED (immediate): Could not send progress item '{item_id}' (disconnected)")
                                    except Exception as e:
                                        logger.error(f"🚫 ERROR (immediate): Failed to send progress item '{item_id}': {e}")
                                
                                asyncio.run(immediate_send())
                        
                        # Update tracking immediately
                        self._sent_progress_items[item_id] = {
                            'assignee': assignee,
                            'due_date_iso': due_date,
                            'status': item.get('status', 'remaining'),
                            'description': item.get('description', ''),
                            'sent_timestamp': time.time()
                        }
                        items_sent_count += 1
                    
                    logger.info(f"🚀 IMMEDIATE: Sent {items_sent_count} progress item updates")
                else:
                    logger.info("🚀 IMMEDIATE: No new or changed progress items to send")
            else:
                logger.info("🚀 IMMEDIATE: No progress items to send")

    def get_status(self) -> dict:
        """Get current status of the coordinator"""
        with self._lock:
            status = {
                "debounce_interval": self.debounce_interval,
                "has_llm_processor": self.llm_processor is not None,
                "has_progress_processor": self.progress_processor is not None,
                "has_analytics": self.analytics is not None,
                "has_timer_manager": self.timer_manager is not None,
                "last_processed_count": self._last_processed_utterance_count,
                "sent_progress_items_count": len(self._sent_progress_items)
            }
            
            return status