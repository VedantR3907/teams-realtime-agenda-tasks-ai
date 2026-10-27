from pydantic import BaseModel, Field, field_validator

class TokenRequest(BaseModel):
    meetingId: str = Field(
        ...,  # ... means required
        min_length=1,
        max_length=50,
        description="Unique identifier for the meeting"
    )

    @field_validator('meetingId')
    @classmethod
    def validate_meeting_id(cls, v: str) -> str:
        if not v.strip():
            raise ValueError('meetingId cannot be empty or whitespace')
        return v

class TokenResponse(BaseModel):
    token: str
    token_type: str = Field(default="bearer", pattern="^bearer$")