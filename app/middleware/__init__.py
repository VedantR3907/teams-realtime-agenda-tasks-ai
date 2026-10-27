from fastapi import FastAPI
from .logging import log_requests_middleware

def setup_middlewares(app: FastAPI):
    """Apply all middlewares to the FastAPI app"""
    
    # Add logging middleware last
    app.middleware("http")(log_requests_middleware)