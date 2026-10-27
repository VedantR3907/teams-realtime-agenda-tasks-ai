from loguru import logger
from typing import Optional
from jose import jwt, JWTError
from app.config import settings
from fastapi import HTTPException
from datetime import datetime, timedelta, timezone
import time
import msal

# You should store this securely in environment variables
JWT_SECRET_KEY = settings.JWT_SECRET_KEY
JWT_ALGORITHM = settings.JWT_ALGORITHM
ACCESS_TOKEN_EXPIRE_MINUTES = settings.ACCESS_TOKEN_EXPIRE_MINUTES

class JWTAuth:
    @staticmethod
    def create_token(data: dict, expires_delta: Optional[timedelta] = None, secret_key: str = JWT_SECRET_KEY) -> str:
        """
        Create a new JWT token
        """
        to_encode = data.copy()
        if expires_delta:
            expire = datetime.now(timezone.utc) + expires_delta
        else:
            expire = datetime.now(timezone.utc) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
            
        to_encode.update({"exp": expire})
        encoded_jwt = jwt.encode(to_encode, secret_key, algorithm=JWT_ALGORITHM)
        return encoded_jwt
    
    @staticmethod
    def decrypt_token(token: str) -> dict:
        """
        Decrypt and validate the JWT token
        """
        try:
            payload = jwt.decode(token, JWT_SECRET_KEY, algorithms=[JWT_ALGORITHM])
            return payload
        except JWTError as e:
            logger.error(f"JWT validation error: {str(e)}")
            raise HTTPException(
                status_code=403,
                detail="Invalid token or expired token"
            )

    @staticmethod
    def verify_token(auth_header: str | None) -> dict:
        """Verify JWT token and return meetingId
        
        Args:
            auth_header: Authorization header containing the Bearer token
            
        Returns:
            dict: The payload from the token including meetingId
            
        Raises:
            HTTPException: If token is invalid or missing required data
        """
        if not auth_header:
            logger.error("No authorization header found")
            raise HTTPException(status_code=403, detail="No authorization header found")
        
        try:
            scheme, token = auth_header.split()
        except ValueError:
            logger.error("Invalid authorization header format")
            raise HTTPException(status_code=403, detail="Invalid authorization header format")

        if scheme.lower() != "bearer":
            logger.error("Invalid authentication scheme")
            raise HTTPException(status_code=403, detail="Invalid authentication scheme")

        try:
            payload = JWTAuth.decrypt_token(token)
            logger.info(f"payload: {payload}")
            
            # Check for meetingId
            if "meetingId" not in payload:
                logger.error("No 'meetingId' in the token")
                raise HTTPException(status_code=403, detail="Missing 'meetingId' in token")
            
            # Validate meetingId
            if not isinstance(payload["meetingId"], str):
                logger.error("Invalid 'meetingId' type; expected a string")
                raise HTTPException(status_code=403, detail="'meetingId' must be a string")
                
            return payload
            
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Token validation error: {str(e)}")
            raise HTTPException(status_code=403, detail="Invalid token")


def get_api_token(
    *,
    client_id: Optional[str] = None,
    client_secret: Optional[str] = None,
    authority: Optional[str] = None,
    scope: Optional[str] = None,
    default_scope: str = "api://your-platform-app-id/.default",
    max_attempts: int = 3,
    base_delay: float = 0.5,
) -> str:
    """
    Acquire an application token using client credentials.

    - If explicit credentials/scope are provided, they are used.
    - Otherwise falls back to configured defaults and default_scope.
    """
    last_error: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            resolved_client_id = client_id or settings.CLIENT_ID
            resolved_client_secret = client_secret or settings.CLIENT_SECRET
            resolved_authority = authority or settings.AUTHORITY_GRAPH_API
            resolved_scope = scope or default_scope

            if not (resolved_client_id and resolved_client_secret and resolved_authority and resolved_scope):
                raise Exception("Missing credentials or scope for token acquisition")

            app = msal.ConfidentialClientApplication(
                client_id=resolved_client_id,
                client_credential=resolved_client_secret,
                authority=resolved_authority,
            )

            result = app.acquire_token_for_client(scopes=[resolved_scope])  # type: ignore[arg-type]

            if "access_token" not in result:
                error_description = result.get("error_description", "Unknown error")
                error_code = result.get("error", "unknown_error")
                raise Exception(f"Token acquisition failed: {error_code} - {error_description}")


            return result["access_token"]
        except Exception as error:
            last_error = error
            logger.error(f"API token acquisition error (attempt {attempt}/{max_attempts}): {error}")
            if attempt < max_attempts:
                delay = base_delay * (2 ** (attempt - 1))
                time.sleep(delay)
            else:
                break
    assert last_error is not None
    raise last_error