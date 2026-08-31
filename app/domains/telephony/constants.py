NANP_COUNTRIES = {"CA", "US"}
SUPPORTED_NUMBER_TYPES = {"local", "mobile", "national", "toll_free"}
SHARED_TRUNK_NAME = "OpenAI Realtime Agent"


class ErrorCode:
    IDEMPOTENCY_CONFLICT = "telephony_idempotency_conflict"
    NUMBER_NOT_SELECTED = "telephony_number_not_selected"
    PHONE_NUMBER_CONFLICT = "telephony_phone_number_conflict"
    PROFILE_REQUIRED = "telephony_profile_required"
    PROVIDER_NOT_CONFIGURED = "telephony_provider_not_configured"
    PROVIDER_UNAVAILABLE = "telephony_provider_unavailable"
    PROVISIONING_NOT_FOUND = "telephony_provisioning_not_found"
    PROVISIONING_NOT_READY = "telephony_provisioning_not_ready"
    TEST_CALL_NOT_FOUND = "telephony_test_call_not_found"
