from __future__ import annotations

from app.domains.auth.exceptions import APIError

from .constants import ErrorCode


class AgentNotProvisioned(APIError):
    """The organization has no business profile yet (no pool number was ever
    assigned), so there is nothing to edit."""

    status_code = 409
    code = ErrorCode.AGENT_NOT_PROVISIONED

    def __init__(self) -> None:
        super().__init__(
            "Your organisation is still waiting for a phone number, so there is "
            "no agent to edit yet."
        )


class AgentLocked(APIError):
    """The organization's lifecycle does not allow agent changes."""

    status_code = 409
    code = ErrorCode.AGENT_LOCKED

    def __init__(self) -> None:
        super().__init__(
            "Agent changes are paused while the account is suspended or closed."
        )


class AgentDraftNotFound(APIError):
    status_code = 404
    code = ErrorCode.AGENT_DRAFT_NOT_FOUND

    def __init__(self) -> None:
        super().__init__("There is no unpublished draft to work with.")
