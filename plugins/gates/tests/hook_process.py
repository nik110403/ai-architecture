"""Test-only hook invocation: a crashed hook is never a silent allow."""

import subprocess


def run(*args, **kwargs):
    return subprocess.run(*args, check=True, **kwargs)
