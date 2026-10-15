"""Fixture task: parse a nonempty comma-separated list of signed integers."""

import re


def parse_numbers(text):
    if not isinstance(text, str):
        raise TypeError("input must be text")
    if not text.strip():
        raise ValueError("input must not be empty")
    parts = text.split(",")
    if any(not re.fullmatch(r"[+-]?[0-9]+", part.strip()) for part in parts):
        raise ValueError("expected comma-separated integers")
    return [int(part.strip()) for part in parts]
