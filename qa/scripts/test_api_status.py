"""TC_API_STATUS_* — circuit breaker for OpenAI API outages.

Background: when the user's quota runs out, every API call returns 429
and the OpenAI client retries 3× with backoff. Without a breaker, each
dictation burns ~20s before falling back to local. The breaker tracks
'API is down' state and short-circuits future calls for a cooldown
period.
"""
import time
from _harness import case, run_all

import api_status


def _fresh():
    """Reset module state between tests."""
    api_status.reset()
    api_status.set_notify_callback(None)


@case("TC_API_STATUS_INITIAL", "api_status",
      "fresh module: breaker is not tripped, time_remaining=0")
def test_initial_state():
    _fresh()
    assert not api_status.is_tripped()
    assert api_status.time_remaining() == 0.0
    assert api_status.last_reason() is None


@case("TC_API_STATUS_TRIP_QUOTA", "api_status",
      "insufficient_quota error trips the breaker")
def test_trip_quota():
    _fresh()
    err = Exception(
        "Error code: 429 - You exceeded your current quota, "
        "please check your plan and billing details. type=insufficient_quota"
    )
    tripped = api_status.trip(err)
    assert tripped is True
    assert api_status.is_tripped()
    assert api_status.time_remaining() > 0
    assert "insufficient_quota" in api_status.last_reason()


@case("TC_API_STATUS_TRIP_BILLING", "api_status",
      "billing-related error also trips the breaker")
def test_trip_billing():
    _fresh()
    err = Exception("billing details required")
    assert api_status.trip(err) is True
    assert api_status.is_tripped()


@case("TC_API_STATUS_NO_TRIP_NETWORK", "api_status",
      "transient network errors do NOT trip the breaker (those should retry naturally)")
def test_no_trip_network():
    _fresh()
    err = Exception("Connection reset by peer")
    assert api_status.trip(err) is False
    assert not api_status.is_tripped()


@case("TC_API_STATUS_NO_TRIP_500", "api_status",
      "5xx server errors do NOT trip (transient — let OpenAI retry)")
def test_no_trip_500():
    _fresh()
    err = Exception("Error code: 500 - internal server error")
    assert api_status.trip(err) is False
    assert not api_status.is_tripped()


@case("TC_API_STATUS_NOTIFY_CALLED", "api_status",
      "registered callback is invoked once when breaker first trips")
def test_notify_called():
    _fresh()
    calls = []
    api_status.set_notify_callback(lambda reason: calls.append(reason))

    api_status.trip(Exception("insufficient_quota"))
    assert len(calls) == 1
    assert "insufficient_quota" in calls[0]

    # Trip again while already tripped — should NOT re-notify
    api_status.trip(Exception("insufficient_quota again"))
    assert len(calls) == 1, f"notify should be once per trip event, got {len(calls)}"


@case("TC_API_STATUS_RESET", "api_status",
      "reset() clears breaker state")
def test_reset():
    _fresh()
    api_status.trip(Exception("insufficient_quota"))
    assert api_status.is_tripped()

    api_status.reset()
    assert not api_status.is_tripped()
    assert api_status.last_reason() is None


@case("TC_API_STATUS_AUTO_RECOVER", "api_status",
      "breaker auto-recovers after cooldown expires")
def test_auto_recover():
    _fresh()
    # Manually expire the breaker by reaching into module state
    api_status.trip(Exception("insufficient_quota"))
    assert api_status.is_tripped()
    # Force cooldown to be in the past
    api_status._tripped_until = time.time() - 1.0
    assert not api_status.is_tripped(), "expired breaker should report not-tripped"


@case("TC_API_STATUS_NOTIFY_REQUIRES_NEW_TRIP", "api_status",
      "after auto-recovery, a new trip event re-notifies")
def test_renotify_after_recovery():
    _fresh()
    calls = []
    api_status.set_notify_callback(lambda reason: calls.append(reason))

    api_status.trip(Exception("insufficient_quota"))
    assert len(calls) == 1

    # Simulate cooldown expiry
    api_status._tripped_until = time.time() - 1.0
    assert not api_status.is_tripped()

    # New failure → new notification
    api_status.trip(Exception("insufficient_quota again"))
    assert len(calls) == 2


if __name__ == "__main__":
    run_all("test_api_status")
