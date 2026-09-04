from __future__ import annotations

# Organization lifecycle states an owner passes through before the number is
# live. ``registered`` -> verify email -> ``profile_pending`` -> complete the
# business profile -> ``eligible`` (billing on, awaiting checkout) -> ``active``.
GATED_LIFECYCLES = frozenset({"registered", "profile_pending", "eligible"})

# States the onboarding activation step is willing to act from. ``registered``
# is included for deployments that do not require email verification (the owner
# never leaves ``registered``); the email gate is still enforced separately when
# the flag is on. ``provisioning`` covers legacy instant-signup accounts that
# never got a number.
ACTIVATABLE_LIFECYCLES = frozenset(
    {"registered", "profile_pending", "eligible", "provisioning"}
)


class ErrorCode:
    EMAIL_NOT_VERIFIED = "email_not_verified"
    BUSINESS_PROFILE_INCOMPLETE = "business_profile_incomplete"
    ONBOARDING_ALREADY_ACTIVE = "onboarding_already_active"
    NUMBER_PROVISIONING_FAILED = "number_provisioning_failed"
