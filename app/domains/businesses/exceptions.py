from __future__ import annotations

from app.domains.auth.exceptions import APIError

from .constants import ErrorCode


class AgentNotProvisioned(APIError):
    """The organization has no business profile or phone number yet."""

    status_code = 409
    code = ErrorCode.AGENT_NOT_PROVISIONED

    def __init__(self) -> None:
        super().__init__(
            "Your organisation is still waiting for a phone number, so there is "
            "no agent to edit yet."
        )


class AgentProvisioningFailed(APIError):
    status_code = 503
    code = ErrorCode.AGENT_PROVISIONING_FAILED

    def __init__(self) -> None:
        super().__init__(
            "We couldn't assign a phone number right now. Please try again shortly."
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
