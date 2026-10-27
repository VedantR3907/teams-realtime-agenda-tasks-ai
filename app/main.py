import asyncio
from contextlib import asynccontextmanager
from app.route import setup_routes
from fastapi import FastAPI
from app.middleware import setup_middlewares
from app.config import settings
from app.logger import setup_logger
from loguru import logger
from app.utils.starter import start_bot_process, stop_bot_process
from app.realtime.client_socketio.socket_connection import connect_with_retry

@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Lifespan context manager for FastAPI application.
    Handles startup and shutdown events.
    """
    # Startup
    logger.info("🚀 Starting Meeting Agenda & Tasks AI server...")
    
    # Only attempt connection if we have the required configuration
    if settings.SECRET_KEY and settings.SOCKET_URL:
        await connect_with_retry()
    else:
        missing = []
        if not settings.SECRET_KEY:
            missing.append("SECRET_KEY")
        if not settings.SOCKET_URL:
            missing.append("SOCKET_URL")
        logger.warning(f"⚠️ Socket.IO connection skipped - missing: {missing}")
        logger.info("💡 Configure these values in .env to enable client server integration")

    # Start bot process
    if settings.ENVIRONMENT.lower() == "production":
        start_bot_process()
    else:
        logger.info("Bot process not started in non-production environment")

    # Yield control back to the application
    yield

    # Shutdown
    stop_bot_process()
    
    logger.info("🛑 Shutting down Meeting Agenda & Tasks AI server...")
    
    try:
        from app.realtime.client_socketio.socket_connection import cleanup_socketio_client
        await cleanup_socketio_client()
        logger.info("✅ Socket.IO client connection cleaned up")
    except Exception as e:
        logger.error(f"❌ Error during Socket.IO cleanup: {e}")

# Create FastAPI app with lifespan context manager
app = FastAPI(title=settings.APP_NAME, debug=settings.DEBUG, lifespan=lifespan)

# Setup logger
setup_logger(settings)

# Attach config to the app instance
app.state.config = settings

# Apply all middlewares first
setup_middlewares(app)

# Then setup routes
setup_routes(app)