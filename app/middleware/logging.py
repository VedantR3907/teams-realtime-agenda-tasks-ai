import time
from loguru import logger
from fastapi import Request
from starlette.responses import Response

async def log_requests_middleware(request: Request, call_next):
    """Middleware to log API requests with execution time"""

    start_time = time.time()  # Start the timer
    
    request_body = None
    if request.method in ("POST", "PUT", "PATCH"):  # For methods with body
        request_body = await request.body()
    
    response = await call_next(request)  # Process the request
    
    duration = (time.time() - start_time) * 1000  # Convert to milliseconds
    logger.info(f"API: {request.method} {request.url.path} | Status: {response.status_code} | Time: {duration:.2f}ms")
    
    return response