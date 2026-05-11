"""Circuit breaker for OpenAI API outages.

Why this exists: when the user's OpenAI quota runs out, every API call
returns 429 with `insufficient_quota`. The OpenAI client transparently
retries 3× with backoff before raising, which means each transcription
attempt burns ~3.5s × 3 models = ~10s before we even reach the local
fallback. Plus the cleanup step also retries. Net cost per dictation:
~20s of pointless waiting instead of ~6s straight to the local model.

This module tracks the breaker state. When we observe a quota/billing
error, we trip the breaker — for a short cooldown period, callers should
skip API calls entirely and go directly to the local model (or skip
optional steps like cleanup).

The breaker auto-resets after _COOLDOWN_SEC. We don't try to be clever
about probing for recovery — at worst the user gets one slow dictation
when the cooldown expires, then we trip again if quota is still exhausted.
"""

import logging
import threading
import time
from typing import Optional

log = logging.getLogger(__name__)

# Cooldown after a 429/insufficient_quota error. Long enough to cover
# the typical case (user ran out of credit and needs to top up) without
# being so long that a transient rate-limit blocks the API for the rest
# of the session. 5 min ≈ user notices the notification, opens the
# dashboard, sees the issue.
_COOLDOWN_SEC = 300.0

# Error fingerprints that should trip the breaker. Anything else (network
# blip, 500, transient timeout) we let the OpenAI client retry naturally
# — those are recoverable in seconds.
_BREAKER_KEYWORDS = (
    "insufficient_quota",
    "exceeded your current quota",
    "billing",
)

_lock = threading.Lock()
_tripped_until: float = 0.0
_last_reason: Optional[str] = None
_notify_callback = None  # set by app.py to surface a user-visible alert


def set_notify_callback(cb) -> None:
    """app.py registers a one-shot user notification (rumps notify)."""
    global _notify_callback
    _notify_callback = cb


def is_tripped() -> bool:
    """Return True iff API calls should be skipped right now."""
    with _lock:
        if _tripped_until == 0.0:
            return False
        if time.time() >= _tripped_until:
            # Auto-recover. Caller will hit the API; if it still fails
            # we'll just trip again.
            return False
        return True


def time_remaining() -> float:
    """Seconds until the breaker auto-resets (0 if not tripped)."""
    with _lock:
        if _tripped_until == 0.0:
            return 0.0
        return max(0.0, _tripped_until - time.time())


def trip(error: BaseException) -> bool:
    """If `error` looks like a quota/billing outage, trip the breaker.

    Returns True if the breaker was tripped (or was already tripped).
    Idempotent: tripping while already tripped just resets the cooldown.
    """
    msg = str(error).lower()
    if not any(kw in msg for kw in _BREAKER_KEYWORDS):
        return False

    global _tripped_until, _last_reason
    with _lock:
        was_tripped_already = _tripped_until > time.time()
        _tripped_until = time.time() + _COOLDOWN_SEC
        _last_reason = str(error)[:200]

    if not was_tripped_already:
        log.warning(
            "OpenAI API circuit breaker TRIPPED (cooldown=%.0fs). Reason: %s",
            _COOLDOWN_SEC, _last_reason,
        )
        # Notify user once per trip event (not on every renewal)
        cb = _notify_callback
        if cb is not None:
            try:
                cb(_last_reason)
            except Exception as e:
                log.debug("notify_callback raised: %s", e)
    return True


def reset() -> None:
    """Force the breaker open (for tests, or a manual 'try again' menu)."""
    global _tripped_until, _last_reason
    with _lock:
        _tripped_until = 0.0
        _last_reason = None
    log.info("API circuit breaker reset")


def last_reason() -> Optional[str]:
    with _lock:
        return _last_reason
