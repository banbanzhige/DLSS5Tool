"""User queue range. Older native hosts still negotiate at most three slots."""
MAX_IN_FLIGHT = 16
LEGACY_MAX_IN_FLIGHT = 3


def clamp_in_flight(value, default=2):
    try:
        return max(1, min(MAX_IN_FLIGHT, int(value)))
    except (TypeError, ValueError, OverflowError):
        return default
