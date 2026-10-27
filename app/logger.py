from loguru import logger
import sys
import os
import logging
import boto3
import watchtower

def _setup_cloudwatch_logging(settings):
    """
    Set up CloudWatch logging for production environment.
    This function mirrors the pattern from the provided example.
    """
    try:
        if (settings.ENVIRONMENT.lower() == "production" and 
            settings.CW_ENABLED):
            
            # Create AWS CloudWatch client
            client = boto3.client("logs", region_name=settings.AWS_REGION)
            
            # Create CloudWatch log handler
            cw_handler = watchtower.CloudWatchLogHandler(
                boto3_client=client,
                log_group=settings.CW_LOG_GROUP,
                stream_name=f"progress_maker_{settings.MEETING_ID or 'default'}",
                create_log_group=True,
                send_interval=getattr(settings, "CW_SEND_INTERVAL", 5),
            )
            cw_handler.setFormatter(logging.Formatter("%(message)s"))
            
            # Create CloudWatch logger
            cw_logger = logging.getLogger("cloudwatch")
            # Map Loguru level string to logging level number
            cw_level = getattr(logging, str(settings.LOG_LEVEL).upper(), logging.INFO)
            cw_logger.setLevel(cw_level)
            cw_logger.addHandler(cw_handler)
            cw_logger.propagate = False
            
            def _loguru_to_stdlib(message):
                """Bridge function to send Loguru logs to stdlib logging for CloudWatch."""
                record = message.record
                # Mirror app.log content and filtering
                if record["extra"].get("system_monitor", False):
                    return
                    
                std_rec = logging.LogRecord(
                    name="cloudwatch",
                    level=record["level"].no,
                    pathname=record["file"].path,
                    lineno=record["line"],
                    msg=record["message"],
                    args=(),
                    exc_info=record["exception"],
                )
                cw_logger.handle(std_rec)
            
            # Add a Loguru sink that formats identically to app.log
            logger.add(
                _loguru_to_stdlib,
                level=settings.LOG_LEVEL,
                enqueue=True,
                backtrace=True,
                diagnose=True,
                format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level} | {file} | {line} | {message}",
            )
            
            logger.info(f"CloudWatch logging initialized for log group: {settings.CW_LOG_GROUP}")
            
    except Exception as e:
        # Gracefully handle CloudWatch setup failures
        logger.warning(f"Failed to setup CloudWatch logging: {str(e)}")
        logger.info("Continuing with local file and console logging only")

def setup_logger(settings):
    # Create a logs directory if it doesn't exist
    LOG_DIR = "logs"
    os.makedirs(LOG_DIR, exist_ok=True)

    # Configure loguru logging
    logger.remove()  # Remove default handler

    logger.add(
        sys.stdout,  # Log to console
        format="{level} | {file} | {line} | {message}",
        level=settings.LOG_LEVEL
    )

    logger.add(
        f"{LOG_DIR}/app.log",
        format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level} | {file} | {line} | {message}",
        rotation="00:00",  # Rotates every midnight
        retention="30 days",  # Keep logs for 30 days
        compression=None,  # Do not compress
        level=settings.LOG_LEVEL,
        enqueue=True,  # Async logging
        backtrace=True,
        diagnose=True,
    )
    
    # Add CloudWatch logging for production environment
    _setup_cloudwatch_logging(settings)
    
    return logger