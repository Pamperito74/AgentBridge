from datetime import datetime, timezone
from pydantic import BaseModel, Field
import uuid
import re

# Valid message types
MESSAGE_TYPES = ("chat", "request", "response", "status", "alert", "stream_start", "stream_chunk", "stream_end")

# @mention pattern
MENTION_RE = re.compile(r"@([\w-]+)")


class Agent(BaseModel):
    name: str
    role: str = ""
    status: str = "online"  # online | busy | idle | needs_input
    working_on: str = ""
    capabilities: list[str] = Field(default_factory=list)
    agent_type: str = "bot"  # 'bot' | 'human'
    connected_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    last_seen: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    # Org hierarchy
    reports_to: str | None = None
    # Budget tracking
    budget_monthly_cents: int = 0   # 0 = no limit
    spent_monthly_cents: int = 0


class Task(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    title: str
    description: str = ""
    status: str = "todo"      # todo | in_progress | in_review | done | blocked | cancelled
    priority: str = "medium"  # low | medium | high | critical
    assignee: str | None = None
    created_by: str
    thread: str = "general"
    parent_id: str | None = None
    labels: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    completed_at: datetime | None = None


class CostEvent(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    agent_name: str
    provider: str = "anthropic"
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cost_cents: int = 0
    task_id: str | None = None
    thread: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ActivityEntry(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    actor_type: str = "system"  # system | agent | user
    actor_id: str | None = None
    action: str
    entity_type: str | None = None  # agent | task | message | approval
    entity_id: str | None = None
    details: dict = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class Approval(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    type: str                        # task.execute | budget.exceed | agent.hire | custom
    requested_by: str
    status: str = "pending"          # pending | approved | rejected | cancelled
    payload: dict = Field(default_factory=dict)
    decision_note: str | None = None
    decided_by: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    decided_at: datetime | None = None


class ContentBlock(BaseModel):
    """Structured content block — richer alternative to plain text content."""
    type: str  # text | code | image | file | url
    content: str
    language: str | None = None   # for type=code (e.g. "python", "typescript")
    mime_type: str | None = None  # for type=image|file
    title: str | None = None      # optional display title


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
    # Structured content blocks (v2, optional — content still required for backward compat)
    blocks: list[ContentBlock] = Field(default_factory=list)
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
