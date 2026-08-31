from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

LeadStatus = Literal["new", "handled", "dismissed"]


class LeadResponse(BaseModel):
    intent: str | None = None
    caller_name: str | None = None
    callback_number: str | None = None
    urgency: str | None = None
    preferred_time: str | None = None
    details: str | None = None
    status: str | None = None


class TurnResponse(BaseModel):
    role: str
    text: str
    at: datetime


class CallResponse(BaseModel):
    call_id: str
    organization_id: str
    agent_version_id: str | None = None
    business: str | None = None
    from_number: str | None = None
    to_number: str | None = None
    started_at: datetime
    ended_at: datetime | None = None
    outcome: str | None = None
    summary: str | None = None
    model_cost: float = 0
    transcript_deleted_at: datetime | None = None
    leads: list[LeadResponse] = Field(default_factory=list)


class CallDetailResponse(CallResponse):
    transcript: list[TurnResponse] = Field(default_factory=list)


class PageInfo(BaseModel):
    next_cursor: str | None = None
    has_more: bool = False


class CallPage(BaseModel):
    items: list[CallResponse]
    page: PageInfo


class LeadItem(BaseModel):
    id: int
    call_id: str
    intent: str | None = None
    caller_name: str | None = None
    callback_number: str | None = None
    urgency: str | None = None
    preferred_time: str | None = None
    details: str | None = None
    at: datetime | None = None
    status: str = "new"
    status_note: str | None = None
    status_updated_at: datetime | None = None


class LeadPage(BaseModel):
    items: list[LeadItem]
    page: PageInfo


class UpdateLeadStatusRequest(BaseModel):
    status: LeadStatus
    note: str | None = Field(default=None, max_length=1000)
