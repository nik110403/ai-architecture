#!/usr/bin/env python3
"""Targeted offline checks; simple scalar frontmatter only, not full YAML.

Path checks reject static escapes/redirects, not concurrent filesystem races.
No network, harness activation, or arbitrary Markdown/code interpretation.
"""

import json
import re
import stat
import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
SKILLS = set(
    "planning execution delegation context continuity review evaluation workspace design research safety maintenance routing convergence lifecycle discovery operations specialist-journeys".split()
)
ROLES = set(
    "builder verifier refuter researcher security-reviewer design-critic".split()
)
COMMANDS = set("plan execute review checkpoint research health".split())
ROOT_DOCS = set(
    "README.md INSTALL.md CATALOG.md COVERAGE.md CONTRIBUTING.md CHANGELOG.md AGENTS.md LICENSE LICENSE-docs LICENSES.md CITATION.cff CAPABILITIES.md WORKFLOWS.md .markdownlint.yaml assets/banner-light.svg assets/banner-dark.svg assets/social-preview.png tools/banner/bin/make-banner tools/banner/fonts/OFL.txt".split()
)
TEMPLATES = set(
    "plan.md work-item.md resume.md worker-brief.md worker-result.md review.md measurement.md maintenance.md route-report.md lifecycle-record.md panel-review.md".split()
)
NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
LINK = re.compile(r"\[[^\]\n]*\]\(([^\s)]+)\)")
PLUGIN_REF = re.compile(r"\$\{CLAUDE_PLUGIN_ROOT\}/([^\s`\"';)]+)")
LOCAL = re.compile(
    r"(?:\.?\.?/)?(?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+\.(?:md|py|json|txt|csv|yaml|yml|toml|sh|svg|png)\Z"
)
ROOT_DIRS = {
    "skills",
    "agents",
    "commands",
    "scripts",
    "tests",
    "examples",
    "templates",
    "adapters",
    "rules",
    "assets",
    "tools",
    ".claude-plugin",
}


def fail(message):
    raise ValueError(message)


def safe(path, directory=False):
    if not path.is_relative_to(ROOT):
        fail(f"escaping path: {path}")
    for part in (path, *path.parents):
        if part == ROOT.parent:
            break
        if part.is_symlink():
            fail(f"symlink: {part}")
    if not path.resolve().is_relative_to(ROOT):
        fail(f"escaping resolved path: {path}")
    mode = path.stat().st_mode
    if not (stat.S_ISDIR(mode) if directory else stat.S_ISREG(mode)):
        fail(f"not a {'directory' if directory else 'regular file'}: {path}")
    return path


def reference(base, raw, directory=False):
    parsed = urlsplit(raw)
    if parsed.scheme in ("http", "https", "mailto") or raw.startswith("#"):
        return
    if parsed.scheme or parsed.netloc:
        fail(f"unsupported local reference: {raw}")
    value = unquote(parsed.path)
    if not value or "\\" in value or Path(value).is_absolute():
        fail(f"invalid local reference: {raw}")
    # Normalize legitimate ../ links, but do not allow an escape or a symlink hop.
    path = base
    for part in Path(value).parts:
        if part == "..":
            path = path.parent
        elif part != ".":
            path = path / part
        if not path.is_relative_to(ROOT) or path.is_symlink():
            fail(f"escaping/redirected reference: {raw}")
    return safe(path, directory=directory)


def frontmatter(path, named=True):
    text = safe(path).read_text(encoding="utf-8")
    if not text.startswith("---\n") or "\n---\n" not in text[4:]:
        fail(f"missing/unterminated frontmatter: {path}")
    header, body = text[4:].split("\n---\n", 1)
    values = {}
    for line in header.splitlines():
        match = re.fullmatch(r"([a-z][a-z-]*): (\S.*)", line)
        if not match:
            fail(f"unsupported/malformed scalar frontmatter: {path}")
        key, value = match.groups()
        if key in values:
            fail(f"duplicate frontmatter field {key}: {path}")
        if value[0] in "\"'":
            quote = value[0]
            if (
                len(value) < 2
                or value[-1] != quote
                or quote in value[1:-1]
                or "\\" in value
            ):
                fail(f"unsupported quoted scalar: {path}")
            value = value[1:-1]
        elif value[0] in "[{|>&*!" or ": " in value or " #" in value:
            fail(f"unsupported YAML syntax: {path}")
        if not value.strip():
            fail(f"empty frontmatter field {key}: {path}")
        values[key] = value
    allowed = {"name", "description", "tools"} if named else {"description"}
    required = {"name", "description"} if named else {"description"}
    if not required <= values.keys() or values.keys() - allowed:
        fail(f"frontmatter fields mismatch: {path}")
    if len(values["description"]) > 1024:
        fail(f"description too long: {path}")
    if named:
        expected = path.parent.name if path.name == "SKILL.md" else path.stem
        name = values["name"]
        if len(name) > 64 or not NAME.fullmatch(name) or name != expected:
            fail(f"invalid/mismatched name: {path}")
    return body


def unique_keys(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            fail(f"duplicate JSON key: {key}")
        out[key] = value
    return out


def manifests():
    def load(name):
        return json.loads(
            safe(ROOT / ".claude-plugin" / name).read_text(encoding="utf-8"),
            object_pairs_hook=unique_keys,
        )

    def attribution(value):
        if value != {"name": "Nik", "url": "https://github.com/nik110403"}:
            fail("invalid owner/author attribution")

    plugin, market = load("plugin.json"), load("marketplace.json")
    if not isinstance(plugin, dict) or set(plugin) != {
        "name",
        "version",
        "description",
        "author",
    }:
        fail("plugin schema fields mismatch")
    if plugin["name"] != "ai-architecture" or plugin["version"] != "0.3.1-preview":
        fail("plugin name/version mismatch")
    if (
        not isinstance(plugin["description"], str)
        or not 1 <= len(plugin["description"].strip()) <= 1024
    ):
        fail("invalid plugin description")
    attribution(plugin["author"])
    if (
        not isinstance(market, dict)
        or set(market) != {"name", "owner", "plugins", "metadata"}
        or market["name"] != plugin["name"]
    ):
        fail("marketplace schema fields mismatch")
    attribution(market["owner"])
    metadata = market["metadata"]
    if not isinstance(metadata, dict) or set(metadata) != {"description", "version"}:
        fail("marketplace metadata fields mismatch")
    if (
        metadata["version"] != plugin["version"]
        or not isinstance(metadata["description"], str)
        or not metadata["description"].strip()
    ):
        fail("marketplace metadata version/description mismatch")
    if not isinstance(market["plugins"], list) or len(market["plugins"]) != 1:
        fail("marketplace plugin inventory mismatch")
    entry = market["plugins"][0]
    if not isinstance(entry, dict) or set(entry) != {"name", "version", "source"}:
        fail("marketplace entry fields mismatch")
    if (
        entry["name"] != plugin["name"]
        or entry["version"] != plugin["version"]
        or entry["source"] != "./"
    ):
        fail("marketplace source/name/version mismatch")
    reference(ROOT, entry["source"], directory=True)


def dependencies(path):
    text = path.read_text(encoding="utf-8")
    if path.parent == ROOT / "commands" and re.search(
        r"(?<![/A-Za-z0-9_}])(?:skills|scripts|tests|assets|templates)/[A-Za-z0-9_.\-/]+",
        text,
    ):
        fail(f"unbound/unnamespaced command dependency: {path}")
    for raw in LINK.findall(text):
        reference(path.parent, raw, directory=raw.split("#", 1)[0].endswith("/"))
    for token in re.findall(r"`([^`\n]+)`", text):
        refs = PLUGIN_REF.findall(token)
        if path.parent == ROOT / "commands" and token.split("/", 1)[0] in ROOT_DIRS:
            fail(f"unbound/unnamespaced command dependency: {path}")
        for raw in refs:
            reference(ROOT, raw, directory=raw in ROOT_DIRS or raw.endswith("/"))
        if (
            refs
            or any(c in token for c in " <>${}")
            or token.startswith(("http:", "https:", "/ai-architecture:"))
        ):
            continue  # Generic placeholders, shell arguments and external references.
        if (
            token.rstrip("/") in ROOT_DIRS
            or token.endswith("/")
            and token.split("/", 1)[0] in ROOT_DIRS
        ):
            reference(ROOT, token, directory=True)
        elif LOCAL.fullmatch(token):
            base = (
                ROOT
                if token.split("/", 1)[0] in ROOT_DIRS
                and not (path.parent / token).exists()
                else path.parent
            )
            reference(base, token)


def main():
    safe(ROOT, directory=True)
    for path in ROOT.rglob("*"):
        if path.is_symlink():
            fail(f"symlink in collection: {path}")
        safe(path, directory=path.is_dir())
    for name in ROOT_DOCS:
        safe(ROOT / name)
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    if "assets/banner-light.svg" not in "\n".join(readme.splitlines()[:15]):
        fail("README banner missing from first 15 lines")
    if re.search(r"sanitized|T[0-9] release|candidate|NOT PUBLISHED|staged|/home/", readme, re.I):
        fail("README contains internal status wording")
    if not (ROOT / "LICENSE").read_text(encoding="utf-8").startswith("MIT License\n"):
        fail("LICENSE must start with 'MIT License'")
    if {p.name for p in (ROOT / "skills").iterdir()} != SKILLS:
        fail("skill inventory mismatch")
    for name in SKILLS:
        body = frontmatter(ROOT / "skills" / name / "SKILL.md")
        for heading in (
            "Objective",
            "Steps",
            "Output and done check",
            "Failure handling",
            "Worked example",
        ):
            if f"## {heading}\n" not in body:
                fail(f"incomplete skill: {name}")
    if {p.name for p in (ROOT / "templates").iterdir()} != TEMPLATES:
        fail("template inventory mismatch")
    for directory, expected in (("agents", ROLES), ("commands", COMMANDS)):
        if {p.name for p in (ROOT / directory).iterdir()} != {
            f"{n}.md" for n in expected
        }:
            fail(f"{directory} inventory mismatch")
        for name in expected:
            path = ROOT / directory / f"{name}.md"
            body = frontmatter(path, named=directory == "agents")
            if directory == "commands" and (
                "${CLAUDE_PLUGIN_ROOT}/" not in body
                or f"/ai-architecture:{name}" not in body
            ):
                fail(f"unbound/unnamespaced command: {name}")
    catalog = safe(ROOT / "CATALOG.md").read_text(encoding="utf-8")
    entries = LINK.findall(catalog)
    expected = (
        {f"skills/{n}/SKILL.md" for n in SKILLS}
        | {f"agents/{n}.md" for n in ROLES}
        | {f"commands/{n}.md" for n in COMMANDS}
    )
    actual = [
        e
        for e in entries
        if e.endswith("/SKILL.md") or e.startswith(("agents/", "commands/"))
    ]
    if set(actual) != expected or len(actual) != len(expected):
        fail("catalog inventory mismatch")
    for label, target in re.findall(r"\[([^\]\n]+)\]\(([^\s)]+)\)", catalog):
        if target in expected:
            name = (
                Path(target).parent.name
                if target.endswith("/SKILL.md")
                else Path(target).stem
            )
            if label != name:
                fail(f"catalog name mismatch: {label} -> {target}")
    for path in ROOT.rglob("*.md"):
        dependencies(path)
    manifests()
    print(
        "PASS: 18 skills, 6 roles, 6 commands; catalog, scalar metadata, local dependencies, manifests and static path checks"
    )


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, TypeError, KeyError) as error:
        print(f"FAIL: {error}", file=sys.stderr)
        sys.exit(1)
