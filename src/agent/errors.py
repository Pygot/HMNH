# src/agent/errors.py
class AgentError(Exception):
    """Base class for every error the agent raises on purpose."""

    pass


class ConfigError(AgentError):
    """Raised when configuration or settings are missing or invalid."""

    pass


class NotConfigured(ConfigError):
    """Raised when a feature needs a setting or credential that is not set."""

    pass


class ComplianceRefusal(AgentError):
    """Raised when a request is refused by the compliance rules."""

    pass


class InvalidRequest(AgentError):
    """Raised when user supplied input is invalid or cannot be acted on.

    The message is written for the person who made the request.
    """

    pass


class VoiceDisabled(AgentError):
    """Raised when voice features are used without a configured voice key."""

    pass


class ForbiddenHostError(AgentError):
    """Raised when an outgoing request targets a host that must not be called."""

    pass


class UpstreamError(AgentError):
    """Base class for failures reported by an external service."""

    pass


class AuthenticationError(UpstreamError):
    """Raised when an external service rejects the supplied credentials."""

    pass


class RateLimitedError(UpstreamError):
    """Raised when an external service answers that its rate limit was reached."""

    def __init__(self, message: str, retry_after: float | None = None):
        """Initialize the error with a message and an optional wait time.

        Args:
            message: Description of the rate limit failure.
            retry_after: Seconds the service asked to wait before retrying, if given.
        """
        super().__init__(message)
        self.retry_after = retry_after


class SourceUnavailable(AgentError):
    """Raised when a page or profile cannot be read or is not allowed to be read."""

    pass


class LLMOutputError(AgentError):
    """Raised when a language model reply is empty, cut off or unusable."""

    pass


class CVParseError(AgentError):
    """Raised when an uploaded CV is rejected or contains no readable text."""

    pass
