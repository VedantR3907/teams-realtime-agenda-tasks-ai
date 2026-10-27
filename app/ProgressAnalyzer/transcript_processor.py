import threading
from datetime import datetime
from loguru import logger
from typing import List, Dict, Optional
import os


def format_timestamp(ts: float, meeting_start: Optional[float] = None) -> str:
    if meeting_start is not None:
        delta = int(ts - meeting_start)
        if delta < 0:
            delta = 0
        h, rem = divmod(delta, 3600)
        m, s   = divmod(rem, 60)
        return f"{h:02d}:{m:02d}:{s:02d}"
    else:
        return datetime.fromtimestamp(ts).strftime('%H:%M:%S')


class TranscriptProcessor:
    """
    Accumulates utterances from multiple speakers and
    generates a merged, chronologically ordered transcript payload.
    """
    def __init__(self, max_history: int = 1000, 
                 combined_transcript_path: str = "transcript/combined_transcript.txt",
                 meeting_start: Optional[float] = None):
        # Protect access to internal state
        self._lock = threading.Lock()
        # List of utterance records: {'start': float, 'user': str, 'text': str}
        self._utterances: List[Dict] = []
        # Maximum number of utterances to keep (to bound memory)
        self._max_history = max_history
        # Path for the combined transcript file
        self._combined_transcript_path = combined_transcript_path
        # The POSIX seconds timestamp when the meeting started:
        # used to normalize all utterance times
        self.meeting_start = meeting_start
        
        # Initialize the combined transcript file path (create folder if needed)
        try:
            dir_path = os.path.dirname(self._combined_transcript_path)
            # Only attempt to create the directory when a non-empty directory is provided
            if dir_path:
                os.makedirs(dir_path, exist_ok=True)
            # Create/clear the file (works with regular files and os.devnull)
            with open(self._combined_transcript_path, "w", encoding="utf-8") as f:
                f.write("")
        except Exception as e:
            logger.error(f"Error preparing combined transcript file: {e}")

    def add_utterance(
        self,
        user: str,
        start_time: float,
        end_time: float,
        text: str
    ) -> None:
        """
        Add a finalized utterance for a speaker.

        Args:
            user: display name or identifier of the speaker
            start_time: utterance start time (posix seconds)
            end_time: utterance end time (posix seconds)
            text: the transcribed text of the utterance
        """
        with self._lock:
            # Append new record
            self._utterances.append({
                'start': start_time,
                'end': end_time,
                'user': user,
                'text': text,
            })
            # If exceeding history limit, drop oldest
            if len(self._utterances) > self._max_history:
                self._utterances.pop(0)
        
        # Persist combined transcript to file
        self._write_combined_transcript()

    def _write_combined_transcript(self) -> None:
        """
        Write the current merged transcript to the combined transcript file.
        """
        lines = self.get_ordered_transcript()
        try:
            with open(self._combined_transcript_path, "w", encoding="utf-8") as f:
                f.write("\n".join(lines))
                if lines:  # Add final newline if there's content
                    f.write("\n")
        except Exception as e:
            # Log error but don't raise to avoid breaking the main flow
            logger.error(f"Error writing combined transcript: {e}")

    def get_ordered_transcript(self) -> List[str]:
        """
        Retrieve the merged transcript as a list of formatted lines,
        sorted by utterance start time.
        """
        with self._lock:
            # Create a shallow copy and sort
            utts = sorted(self._utterances, key=lambda u: u['start'])

        lines = []
        for u in utts:
            ts = format_timestamp(u['start'], self.meeting_start)
            lines.append(f"[{ts}] [{u['user']}] {u['text']}")
        return lines

    def get_payload(self) -> str:
        """
        Return the assembled transcript as a single string,
        suitable for sending to an LLM or external service.
        """
        lines = self.get_ordered_transcript()
        return "\n".join(lines)

    def clear(self) -> None:
        """
        Clear all stored utterances.
        """
        with self._lock:
            self._utterances.clear()
        
        # Clear the combined transcript file
        try:
            with open(self._combined_transcript_path, "w", encoding="utf-8") as f:
                f.write("")
        except Exception as e:
            logger.error(f"Error clearing combined transcript file: {e}")
