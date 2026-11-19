"""Redaction for promotion: the observe producer's Redactor plus six extra secret classes (12 in total).

The six built-in patterns are reused from modules/memory-backends/observe.py by path, never copied. The extras are the
classes the operator's own redaction tool covered beyond them.
"""

import re
from pathlib import Path

import importlib.util
import sys

HERE = Path(__file__).resolve().parent


def _load(key, path):
    if key not in sys.modules:
        spec = importlib.util.spec_from_file_location(key, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[key] = module
        spec.loader.exec_module(module)
    return sys.modules[key]


observe = _load("memops_observe", HERE.parent / "memory-backends" / "observe.py")

BUILTIN_NAMES = ("private-key", "sk-key", "github-token", "slack-token", "aws-key", "password")  # order of observe's list
EXTRA = {
    "jwt": r"\beyJ[A-Za-z0-9_\-]{5,}\.[A-Za-z0-9_\-]{5,}\.[A-Za-z0-9_\-]{5,}",
    "slack-webhook": r"https://hooks\.slack\.com/services/[A-Za-z0-9/_\-]+",
    "bearer": r"(?i)\bbearer\s+[A-Za-z0-9._~+/=\-]{8,}",
    "credential-url": r"\b[A-Za-z][A-Za-z0-9+.\-]*://[^\s/:@]+:[^\s/@]+@",
    "github_pat": r"\bgithub_pat_[A-Za-z0-9_]{20,}",
    "mysql-p": r"(?i)\bmysql\w*\b[^\n]*?\s-p\S+",
}
CLASSES = dict(zip(BUILTIN_NAMES, observe.BUILTIN_REDACTIONS), **EXTRA)


def redact(text):
    """Return (redacted text, number of replacements). Runs before anything is written."""
    redactor = observe.Redactor(extra=[re.compile(p) for p in EXTRA.values()])
    return redactor(text), redactor.count
