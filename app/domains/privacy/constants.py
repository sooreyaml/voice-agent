from datetime import timedelta

DEFAULT_RETENTION_DAYS = 90
DELETION_GRACE_PERIOD = timedelta(days=7)
EXPORT_TTL = timedelta(hours=24)
JOB_LOCK_TIMEOUT = timedelta(minutes=10)


class ErrorCode:
    DATA_REQUEST_NOT_FOUND = "data_request_not_found"
    DATA_REQUEST_CONFLICT = "data_request_conflict"
    DATA_REQUEST_NOT_CANCELLABLE = "data_request_not_cancellable"
    DELETION_CONFIRMATION_MISMATCH = "deletion_confirmation_mismatch"
