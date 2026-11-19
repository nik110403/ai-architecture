import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import slug  # noqa: E402


class TestSlug(unittest.TestCase):
    def test_collapses_whitespace(self):
        self.assertEqual(slug.slugify("Hello   World"), "hello-world")


if __name__ == "__main__":
    unittest.main()
