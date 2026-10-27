import uvicorn
from app.config import settings  # Import your FastAPI settings
import argparse
from loguru import logger

# Parse command line arguments for interview_id and domain
parser = argparse.ArgumentParser(description="Uvicorn config with optional meeting_id and domain.")
parser.add_argument('--bot_join_url', type=str, help='Bot Join URL')
parser.add_argument('--execution_id', type=str, help='Execution ID')
parser.add_argument('--sprint_id', type=str, help='Sprint ID')
parser.add_argument('--item_id', type=str, help='Item ID')
parser.add_argument('--meeting_id', type=str, help='Meeting ID')
parser.add_argument('--organization_id', type=str, help='Organization ID')
parser.add_argument('--backend_url', type=str, help='WebSocket URL')
parser.add_argument('--cw_log_group', type=str, help='CloudWatch Log Group')
parser.add_argument('--adaptive_card_server_url', type=str, help='Adaptive Card Server URL')
args, unknown = parser.parse_known_args()

# Log received CLI arguments (sanitized)
try:
    logger.info("🚀 Uvicorn startup arguments:")
    logger.info(f"  └─ bot_join_url: {args.bot_join_url}")
    logger.info(f"  └─ execution_id: {args.execution_id}")
    logger.info(f"  └─ sprint_id: {args.sprint_id}")
    logger.info(f"  └─ item_id: {args.item_id}")
    logger.info(f"  └─ meeting_id: {args.meeting_id}")
    logger.info(f"  └─ organization_id: {args.organization_id}")
    logger.info(f"  └─ backend_url: {args.backend_url}")
    logger.info(f"  └─ cw_log_group: {args.cw_log_group}")
    logger.info(f"  └─ adaptive_card_server_url: {args.adaptive_card_server_url}")
except Exception:
    pass

if args.meeting_id:
    settings.MEETING_ID = args.meeting_id
if args.execution_id:
    settings.EXECUTION_ID = args.execution_id
if args.sprint_id:
    settings.SPRINT_ID = args.sprint_id
if args.item_id:
    settings.ITEM_ID = args.item_id
if args.organization_id:
    settings.ORGANIZATION_ID = args.organization_id
if args.backend_url:
    settings.SOCKET_URL = args.backend_url
if args.cw_log_group:
    settings.CW_LOG_GROUP = args.cw_log_group
if args.adaptive_card_server_url:
    settings.ADAPTIVE_CARD_SERVER_URL = args.adaptive_card_server_url
if args.bot_join_url:
    settings.BOT_JOIN_URL = args.bot_join_url
ENV = settings.ENVIRONMENT.lower()

if ENV == "development":
    reload = True
    log_level = "info"
    workers = 1
elif ENV == "production":
    reload = False
    log_level = "warning"
    workers = 1
else:
    raise ValueError(f"Unknown APP_ENV: {ENV}")

if __name__ == "__main__":
    uvicorn.run(
        "app.main:app",  # Replace with your actual FastAPI app import
        host=settings.HOST,
        port=settings.PORT,
        reload=reload,
        workers=workers,
        log_level=log_level,
        ws=settings.WS,
        ws_ping_timeout=settings.WS_PING_TIMEOUT
    )