import time
import asyncio
import threading
from typing import Dict, List, Optional, Any, Callable, Union
from dataclasses import dataclass, field
from loguru import logger
from datetime import datetime


@dataclass
class TimeSession:
    """Represents a single time session for an agenda item"""
    start_wall: float  # Wall-clock time when session started
    end_wall: Optional[float] = None  # Wall-clock time when session ended
    duration: float = 0.0  # Calculated duration in seconds


@dataclass
class AgendaTimer:
    """Tracks timing data for a single agenda item"""
    title: str
    allocated_time: float  # Total allocated time in seconds
    sessions: List[TimeSession] = field(default_factory=list)
    current_session: Optional[TimeSession] = None
    total_time_spent: float = 0.0
    warnings_sent: List[str] = field(default_factory=list)  # Track warning levels sent
    is_active: bool = False
    
    @property
    def remaining_time(self) -> float:
        """Calculate remaining time in seconds"""
        return max(0, self.allocated_time - self.total_time_spent)
    
    @property
    def time_usage_percentage(self) -> float:
        """Calculate percentage of allocated time used"""
        if self.allocated_time <= 0:
            return 0.0
        return (self.total_time_spent / self.allocated_time) * 100
    
    @property
    def is_overtime(self) -> bool:
        """Check if agenda is over allocated time"""
        return self.total_time_spent > self.allocated_time


class AgendaTimerManager:
    """
    Simplified agenda timer manager that tracks wall-clock time for each agenda item.
    Now includes asynchronous background monitoring for automatic overtime alerts.
    """
    
    def __init__(self):
        self.agenda_timers: Dict[str, AgendaTimer] = {}
        self.current_active_agenda: Optional[str] = None
        self._monitoring_task: Optional[asyncio.Task] = None
        self._monitoring_active = False
        self._overtime_callback: Optional[Callable[[str], Any]] = None
        # Track scheduled alert handles (could be asyncio.TimerHandle or threading.Timer)
        self._scheduled_alerts: Dict[str, Union[asyncio.TimerHandle, threading.Timer]] = {}
        
    def initialize_agenda_timers(self, agenda_items: List[Dict[str, Any]]):
        """Initialize timers for all agenda items"""
        for item in agenda_items:
            title = item.get('title', '')
            allocated_time = item.get('time', 0)  # Time in seconds
            
            if title and allocated_time > 0:
                self.agenda_timers[title] = AgendaTimer(
                    title=title,
                    allocated_time=allocated_time
                )
                logger.info(f"📅 Initialized timer for '{title}': {allocated_time}s ({allocated_time/60:.1f} minutes)")
            else:
                logger.warning(f"⚠️ Skipping agenda item '{title}' - no time allocation")
                
        logger.info(f"🕒 Agenda timer manager initialized with {len(self.agenda_timers)} timers")
    
    def set_overtime_callback(self, callback: Callable[[str], Any]):
        """Set the callback function to call when an agenda goes overtime"""
        self._overtime_callback = callback
        logger.info("⚡ Overtime callback registered for automatic alerts")
    
    def _schedule_exact_overtime_alert(self, agenda_title: str, alert_time: float):
        """Schedule an alert to fire exactly when the agenda reaches its allocated time"""

        async def send_alert():
            try:
                # Double-check the timer is still active before sending alert
                timer = self.agenda_timers.get(agenda_title)
                if timer and timer.is_active and timer.current_session:
                    logger.warning(
                        f"🚨 EXACT OVERTIME ALERT: '{agenda_title}' reached {timer.allocated_time}s at {time.time():.3f}"
                    )

                    # Mark overtime warning as sent
                    if "overtime" not in timer.warnings_sent:
                        timer.warnings_sent.append("overtime")

                    # Trigger callback
                    if self._overtime_callback:
                        try:
                            result = self._overtime_callback(agenda_title)
                            if asyncio.iscoroutine(result):
                                await result
                        except Exception as e:
                            logger.error(f"❌ Error in exact overtime callback for '{agenda_title}': {e}")
                else:
                    logger.debug(
                        f"⏭️ Skipped overtime alert for '{agenda_title}' - timer no longer active"
                    )

            except asyncio.CancelledError:
                logger.debug(f"⏹️ Overtime alert cancelled for '{agenda_title}'")
            except Exception as e:
                logger.error(f"❌ Error in scheduled overtime alert for '{agenda_title}': {e}")
            finally:
                if agenda_title in self._scheduled_alerts:
                    del self._scheduled_alerts[agenda_title]

        # Cancel any existing alert for this agenda
        if agenda_title in self._scheduled_alerts:
            self._scheduled_alerts[agenda_title].cancel()

        delay = max(0.0, alert_time - time.time())
        logger.info(
            f"⏰ Scheduled overtime alert for '{agenda_title}' in {delay:.3f}s (at {alert_time:.3f})"
        )

        try:
            # Prefer scheduling on the currently running loop (if any)
            loop = asyncio.get_running_loop()
            handle = loop.call_later(delay, lambda: loop.create_task(send_alert()))
            self._scheduled_alerts[agenda_title] = handle
        except RuntimeError:
            # No running loop in this thread – fall back to main event loop or threading.Timer
            try:
                loop = asyncio.get_event_loop()
                handle = loop.call_later(delay, lambda: loop.create_task(send_alert()))
                self._scheduled_alerts[agenda_title] = handle
            except RuntimeError:
                # Final fallback: use threading.Timer (blocking) to run the coroutine
                def _timer_target():
                    asyncio.run(send_alert())

                t = threading.Timer(delay, _timer_target)
                t.daemon = True
                t.start()
                self._scheduled_alerts[agenda_title] = t
    
    def _cancel_scheduled_alert(self, agenda_title: str):
        """Cancel any scheduled overtime alert for an agenda"""
        if agenda_title in self._scheduled_alerts:
            self._scheduled_alerts[agenda_title].cancel()
            del self._scheduled_alerts[agenda_title]
            logger.debug(f"⏹️ Cancelled scheduled alert for '{agenda_title}'")
    
    async def start_background_monitoring(self, check_interval: float = 1.0):
        """Start background task to monitor all timers for overtime"""
        if self._monitoring_task is not None:
            logger.warning("Background monitoring already running")
            return
        
        self._monitoring_active = True
        self._monitoring_task = asyncio.create_task(self._monitor_timers(check_interval))
        logger.info(f"🔄 Started background timer monitoring (check every {check_interval}s) at {time.time():.3f}")
    
    async def stop_background_monitoring(self):
        """Stop background monitoring task"""
        self._monitoring_active = False
        logger.info(f"⏹️ Stopping background monitoring at {time.time():.3f}")
        if self._monitoring_task:
            self._monitoring_task.cancel()
            try:
                await self._monitoring_task
            except asyncio.CancelledError:
                logger.info("✅ Background monitoring task cancelled successfully")
                pass
            self._monitoring_task = None
            logger.info("⏹️ Background timer monitoring stopped")
    
    async def _monitor_timers(self, check_interval: float):
        """Background task that continuously monitors all active timers"""
        logger.info("🕵️ Background timer monitoring started")
        
        while self._monitoring_active:
            try:
                current_check_time = time.time()
                logger.debug(f"🔍 Timer monitoring check at {current_check_time:.3f}")
                
                # Check all active timers
                for title, timer in self.agenda_timers.items():
                    if timer.is_active and timer.current_session:
                        # Calculate current spent time including active session
                        current_session_duration = current_check_time - timer.current_session.start_wall
                        current_total = timer.total_time_spent + current_session_duration
                        
                        logger.debug(f"📊 '{title}': {current_total:.1f}s/{timer.allocated_time}s (started @ {timer.current_session.start_wall:.3f})")
                        
                        # Check for overtime
                        if current_total > timer.allocated_time and "overtime" not in timer.warnings_sent:
                            overtime_seconds = current_total - timer.allocated_time
                            logger.error(f"🚨 OVERTIME DETECTED: '{title}' has exceeded {timer.allocated_time}s (current: {current_total:.1f}s, overtime: {overtime_seconds:.1f}s)")
                            logger.error(f"🕐 Timer started at: {timer.current_session.start_wall:.3f}")
                            logger.error(f"🕐 Current check time: {current_check_time:.3f}")
                            logger.error(f"🕐 Expected overtime at: {timer.current_session.start_wall + timer.allocated_time:.3f}")
                            logger.error(f"🕐 Detection delay: {current_check_time - (timer.current_session.start_wall + timer.allocated_time):.3f}s")
                            
                            timer.warnings_sent.append("overtime")
                            
                            # Trigger overtime callback if available
                            if self._overtime_callback:
                                try:
                                    # Handle both sync and async callbacks
                                    result = self._overtime_callback(title)
                                    if asyncio.iscoroutine(result):
                                        await result
                                except Exception as e:
                                    logger.error(f"❌ Error in overtime callback for '{title}': {e}")
                
                # Wait before next check
                await asyncio.sleep(check_interval)
                
            except asyncio.CancelledError:
                logger.info("🛑 Timer monitoring cancelled")
                break
            except Exception as e:
                logger.error(f"❌ Error in timer monitoring: {e}")
                await asyncio.sleep(check_interval)  # Continue monitoring despite errors
        
        logger.info("🏁 Background timer monitoring stopped")
    
    def start_agenda_timer(self, agenda_title: str, at_time: float | None = None):
        """Begin timing an agenda item at wall-clock now"""
        if agenda_title not in self.agenda_timers:
            logger.warning(f"❌ Cannot start timer for unknown agenda: {agenda_title}")
            logger.info(f"📋 Available agenda timers: {list(self.agenda_timers.keys())}")
            return
            
        # Pause any other active agenda
        if self.current_active_agenda and self.current_active_agenda != agenda_title:
            self.pause_agenda_timer(self.current_active_agenda)
        
        timer = self.agenda_timers[agenda_title]
        
        # If resuming an agenda that's already overtime, reset overtime warning to allow new alerts
        if timer.total_time_spent > timer.allocated_time and "overtime" in timer.warnings_sent:
            timer.warnings_sent.remove("overtime")
            logger.info(f"🔄 Reset overtime warning for '{agenda_title}' - resuming overtime agenda")
        
        # Start new session at the provided epoch (or fallback to now)
        start = at_time if at_time is not None else time.time()
        timer.current_session = TimeSession(start_wall=start)
        timer.is_active = True
        self.current_active_agenda = agenda_title
        
        # Calculate exact expiration time and schedule alert
        expiration_time = start + timer.allocated_time
        self._schedule_exact_overtime_alert(agenda_title, expiration_time)
        
        logger.info(f"▶️ Started timer for '{agenda_title}' (remaining: {timer.remaining_time:.1f}s) @ {start:.3f}")
        logger.info(f"⏰ Exact overtime alert scheduled for {expiration_time:.3f} ({timer.allocated_time}s from start)")
    
    def pause_agenda_timer(self, agenda_title: str, at_time: float | None = None):
        """Pause timing an agenda item, stopping its current session"""
        if agenda_title not in self.agenda_timers:
            return
            
        # Cancel any scheduled alert for this agenda
        self._cancel_scheduled_alert(agenda_title)
        
        timer = self.agenda_timers[agenda_title]
        
        if timer.current_session and timer.is_active:
            # End current session at the provided epoch (or fallback to now)
            end = at_time if at_time is not None else time.time()
            timer.current_session.end_wall = end
            timer.current_session.duration = (
                timer.current_session.end_wall - timer.current_session.start_wall
            )
            
            # Add to sessions and update total time
            timer.sessions.append(timer.current_session)
            timer.total_time_spent += timer.current_session.duration
            timer.current_session = None
            timer.is_active = False
            
            if self.current_active_agenda == agenda_title:
                self.current_active_agenda = None
                
            logger.info(f"⏸️ Paused timer for '{agenda_title}' (total spent: {timer.total_time_spent:.1f}s, remaining: {timer.remaining_time:.1f}s)")

    def complete_agenda_timer(self, agenda_title: str, at_time: float | None = None):
        """Complete timing an agenda item, finalizing its session and setting remaining time to 0"""
        if agenda_title not in self.agenda_timers:
            logger.warning(f"❌ Cannot complete timer for unknown agenda: {agenda_title}")
            return
            
        # Cancel any scheduled alert for this agenda
        self._cancel_scheduled_alert(agenda_title)
        
        timer = self.agenda_timers[agenda_title]
        
        # If there's an active session, finalize it first
        if timer.current_session and timer.is_active:
            # End current session at the provided epoch (or fallback to now)
            end = at_time if at_time is not None else time.time()
            timer.current_session.end_wall = end
            timer.current_session.duration = (
                timer.current_session.end_wall - timer.current_session.start_wall
            )
            
            # Add to sessions and update total time
            timer.sessions.append(timer.current_session)
            timer.total_time_spent += timer.current_session.duration
            timer.current_session = None
            timer.is_active = False
            
            if self.current_active_agenda == agenda_title:
                self.current_active_agenda = None
        
        # Force completion: set total_time_spent to allocated_time to make remaining_time = 0
        timer.total_time_spent = timer.allocated_time
        
        logger.info(f"🏁 Timer completed for '{agenda_title}' – remaining time set to 0 s")
    
    def get_timer_status(self, agenda_title: str) -> Optional[Dict[str, Any]]:
        """Get current timer status for an agenda item"""
        if agenda_title not in self.agenda_timers:
            return None
            
        timer = self.agenda_timers[agenda_title]
        
        # Calculate current total time including active session
        current_total = timer.total_time_spent
        if timer.current_session and timer.is_active:
            current_session_duration = time.time() - timer.current_session.start_wall
            current_total += current_session_duration
        
        # Check for overtime (but don't send alerts from here - do it from background monitor)
        is_overtime = current_total > timer.allocated_time
        
        return {
            "title": timer.title,
            "allocated_time": timer.allocated_time,
            "allocated_minutes": timer.allocated_time / 60,
            "time_spent": current_total,
            "time_spent_minutes": current_total / 60,
            "remaining_time": max(0, timer.allocated_time - current_total),
            "remaining_minutes": max(0, (timer.allocated_time - current_total) / 60),
            "percentage_used": (current_total / timer.allocated_time) * 100 if timer.allocated_time > 0 else 0,
            "is_active": timer.is_active,
            "is_overtime": is_overtime,
            "sessions_count": len(timer.sessions),
            "warnings_sent": timer.warnings_sent
        }
    
    def get_all_timers_status(self) -> Dict[str, Any]:
        """Get status of all agenda timers"""
        return {
            title: self.get_timer_status(title) 
            for title in self.agenda_timers.keys()
            if self.get_timer_status(title) is not None
        }
    

    async def cleanup(self):
        """Cleanup timer resources"""
        # Stop background monitoring
        await self.stop_background_monitoring()
        
        # Cancel all scheduled alerts
        for agenda_title in list(self._scheduled_alerts.keys()):
            self._cancel_scheduled_alert(agenda_title)
        
        # Pause any active timer
        if self.current_active_agenda:
            self.pause_agenda_timer(self.current_active_agenda)
        
        logger.info("🧹 Agenda timer manager cleaned up") 