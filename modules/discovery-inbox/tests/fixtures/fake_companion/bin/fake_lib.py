"""Authored stdlib stand-in for a discovery companion: one record per candidate, status in a small enum."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPOS = ROOT / "data" / "repos"
STATUSES = ["untriaged", "next", "integrated", "harvested", "deferred", "skip"]


def repo_path(repo):
    return REPOS / (repo.replace("/", "__") + ".md")


def load(path):
    text = Path(path).read_text()
    head, _, body = text.partition("\n---\n")
    meta = {}
    for line in head.splitlines()[1:]:
        key, _, value = line.partition(": ")
        meta[key.strip()] = value.strip().strip('"')
    return meta, body


def set_status(path, value):
    text = Path(path).read_text()
    Path(path).write_text(re.sub(r'(?m)^status: .*$', 'status: "%s"' % value, text, count=1))


def all_repos():
    return [(p, *load(p)) for p in sorted(REPOS.glob("*.md"))]
