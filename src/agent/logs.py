# src/agent/logs.py
from collections.abc import Iterable
from agent.config import LogTuning

import logging


class RedactingFilter(logging.Filter):
    """A logging filter that masks known secret values in log messages.

    Secrets shorter than the minimum length are ignored, so short common strings are
    not masked by accident. The filter never drops a record.
    """

    def __init__(self, secrets: Iterable[str], min_length: int):
        """Initialize the filter with the secrets to hide.

        Args:
            secrets: secret strings that must not appear in logs.
            min_length: minimum length for a secret to be redacted.
        """
        super().__init__()
        self._secrets = [secret for secret in secrets if len(secret) >= min_length]

    def filter(self, record: logging.LogRecord) -> bool:
        """Replace secret values in the record's message with asterisks.

        The record is changed in place: its message is formatted and redacted, and its
        format arguments are cleared.

        Args:
            record: the log record being handled.

        Returns:
            Always True, so the record is kept.
        """
        message = record.getMessage()
        for secret in self._secrets:
            message = message.replace(secret, "***")
        record.msg = message
        # Clear the arguments so the already redacted message is not formatted a second time.
        record.args = None
        return True


def configure_logging(secrets: Iterable[str], tuning: LogTuning) -> None:
    """Set up root logging with a stream handler that redacts secrets.

    Any existing root handlers are replaced. Loggers listed as noisy are limited to
    warnings.

    Args:
        secrets: secret strings that must not appear in logs.
        tuning: log format, level, minimum secret length and noisy logger names.
    """
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter(tuning.format))
    # The filter sits on the handler, not on a logger, so records from every logger are redacted.
    handler.addFilter(RedactingFilter(secrets, tuning.min_secret_length))
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(tuning.level)
    for name in tuning.noisy_loggers:
        logging.getLogger(name).setLevel(logging.WARNING)
