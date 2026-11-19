"""Small text helpers that already exist in the project."""


def collapse_ws(text):
    return " ".join(text.split())


def title_case(text):
    return " ".join(word.capitalize() for word in text.split())
