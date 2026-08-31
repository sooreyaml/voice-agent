from __future__ import annotations

from app.domains.auth.exceptions import APIError


class InvitationInvalid(APIError):
    status_code = 400
    code = "invitation_invalid"

    def __init__(self, message: str = "This invitation is invalid or has expired.") -> None:
        super().__init__(message)


class InvitationEmailMismatch(APIError):
    status_code = 403
    code = "invitation_email_mismatch"

    def __init__(self) -> None:
        super().__init__(
            "This invitation was sent to a different email address. Sign in with "
            "that address to accept it."
        )


class AlreadyMember(APIError):
    status_code = 409
    code = "already_member"

    def __init__(self) -> None:
        super().__init__("That person is already in this organization.")


class LastOwner(APIError):
    status_code = 409
    code = "last_owner"

    def __init__(self, action: str) -> None:
        super().__init__(
            f"You cannot {action} the last owner. Promote another owner first."
        )


class CannotChangeOwnRole(APIError):
    status_code = 409
    code = "cannot_change_own_role"

    def __init__(self) -> None:
        super().__init__("You cannot change your own role.")


class RoleTooHigh(APIError):
    status_code = 403
    code = "role_too_high"

    def __init__(self) -> None:
        super().__init__("You cannot grant a role higher than your own.")


class MemberNotFound(APIError):
    status_code = 404
    code = "member_not_found"

    def __init__(self) -> None:
        super().__init__("That person is not a member of this organization.")
