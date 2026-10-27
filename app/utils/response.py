from fastapi.responses import JSONResponse
from typing import Any
from pydantic import BaseModel

def success_response(data: Any, status_code: int = 200) -> JSONResponse:
    if isinstance(data, BaseModel):
        data = data.model_dump()
    return JSONResponse(
        content={"result": data},
        status_code=status_code
    )

def error_response(error: str, status_code: int = 400) -> JSONResponse:
    return JSONResponse(
        content={"error": error},
        status_code=status_code
    )