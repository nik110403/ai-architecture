"""Slug helper."""
from textutil import collapse_ws


def slugify(text):
    return collapse_ws(text).lower().replace(" ", "-")
