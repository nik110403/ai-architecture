"""Reproduction command: prints the symptom while the bug exists."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import slug  # noqa: E402

print(slug.slugify("Hello   World"))
