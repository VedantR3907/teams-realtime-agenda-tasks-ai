import asyncio
from typing import Optional

from loguru import logger

from app.config import settings
from app.realtime.client_socketio.client_websocket import MeetingSocketIOClient


_socketio_client: Optional[MeetingSocketIOClient] = None
_socketio_loop: Optional[asyncio.AbstractEventLoop] = None


async def get_socketio_client() -> MeetingSocketIOClient:
    """Get or create the global Socket.IO client instance."""
    global _socketio_client, _socketio_loop

    if _socketio_client is None:
        _socketio_client = MeetingSocketIOClient()
        _socketio_loop = asyncio.get_running_loop()
        await _socketio_client.connect()

    return _socketio_client


def get_socketio_loop() -> Optional[asyncio.AbstractEventLoop]:
    """Return the event loop used by the Socket.IO client if initialized."""
    return _socketio_loop


async def cleanup_socketio_client() -> None:
    """Cleanup the Socket.IO client connection."""
    global _socketio_client, _socketio_loop

    if _socketio_client:
        await _socketio_client.disconnect()
        _socketio_client = None

    _socketio_loop = None
    logger.info("Socket.IO client cleaned up")

async def connect_with_retry() -> None:
    """
    Attempt to connect to the Socket.IO server with retry logic.
    Uses exponential backoff between retry attempts.
    """
    
    
    max_attempts = settings.SOCKET_MAX_RETRY_ATTEMPTS
    delay = settings.SOCKET_RETRY_DELAY
    backoff_multiplier = settings.SOCKET_RETRY_BACKOFF_MULTIPLIER
    
    for attempt in range(1, max_attempts + 1):
        try:
            logger.info(f"🔌 Connecting to client Socket.IO server: {settings.SOCKET_URL} (Attempt {attempt}/{max_attempts})")
            client = await get_socketio_client()
            
            if client.connected:
                logger.info("✅ Successfully connected to client Socket.IO server")
                return
            else:
                logger.warning(f"⚠️ Failed to connect to client Socket.IO server (Attempt {attempt}/{max_attempts})")
                
        except Exception as e:
            logger.error(f"❌ Error connecting to client Socket.IO server (Attempt {attempt}/{max_attempts}): {e}")
        
        # If this wasn't the last attempt, wait before retrying
        if attempt < max_attempts:
            logger.info(f"⏳ Waiting {delay:.1f} seconds before retry...")
            await asyncio.sleep(delay)
            delay *= backoff_multiplier  # Exponential backoff
        else:
            logger.error(f"❌ Failed to connect to Socket.IO server after {max_attempts} attempts")
            logger.warning("📡 Server will continue running, but Socket.IO events will fail")