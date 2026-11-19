"""Metrics helpers. Intentionally disagrees with the glossary."""

ACTIVE_WINDOW_DAYS = 90


def is_active(days_since_login):
    return days_since_login <= ACTIVE_WINDOW_DAYS
