"""Offline behavioral checks; every project and recipient is temporary."""

import importlib.util
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch


SCRIPT = Path(__file__).with_name("context_packet.py")
HEADER = "## Task routing\n| Task | Room | Read first | Do not load | Capabilities | Output |\n|---|---|---|---|---|---|\n"
ROW = "| Build | Tools | `a.txt`, missing.txt | archive/old.txt | `lint`; AGENT: checker, agent: reviewer | report |\n"


class PacketTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "a.txt").write_bytes(b"123456789")

    def run_cli(self, text=HEADER + ROW, *args, script=SCRIPT):
        if text is not None:
            (self.root / "CLAUDE.md").write_text(text, encoding="utf-8")
        return subprocess.run(
            [
                sys.executable,
                "-B",
                str(script),
                "--project",
                str(self.root),
                "--task",
                "Build",
                *args,
            ],
            capture_output=True,
            text=True,
            cwd=self.root,
        )

    def packet(self, result):
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def rejected(self, result):
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertNotIn("Traceback", result.stderr)

    def test_exact_selection_lists_sizes_stamp_and_determinism(self):
        stamp = 'quote"\\\nline'
        first = self.run_cli(HEADER + ROW, "--stamp", stamp, "--room", "ignored")
        packet = self.packet(first)
        self.assertEqual(packet["schema_version"], 1)
        self.assertEqual(packet["disposition"], "ROUTED")
        self.assertEqual(packet["room"], "Tools")
        self.assertEqual(packet["read"], ["a.txt", "missing.txt"])
        self.assertEqual(packet["exclude"], ["archive/old.txt"])
        self.assertEqual(packet["skills"], ["lint"])
        self.assertEqual(packet["agent"], ["checker", "reviewer"])
        self.assertEqual(packet["missing_paths"], ["missing.txt", "archive/old.txt"])
        self.assertEqual(packet["estimated_tokens"], 2)
        self.assertEqual(packet["budget_tokens"], 4000)
        self.assertIsNone(packet["expected_turns"])
        self.assertEqual(packet["provenance"]["date"], stamp)
        self.assertNotIn("output", packet)
        self.assertFalse(packet["over_budget"])
        self.assertEqual(
            first.stdout,
            self.run_cli(HEADER + ROW, "--stamp", stamp, "--room", "ignored").stdout,
        )

    def test_room_fallback_and_first_match(self):
        text = HEADER + ROW + ROW.replace("a.txt", "second.txt")
        self.assertEqual(self.packet(self.run_cli(text))["read"][0], "a.txt")
        packet = self.packet(self.run_cli(text, "--task", "Unknown", "--room", "Tools"))
        self.assertEqual(packet["task"], "Unknown")
        self.assertEqual(packet["read"][0], "a.txt")
        # An exact task later in the table wins over an earlier matching room.
        text += ROW.replace("Build", "Later").replace("a.txt", "later.txt")
        self.assertEqual(
            self.packet(self.run_cli(text, "--task", "Later", "--room", "Tools"))[
                "read"
            ][0],
            "later.txt",
        )

    def test_degraded_cases(self):
        for text in (None, "# Project\n", "## Task routing\n", HEADER):
            with self.subTest(text=text):
                (self.root / "CLAUDE.md").unlink(missing_ok=True)
                packet = self.packet(self.run_cli(text))
                self.assertEqual(packet["disposition"], "KERNEL_ONLY")
                self.assertEqual(packet["read"], ["CLAUDE.md"])
                self.assertIn("no task routing", packet["note"])

    def test_fenced_examples_are_not_routes(self):
        for fence in ("```md", "~~~md", "   ````md"):
            close = fence.strip().rstrip("md")
            example = fence + "\n" + HEADER + ROW + close + "\n"
            with self.subTest(fence=fence):
                self.assertEqual(
                    self.packet(self.run_cli(example))["disposition"], "KERNEL_ONLY"
                )
                actual = HEADER + ROW.replace("Tools", "Actual")
                self.assertEqual(
                    self.packet(self.run_cli(example + actual))["room"], "Actual"
                )

    def test_crlf_routing(self):
        (self.root / "CLAUDE.md").write_bytes(
            (HEADER + ROW).replace("\n", "\r\n").encode()
        )
        self.assertEqual(self.packet(self.run_cli(None))["room"], "Tools")

    def test_directory_shaped_targets(self):
        for target in (
            "a.txt/",
            "missing/",
            "a.txt/.",
            "a.txt\\",
            "C:/a.txt",
            "C:a.txt",
            "//host/share",
        ):
            with self.subTest(target=target):
                self.rejected(self.run_cli(HEADER + ROW.replace("a.txt", target)))

    def test_missing_descriptor_flags_fallback_packet_and_read_bounds(self):
        spec = importlib.util.spec_from_file_location("packet", SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        (self.root / "CLAUDE.md").write_text(HEADER + ROW)
        before = {
            p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.root.iterdir()
        }
        expected = self.run_cli(None).stdout
        import builtins

        real_open = builtins.open
        real_lstat = module.os.lstat
        for flag in ("O_DIRECTORY", "O_NOFOLLOW", "O_NONBLOCK"):
            opened, inspected = [], []

            def checked_open(path, *args, **kwargs):
                opened.append(str(path))
                return real_open(path, *args, **kwargs)

            def checked_lstat(path, *args, **kwargs):
                inspected.append(str(path))
                return real_lstat(path, *args, **kwargs)

            with (
                patch.dict(module.os.__dict__),
                patch("builtins.open", side_effect=checked_open),
                patch.object(module.os, "lstat", side_effect=checked_lstat),
                redirect_stdout(io.StringIO()) as out,
                redirect_stderr(io.StringIO()) as err,
            ):
                delattr(module.os, flag)
                self.assertEqual(
                    module.main(["--project", str(self.root), "--task", "Build"]),
                    0,
                    err.getvalue(),
                )
            self.assertEqual(out.getvalue(), expected)
            self.assertEqual(opened, [str(self.root / "CLAUDE.md")])
            self.assertTrue(all(Path(p).is_relative_to(self.root) for p in inspected))
        self.assertEqual(
            before,
            {
                p.name: (p.read_bytes(), p.stat().st_mtime_ns)
                for p in self.root.iterdir()
            },
        )

    def test_fallback_rejects_links_and_non_directory_without_descending(self):
        spec = importlib.util.spec_from_file_location("packet", SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        (self.root / "link").symlink_to(self.root.parent, target_is_directory=True)
        real_lstat = module.os.lstat
        for target in (
            "link/outside.txt",
            "a.txt/child",
            "../outside",
            "C:/outside",
            "a.txt/",
        ):
            (self.root / "CLAUDE.md").write_text(HEADER + ROW.replace("a.txt", target))
            seen = []

            def checked_lstat(path):
                seen.append(str(path))
                return real_lstat(path)

            with (
                patch.dict(module.os.__dict__),
                patch.object(module.os, "lstat", side_effect=checked_lstat),
                redirect_stdout(io.StringIO()) as out,
                redirect_stderr(io.StringIO()) as err,
            ):
                del module.os.O_DIRECTORY
                self.assertEqual(
                    module.main(["--project", str(self.root), "--task", "Build"]), 2
                )
            self.assertEqual(out.getvalue(), "")
            self.assertNotIn("Traceback", err.getvalue())
            self.assertTrue(all(Path(p).is_relative_to(self.root) for p in seen))
            self.assertFalse(any(p.endswith(("outside.txt", "child")) for p in seen))

    def test_fallback_missing_function_is_controlled(self):
        spec = importlib.util.spec_from_file_location("packet", SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        for missing in (True, False):
            with (
                patch.dict(module.os.__dict__),
                redirect_stdout(io.StringIO()) as out,
                redirect_stderr(io.StringIO()) as err,
            ):
                del module.os.O_DIRECTORY
                if missing:
                    del module.os.lstat
                else:
                    module.os.lstat = lambda path: (_ for _ in ()).throw(
                        NotImplementedError("lstat unavailable")
                    )
                self.assertEqual(
                    module.main(["--project", str(self.root), "--task", "Build"]), 2
                )
            self.assertEqual(out.getvalue(), "")
            self.assertNotIn("Traceback", err.getvalue())
            self.assertIn("lstat", err.getvalue())

    def test_warn_only_budget_and_sum_before_division(self):
        (self.root / "b.txt").write_bytes(b"123")
        text = HEADER + ROW.replace("missing.txt", "b.txt")
        result = self.run_cli(text, "--budget", "0")
        packet = self.packet(result)
        self.assertEqual(packet["estimated_tokens"], 3)
        self.assertTrue(packet["over_budget"])
        self.assertIn("WARN:", result.stderr)
        self.assertEqual(len(packet["read"]), 2)

    def test_unknown_case_sensitive_and_invalid_cli(self):
        for args in (
            ("--task", "build"),
            ("--room", "No", "--task", "No"),
            ("--task", "  "),
            ("--budget", "-1"),
            ("--budget", "1.2"),
        ):
            with self.subTest(args=args):
                self.rejected(self.run_cli(HEADER + ROW, *args))

    def test_malformed_tables(self):
        for text in (
            HEADER.replace("Read first", "Read") + ROW,
            HEADER.replace("|---|---|---|---|---|---|", "|--|---|---|---|---|---|")
            + ROW,
            HEADER + ROW.replace("report", "a|b"),
            HEADER + ROW.replace("report", r"a\|b"),
            HEADER + ROW.replace("report", "`a|b`"),
            HEADER + ROW.replace("`a.txt`", "`a.txt"),
            HEADER + ROW.rstrip("\n|") + "\n",
        ):
            with self.subTest(text=text):
                self.rejected(self.run_cli(text))

    def test_unsafe_read_and_exclude_paths(self):
        for target in (
            "../outside",
            "a/../outside",
            "/etc/passwd",
            r"C:\outside",
            r"a\b",
        ):
            for old in ("a.txt", "archive/old.txt"):
                with self.subTest(target=target, column=old):
                    self.rejected(self.run_cli(HEADER + ROW.replace(old, target)))

    def test_missing_nested_read_and_no_stamp(self):
        packet = self.packet(
            self.run_cli(HEADER + ROW.replace("a.txt", "absent/nested.txt"))
        )
        self.assertIn("absent/nested.txt", packet["missing_paths"])
        self.assertEqual(packet["estimated_tokens"], 0)
        self.assertIsNone(packet["provenance"]["date"])

    def test_symlink_leaf_ancestor_and_directories(self):
        (self.root / "link").symlink_to(self.root / "a.txt")
        (self.root / "ancestor").symlink_to(self.root, target_is_directory=True)
        (self.root / "folder").mkdir()
        for target in ("link", "ancestor/a.txt", "folder"):
            with self.subTest(target=target):
                self.rejected(self.run_cli(HEADER + ROW.replace("a.txt", target)))
        (self.root / "CLAUDE.md").unlink()
        (self.root / "CLAUDE.md").symlink_to(self.root / "a.txt")
        self.rejected(self.run_cli(None))

    def test_utf8_and_nonregular_instruction(self):
        (self.root / "CLAUDE.md").write_bytes(b"\xff")
        self.rejected(self.run_cli(None))
        (self.root / "CLAUDE.md").unlink()
        (self.root / "CLAUDE.md").mkdir()
        self.rejected(self.run_cli(None))

    def test_no_target_body_reads_outside_stat_or_mutation(self):
        spec = importlib.util.spec_from_file_location("packet", SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        (self.root / "CLAUDE.md").write_text(HEADER + ROW)
        before = {
            p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.root.iterdir()
        }
        real_open = module.os.open
        opened = []

        def checked_open(path, flags, *args, **kwargs):
            opened.append(str(path))
            return real_open(path, flags, *args, **kwargs)

        with (
            patch.object(module.os, "open", side_effect=checked_open),
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(
                module.main(["--project", str(self.root), "--task", "Build"]), 0
            )
        self.assertNotIn("a.txt", opened)
        self.assertNotIn("ROOM.md", opened)
        self.assertEqual(
            before,
            {
                p.name: (p.read_bytes(), p.stat().st_mtime_ns)
                for p in self.root.iterdir()
            },
        )
        real_stat = module.os.stat
        seen = []

        def checked_stat(path, *args, **kwargs):
            seen.append(str(path))
            return real_stat(path, *args, **kwargs)

        (self.root / "CLAUDE.md").write_text(
            HEADER + ROW.replace("a.txt", "../outside")
        )
        with (
            patch.object(module.os, "stat", side_effect=checked_stat),
            redirect_stdout(io.StringIO()),
            redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(
                module.main(["--project", str(self.root), "--task", "Build"]), 2
            )
        self.assertNotIn("../outside", seen)
        (self.root / "redirect").symlink_to(self.root, target_is_directory=True)
        (self.root / "CLAUDE.md").write_text(
            HEADER + ROW.replace("a.txt", "redirect/a.txt")
        )
        seen.clear()
        with (
            patch.object(module.os, "stat", side_effect=checked_stat),
            redirect_stdout(io.StringIO()),
            redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(
                module.main(["--project", str(self.root), "--task", "Build"]), 2
            )
        self.assertIn("redirect", seen)
        self.assertNotIn("a.txt", seen)  # Never descend through the link.

    def test_access_error_controlled(self):
        spec = importlib.util.spec_from_file_location("packet", SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with (
            patch.object(module.os, "open", side_effect=PermissionError("denied")),
            redirect_stdout(io.StringIO()) as out,
            redirect_stderr(io.StringIO()) as err,
        ):
            self.assertEqual(
                module.main(["--project", str(self.root), "--task", "Build"]), 2
            )
        self.assertEqual(out.getvalue(), "")
        self.assertIn("denied", err.getvalue())

    def test_copied_minimal_bundle(self):
        recipient = self.root / "recipient"
        recipient.mkdir()
        bundle = recipient / "context"
        shutil.copytree(SCRIPT.parent.parent, bundle)
        fixture = bundle / "fixtures/routed"
        shutil.copyfile(fixture / "CLAUDE.md", recipient / "CLAUDE.md")
        shutil.copyfile(fixture / "input.txt", recipient / "input.txt")
        result = subprocess.run(
            [
                sys.executable,
                "-B",
                str(bundle / "scripts/context_packet.py"),
                "--project",
                ".",
                "--task",
                "Inspect input",
            ],
            cwd=recipient,
            env={},
            capture_output=True,
            text=True,
        )
        packet = self.packet(result)
        self.assertEqual(packet["room"], "Inspection")
        self.assertEqual(packet["project"], str(recipient))
        self.assertEqual(packet["read"], ["input.txt"])
        self.assertEqual(packet["agent"], ["verifier"])

    def test_exact_minimal_copy_without_environment_changes(self):
        recipient = self.root / "minimal"
        scripts = recipient / "scripts"
        scripts.mkdir(parents=True)
        shutil.copyfile(SCRIPT, scripts / SCRIPT.name)
        shutil.copyfile(
            SCRIPT.parent.parent / "context-packet.md", recipient / "context-packet.md"
        )
        fixture = SCRIPT.parent.parent / "fixtures/routed"
        for name in ("CLAUDE.md", "input.txt"):
            shutil.copyfile(fixture / name, recipient / name)
        result = subprocess.run(
            [
                sys.executable,
                "-B",
                str(scripts / SCRIPT.name),
                "--project",
                str(recipient),
                "--task",
                "Inspect input",
            ],
            cwd=recipient,
            capture_output=True,
            text=True,
        )
        packet = self.packet(result)
        self.assertEqual(packet["read"], ["input.txt"])
        self.assertEqual(packet["room"], "Inspection")
        self.assertEqual(
            packet["estimated_tokens"], (recipient / "input.txt").stat().st_size // 4
        )


if __name__ == "__main__":
    unittest.main()
