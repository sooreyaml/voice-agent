from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import AnyHttpUrl, BaseModel, BeforeValidator, Field

Currency = Annotated[
    str,
    BeforeValidator(lambda value: str(value).strip().upper()),
    Field(pattern=r"^[A-Z]{3}$"),
]
PlanCode = Annotated[
    str,
    BeforeValidator(lambda value: str(value).strip().lower()),
    Field(min_length=2, max_length=64, pattern=r"^[a-z0-9]+(?:[-_][a-z0-9]+)*$"),
]
IdempotencyKey = Annotated[
    str,
    Field(
        min_length=8,
        max_length=255,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    ),
]


class BillingPlanResponse(BaseModel):
    id: str
    code: str
    name: str
    status: Literal["active", "archived"]
    currency: str
    monthly_amount_minor: int
    included_seconds: int
    overage_amount_micros_per_second: int
    stripe_price_id: str | None = None
    stripe_meter_event_name: str | None = None
    entitlements: dict[str, Any]
    created_at: datetime | None = None
    updated_at: datetime | None = None


class SubscriptionResponse(BaseModel):
    id: str
    organization_id: str
    plan: BillingPlanResponse
    provider: str
    status: str
    current_period_start: datetime | None = None
    current_period_end: datetime | None = None
    trial_end: datetime | None = None
    cancel_at_period_end: bool
    last_invoice_status: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class UsageTotal(BaseModel):
    event_type: str
    unit: str
    quantity: int
    provider_cost_micros: int
    customer_charge_micros: int


class BillingOverviewResponse(BaseModel):
    organization_id: str
    subscription: SubscriptionResponse | None = None
    period_start: datetime
    period_end: datetime
    usage: list[UsageTotal]
    provider_cost_micros: int
    customer_charge_micros: int


class CheckoutSessionRequest(BaseModel):
    plan_code: PlanCode
    success_url: AnyHttpUrl
    cancel_url: AnyHttpUrl
    idempotency_key: IdempotencyKey


class PortalSessionRequest(BaseModel):
    return_url: AnyHttpUrl


class HostedSessionResponse(BaseModel):
    id: str
    url: str
    expires_at: datetime | None = None


class UsageEventResponse(BaseModel):
    id: str
    organization_id: str
    call_id: str | None = None
    event_type: str
    quantity: int
    unit: str
    provider_cost_micros: int
    customer_charge_micros: int
    currency: str
    source: str
    idempotency_key: str
    provider_reference: str | None = None
    reversal_of_event_id: str | None = None
    metadata: dict[str, Any] | None = None
    occurred_at: datetime
    recorded_at: datetime | None = None


class UsagePageInfo(BaseModel):
    next_cursor: str | None = None
    has_more: bool = False


class UsageEventPage(BaseModel):
    items: list[UsageEventResponse]
    page: UsagePageInfo


class UpdateSpendLimitRequest(BaseModel):
    # null disables the limit. One currency unit is the smallest configured cap.
    monthly_limit_micros: int | None = Field(ge=1_000_000)
    hard_limit: bool = True
    warning_threshold_percent: int = Field(default=80, ge=1, le=100)


class SpendLimitResponse(BaseModel):
    organization_id: str
    monthly_limit_micros: int | None = None
    hard_limit: bool
    warning_threshold_percent: int
    period_start: datetime
    period_end: datetime
    spent_micros: int
    percent_used: float | None = None
    warning: bool
    blocked: bool
    blocked_at: datetime | None = None
    updated_by_user_id: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
