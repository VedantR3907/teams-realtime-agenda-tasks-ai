"""
Meeting utilities package

This package contains all utilities related to meeting management,
including analytics, participant management, LLM coordination, and WebSocket event handling.
"""

from .analytics import MeetingAnalytics
from .llm_coordinator import LLMCoordinator
from .participant_manager import ParticipantManager
# Re-export from realtime package for backward compatibility
from app.realtime.websocket_handlers import WebSocketEventHandler

__all__ = [
    "MeetingAnalytics",
    "LLMCoordinator", 
    "ParticipantManager",
    "WebSocketEventHandler"
] 