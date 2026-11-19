"""Slug helper. Intentionally buggy fixture: repeated spaces become repeated dashes."""


def slugify(text):
    return text.lower().replace(" ", "-")
