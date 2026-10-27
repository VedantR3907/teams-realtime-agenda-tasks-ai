import os
import json
from loguru import logger
from fastapi import HTTPException, APIRouter, WebSocket, WebSocketDisconnect
from app.config import settings
from app.utils.token import JWTAuth
from app.ProgressAnalyzer.transcript_processor import TranscriptProcessor
from app.utils.meeting import MeetingAnalytics, LLMCoordinator, ParticipantManager, WebSocketEventHandler

router = APIRouter()

# Initialize core components
# SPEED OPTIMIZATION: Disabled file output for faster performance
processor = TranscriptProcessor(max_history=500, combined_transcript_path=os.devnull)
analytics = MeetingAnalytics()
llm_coordinator = LLMCoordinator(debounce_interval=0.25)
participant_manager = ParticipantManager()

# Initialize event handler with all components
event_handler = WebSocketEventHandler(processor, analytics, llm_coordinator, participant_manager)

def get_current_transcript():
    """Get the current merged transcript from the processor"""
    return processor.get_payload()

def get_transcript_lines():
    """Get the current transcript as formatted lines"""
    return processor.get_ordered_transcript()

@router.websocket("/")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    logger.info(f"Client connected (meeting_id={settings.MEETING_ID})")

    # Validate Deepgram API key
    if not settings.DEEPGRAM_API_KEY:
        logger.error("DEEPGRAM_API_KEY is not configured")
        await websocket.close(code=1008, reason="Deepgram API key not configured")
        return
    else:
        # Log partial API key for debugging
        masked_key = settings.DEEPGRAM_API_KEY[:5] + "..." if len(settings.DEEPGRAM_API_KEY) > 5 else "invalid"
        logger.info(f"Using Deepgram API key: {masked_key}")

    try:
        while True:
            msg = await websocket.receive_text()    
            data = json.loads(msg)
            event_type = data.get("type")
            
            # Handle the event using the event handler
            await event_handler.handle_event(event_type, data, websocket)

    except WebSocketDisconnect:
        logger.info(f"Client disconnected (meeting_id={settings.MEETING_ID})")
    except json.JSONDecodeError as je:
        logger.error(f"JSON decode error: {je}")
    finally:
        # Cleanup resources
        event_handler.cleanup()