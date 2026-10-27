import asyncio
import time
from loguru import logger
from typing import List, Dict, Callable, Awaitable, Set, Optional

class ActiveSpeakerManager:
    """
    Manages and tracks the state of active speakers during a meeting.
    
    This class maintains a list of speakers based on the last time they were
    detected in an audio chunk. It uses a background task to periodically
    check for state changes (speakers becoming active or inactive) and
    notifies clients via a registered callback.
    """
    def __init__(self, timeout_seconds: float = 0.5):
        self._speakers: Dict[str, Dict] = {}  # {email: {displayName, last_seen}}
        self._timeout = timeout_seconds
        self._lock = asyncio.Lock()
        self._on_change_callback: Optional[Callable[[List[Dict]], Awaitable[None]]] = None
        self._last_sent_state: Set[str] = set()

    def set_callback(self, callback: Callable[[List[Dict]], Awaitable[None]]):
        """Register the async callback to be invoked when the active speaker list changes."""
        self._on_change_callback = callback

    async def update_speaker(self, email: str, display_name: str):
        """Updates the last seen time for a speaker. This should be called frequently."""
        async with self._lock:
            self._speakers[email] = {
                "displayName": display_name,
                "last_seen": time.time()
            }

    async def run_notifier(self):
        """
        The main background task that periodically checks for changes and notifies clients.
        This task should be started when a meeting begins.
        """
        while True:
            await asyncio.sleep(self._timeout / 2)  # Check more frequently than the timeout

            if not self._on_change_callback:
                continue

            active_speakers_list = []
            active_speakers_set = set()
            stale_keys = []
            now = time.time()

            async with self._lock:
                # Determine who is currently active and who is stale
                for email, data in self._speakers.items():
                    if (now - data['last_seen']) < self._timeout:
                        active_speakers_list.append({"displayName": data['displayName'], "email": email})
                        active_speakers_set.add(email)
                    else:
                        stale_keys.append(email)

                # Prune stale speakers from the main dictionary
                for key in stale_keys:
                    del self._speakers[key]

            # If the set of active speakers has changed, send an update
            if active_speakers_set != self._last_sent_state:
                logger.info(f"Active speaker state changed. New state: {active_speakers_list}")
                asyncio.create_task(self._on_change_callback(active_speakers_list))
                self._last_sent_state = active_speakers_set

    async def clear(self):
        """Clears all speakers and sends a final empty list to clients."""
        async with self._lock:
            self._speakers.clear()
        
        # If the last state was not empty, send a final clearing update
        if self._on_change_callback and self._last_sent_state:
            await self._on_change_callback([])
            self._last_sent_state = set() 