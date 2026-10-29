"""Exception logging helpers that keep credentials and request headers out of logs."""
import os
import re
import traceback


_HEADER_LINE = re.compile(
    r"(?im)^.*\b(?:authorization|proxy-authorization|cookie|set-cookie|x-api-key|headers?)\b.*$"
)
_BEARER = re.compile(r"(?i)\bBearer\s+[^\s,;\"']+")
_KEY_ASSIGNMENT = re.compile(
    r"(?i)\b(?:api[_-]?key|access[_-]?token|secret)\b\s*[:=]\s*[^\s,;\"']+"
)


def _redact_exception_text(text):
    text = _HEADER_LINE.sub("[request header details redacted]", text)
    text = _BEARER.sub("Bearer [REDACTED]", text)
    text = _KEY_ASSIGNMENT.sub("credential=[REDACTED]", text)
    for name, value in os.environ.items():
        if value and any(word in name.upper() for word in ("API_KEY", "TOKEN", "SECRET")):
            text = text.replace(value, "[REDACTED]")
    return text


def log_exception(logger, context, exc):
    """Log traceback and exception details after removing credentials and headers."""
    details = "".join(traceback.TracebackException.from_exception(
        exc, capture_locals=False).format())
    logger.error("%s\n%s", context, _redact_exception_text(details))
