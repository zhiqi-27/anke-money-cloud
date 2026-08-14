from __future__ import annotations

from app.models.sync import APIModel


from pydantic import Field


class AnkeSessionResponse(APIModel):
    access_token: str
    expires_at: str
    uid: str
    provider: str
    display_name: str | None = None
    email: str | None = None


class ProfileUpdateRequest(APIModel):
    display_name: str = Field(min_length=1, max_length=40)
