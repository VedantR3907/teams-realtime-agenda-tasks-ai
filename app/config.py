from functools import lru_cache
from typing import Optional, Dict
from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    # Base configuration
    APP_NAME: str = "Meeting Agenda & Tasks AI"
    DEBUG: bool = False

    MODEL_PROVIDER: str = "gemini"  # Options: "openai", "gemini"
    OPENAI_API_KEY: Optional[str] = None
    OPENAI_MODEL: str = "gpt-4.1-nano-2025-04-14"  # Default OpenAI model

    GEMINI_API_KEY: Optional[str] = None
    GEMINI_MODEL: str = "gemini-2.0-flash"
    GEMINI_BASE_URL: str = "https://generativelanguage.googleapis.com/v1beta/openai/"
    GEMINI_MINUTES_MODEL: str = "gemini-2.5-pro"
    
    # JWT configuration
    JWT_SECRET_KEY: str = "change-me"  # Change this in production!
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 30
    
    #BOT JOIN URL
    BOT_JOIN_URL: str = "http://localhost:9442"
    # Client Server Configuration
    SOCKET_URL: str = ""  # Base URL of the meeting platform backend (Socket.IO + REST)
    SECRET_KEY: Optional[str] = None  # Shared secret key for generating app-specific tokens (to be provided by client)

    #BOT WORKING DIRECTORY
    BOT_WORKING_DIRECTORY: str = "C:/meeting-bot"

    ADAPTIVE_CARD_SERVER_URL: str = ""  # Notification service that posts task cards in Teams
    # Socket.IO Connection Retry Configuration
    SOCKET_MAX_RETRY_ATTEMPTS: int = 5  # Maximum number of connection retry attempts
    SOCKET_RETRY_DELAY: float = 2.0  # Initial delay between retries in seconds
    SOCKET_RETRY_BACKOFF_MULTIPLIER: float = 1.5  # Multiplier for exponential backoff

    DEEPGRAM_API_KEY: Optional[str] = None
    TRANSCRIPT_FILE_PATH: str = "transcript.txt"
    LLM_BUFFER_FILE_PATH: str = "llm_buffer.json"
    
    # Deepgram STT Configuration
    STT_KEEPALIVE_PERIOD: float = 5.0    # send KA every 5s (Deepgram times out at 10s)
    STT_IDLE_TIMEOUT: float = 45.0       # close connection after 45s with no user audio
    STT_FINALIZE_GAP: float = 0.3        # finalize gap timing in seconds
    STT_MODEL: str = "nova-3"            # Deepgram model to use
    STT_LANGUAGE: str = "multi"          # Language code for speech recognition
    STT_TRANSCRIPT_PATH: str = "transcript"  # Base path for transcript files
    STT_WRITE_FILES: bool = False  # Toggle per-speaker transcript file writes
    
    # Server configuration
    HOST: str = "0.0.0.0"
    PORT: int = 8000
    WS: str = "websockets"
    WS_PING_TIMEOUT: float = 7200.0

    # Logging configuration
    LOG_LEVEL: str = "INFO"
    
    # AWS CloudWatch Logging configuration
    CW_ENABLED: bool = True  # Enable CloudWatch logging (production only)
    AWS_REGION: str = "eu-central-1"  # AWS region for CloudWatch
    CW_LOG_GROUP: str = "meeting-agenda-tasks-ai"  # CloudWatch log group name
    CW_SEND_INTERVAL: int = 5  # Interval in seconds for sending logs to CloudWatch

    # Meeting configuration
    MEETING_ID: str = ""
    TOUCHPOINT_ID: str = ""
    MEETING_TIME_DURATION: int = 0  # Total meeting duration in milliseconds from API
    EXECUTION_ID: str = ""
    SPRINT_ID: str = ""
    ITEM_ID: str = ""
    ORGANIZATION_ID: str = ""
    
    # Touchpoint and meeting metadata storage (global, fetched from REST API)
    TOUCHPOINT_DATA: Dict = {}
    MEETING_START_TIME: str = ""
    MEETING_END_TIME: str = ""
    MEETING_DATE: str = ""
    MEETING_JOIN_URL: str = ""

    # MEETING DETAILS/MEMBERS TOKEN VARIABLES
    CLIENT_ID: str = ""
    CLIENT_SECRET: str = ""
    TENANT_ID: str = ""
    AUTHORITY_GRAPH_API: str = f"https://login.microsoftonline.com/{TENANT_ID}"     # Agenda configuration
    AGENDA_ITEMS: Dict[str, str] = {}
    AGENDA_ITEMS_FULL: list = []  # Full agenda data with timing info
    PREVIOUS_PROGRESS_ITEMS: list = []  # Progress items from previous meetings
    # Mapping between internal agenda titles and external agenda IDs
    AGENDA_TITLE_TO_ID: Dict[str, str] = {}
    AGENDA_ID_TO_TITLE: Dict[str, str] = {}  # Inverse mapping for lookups by id → title
    AGENDA_ID_TO_STATE: Dict[str, str] = {}  # Agenda ID to current state (e.g., "not_started", "in_discussion", "discussed")
    AGENDA_ID_TO_TIMINGS: Dict[str, list] = {}  # Agenda ID to list of timing timestamps
    AGENDA_TITLE_TO_STATE: Dict[str, str] = {}  # Agenda title to current state
    AGENDA_TITLE_TO_TIMINGS: Dict[str, list] = {}  # Agenda title to list of timing timestamps
    
    # Participant name to ID mapping for assignee conversion
    PARTICIPANT_NAME_TO_ID: Dict[str, str] = {}
    # Participant profiles by ID for assignee enrichment
    ORGANIZATION_PARTICIPANTS: list = []
    PARTICIPANT_ID_TO_PROFILE: Dict[str, Dict] = {}

    # CORS configuration
    CORS_ORIGINS: list[str] = []  # Empty by default, will be populated with domains
    CORS_METHODS: list = ["*"]
    CORS_HEADERS: list = ["*"]

    # Environment-specific settings (for dynamic behavior)
    ENVIRONMENT: str = "development"  # Default to development
    
    class Config:
        env_file = ".env"  # Single .env file for all environments
        case_sensitive = True

@lru_cache()
def get_settings():
    """
    Function to load settings based on the environment from the `.env` file.
    """
    settings = Settings()  # Load the settings from the .env file
    
    # Adjust settings dynamically based on the environment
    if settings.ENVIRONMENT.lower() == "production":
        settings.DEBUG = False
        settings.LOG_LEVEL = "INFO"
        # Parse CORS origins from the environment string
        if isinstance(settings.CORS_ORIGINS, str):
            # Remove quotes if present
            cors_str = settings.CORS_ORIGINS.strip('"').strip("'")
            if cors_str == "*":
                settings.CORS_ORIGINS = []  # Disallow "*" in production for security
            else:
                settings.CORS_ORIGINS = [origin.strip() for origin in cors_str.split(",") if origin.strip()]
        else:
            settings.CORS_ORIGINS = []  # No CORS origins allowed if not specified
        settings.CORS_HEADERS = [
            "Authorization",  # For JWT tokens
            "Content-Type",   # For application/json and other content types
            "Accept",         # For content negotiation
            "Origin",        # Required for CORS
            "X-Requested-With"  # For AJAX requests
        ]
    else:
        settings.DEBUG = True
        settings.LOG_LEVEL = "INFO"
        settings.CORS_ORIGINS = ["*"]  # Allow all origins for development
    
    # Ensure AUTHORITY_GRAPH_API includes tenant segment if available
    try:
        from urllib.parse import urlparse
        auth = settings.AUTHORITY_GRAPH_API or ""
        parsed = urlparse(auth)
        if not parsed.scheme or not parsed.netloc:
            # Invalid or empty; build from tenant if present
            if settings.TENANT_ID:
                settings.AUTHORITY_GRAPH_API = f"https://login.microsoftonline.com/{settings.TENANT_ID}"
        else:
            # If path is missing ("/"), append tenant when available
            if (parsed.path == "" or parsed.path == "/") and settings.TENANT_ID:
                settings.AUTHORITY_GRAPH_API = auth.rstrip('/') + f"/{settings.TENANT_ID}"
    except Exception:
        # Fallback best-effort
        if settings.TENANT_ID and (not settings.AUTHORITY_GRAPH_API or settings.AUTHORITY_GRAPH_API.endswith('/')):
            settings.AUTHORITY_GRAPH_API = f"https://login.microsoftonline.com/{settings.TENANT_ID}"

    return settings

# Create a settings instance
settings = get_settings()