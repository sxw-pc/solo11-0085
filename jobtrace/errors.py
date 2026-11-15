"""Domain errors for the offline job metadata collector."""


class JobTraceError(Exception):
    """Base class carrying a stable, user-facing error code."""

    code = "jobtrace_error"

    def __init__(self, message: str, *, details: dict | None = None):
        super().__init__(message)
        self.message = message
        self.details = details or {}


class ConfigError(JobTraceError):
    code = "config_error"


class UnsafeURLError(JobTraceError):
    code = "unsafe_url"


class FetchError(JobTraceError):
    code = "fetch_error"

    def __init__(self, message: str, *, url: str = "", status: int | None = None,
                 retryable: bool = False, attempts: int = 0):
        super().__init__(message, details={"url": url, "status": status})
        self.url = url
        self.status = status
        self.retryable = retryable
        self.attempts = attempts


class ParseError(JobTraceError):
    code = "parse_error"


class PaginationError(JobTraceError):
    code = "pagination_error"


class PaginationLoopError(PaginationError):
    code = "pagination_loop"


class PaginationLimitError(PaginationError):
    code = "pagination_limit"


class StorageError(JobTraceError):
    code = "storage_error"


class RawStoreError(JobTraceError):
    code = "rawstore_error"
