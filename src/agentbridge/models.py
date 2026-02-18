from datetime import datetime, timezone
from pydantic import BaseModel, Field
import uuid


class Agent(BaseModel):
    name: str
    role: str = ""
    connected_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    last_seen: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class Message(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    sender: str
    recipient: str | None = None
    thread: str = "general"
    content: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class Thread(BaseModel):
    name: str
    created_by: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
