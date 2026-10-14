# Run locally

Requires Python 3.11 or later and no third-party packages.

```text
python3 examples/continuity/run.py
python3 scripts/package_preview.py
python3 scripts/package_preview.py --check dist/ai-architecture-preview.zip
```

For a release review, scan both this directory and a freshly extracted archive with the parent repository's `scripts/release_check.py`. The scanner is deliberately not shipped in this preview.
