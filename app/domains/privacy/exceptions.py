from app.domains.auth.exceptions import APIError

from .constants import ErrorCode


class DataRequestNotFound(APIError):
    status_code = 404
    code = ErrorCode.DATA_REQUEST_NOT_FOUND

    def __init__(self) -> None:
        super().__init__("We couldn't find that data request.")


class DataRequestConflict(APIError):
    status_code = 409
    code = ErrorCode.DATA_REQUEST_CONFLICT

    def __init__(self) -> None:
        super().__init__("A deletion request is already pending for this organization.")


class DataRequestNotCancellable(APIError):
    status_code = 409
    code = ErrorCode.DATA_REQUEST_NOT_CANCELLABLE

    def __init__(self) -> None:
        super().__init__("Only a pending data request can be cancelled.")


class DeletionConfirmationMismatch(APIError):
    status_code = 422
    code = ErrorCode.DELETION_CONFIRMATION_MISMATCH

    def __init__(self) -> None:
        super().__init__(
            "Type the organization's current slug exactly to schedule deletion.",
            field_errors={"confirm_organization_slug": "Does not match."},
        )
