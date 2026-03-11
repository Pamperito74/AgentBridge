from datetime import datetime, timezone
from pydantic import BaseModel, Field
import uuid
import re

# Valid message types
MESSAGE_TYPES = ("chat", "request", "response", "status", "alert")

# @mention pattern
MENTION_RE = re.compile(r"@([\w-]+)")


class Agent(BaseModel):
    name: str
    role: str = ""
    status: str = "online"  # online | busy | idle
    working_on: str = ""
    capabilities: list[str] = Field(default_factory=list)
    agent_type: str = "bot"  # 'bot' | 'human'
    connected_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    last_seen: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class Artifact(BaseModel):
    type: str  # file | code | url
    content: str


class Message(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    # Generic event envelope
    actor_id: str | None = None
    actor_type: str = "agent"
    target_id: str | None = None
    target_type: str | None = None
    event_type: str = "note.text"
    metadata: dict = Field(default_factory=dict)
    labels: list[str] = Field(default_factory=list)
    # Backward-compatible aliases
    sender: str
    recipient: str | None = None
    thread: str = "general"
    content: str
    msg_type: str = "chat"
    mentions: list[str] = Field(default_factory=list)
    artifacts: list[Artifact] = Field(default_factory=list)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    # Request-response correlation
    correlation_id: str | None = None  # UUID linking request to response

    def model_post_init(self, __context):
        if self.actor_id is None:
            self.actor_id = self.sender
        if self.target_id is None and self.recipient:
            self.target_id = self.recipient
        if self.msg_type == "chat" and self.event_type == "note.text":
            pass
        elif self.event_type == "note.text":
            self.event_type = f"message.{self.msg_type}"
        # Auto-extract @mentions from content if not explicitly provided
        if not self.mentions:
            self.mentions = MENTION_RE.findall(self.content)


class AgentRequest(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    from_agent: str
    to_agent: str
    content: str
    status: str = "pending"  # pending | answered | timeout | rejected
    correlation_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    thread: str = "general"
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    expires_at: datetime
    response: str | None = None
    responded_at: datetime | None = None


class Thread(BaseModel):
    name: str
    created_by: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ThreadSummary(BaseModel):
    name: str
    message_count: int
    participants: list[str]
    first_message_at: datetime | None = None
    last_message_at: datetime | None = None
    last_message_preview: str = ""
