"""File server fixture: serves files below a base directory. Intentionally unguarded; never import it."""
import os


def read_file(base_dir, relative):
    with open(os.path.join(base_dir, relative), "rb") as handle:
        return handle.read()
