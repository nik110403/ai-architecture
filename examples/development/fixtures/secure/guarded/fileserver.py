"""File server fixture: serves files below a base directory, with one independent guard per threat."""
import os


class Refused(Exception):
    """A guard refused the request."""


def has_symlink(base_dir, parts):
    current = base_dir
    for part in parts:
        current = os.path.join(current, part)
        if os.path.islink(current):
            return True
    return False


def read_file(base_dir, relative):
    parts = relative.split("/")
    if os.path.isabs(relative): raise Refused("absolute path")  # GUARD absolute
    if ".." in parts: raise Refused("traversal")  # GUARD traversal
    if has_symlink(base_dir, parts): raise Refused("symlink")  # GUARD symlink-escape
    with open(os.path.join(base_dir, relative), "rb") as handle:
        return handle.read()
