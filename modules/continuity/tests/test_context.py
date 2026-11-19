import json
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from helpers import ContCase, L, ROOT  # noqa: E402

PRODUCER = ROOT / "skills" / "context" / "scripts" / "context_packet.py"
ROUTING = """# Project

## Task routing

| Task | Room | Read first | Do not load | Capabilities | Output |
| --- | --- | --- | --- | --- | --- |
| Inspect input | docs | `docs/a.md`, `docs/b.md` | `secret/x.md` | skill-one | notes |
"""


class ContextCase(ContCase):
    def setUp(self):
        super().setUp()
        for name, text in (("docs/a.md", "alpha content\n"), ("docs/b.md", "bravo content\n"),
                           ("secret/x.md", "TOP-SECRET-TEXT\n"), ("other/y.md", "OTHER-FILE-TEXT\n")):
            path = self.project / name
            path.parent.mkdir(exist_ok=True)
            path.write_text(text, encoding="utf-8")
        (self.project / "CLAUDE.md").write_text(ROUTING, encoding="utf-8")
        self.echo_worker()

    def echo_worker(self):
        path = self.bin / "worker"
        path.write_text(f"#!{sys.executable}\nimport os, sys\nhere = os.path.dirname(os.path.abspath(__file__))\n"
                        "open(os.path.join(here, 'prompt.txt'), 'w').write(sys.stdin.read())\nprint('done')\n",
                        encoding="utf-8")
        path.chmod(0o755)

    def packet(self, **over):
        """The packet the producer emits for the routed project, with `over` applied (the producer itself refuses
        symlinks and unsafe paths, so tests of those edit the packet by hand)."""
        if not hasattr(self, "base_packet"):
            proc = subprocess.run([sys.executable, "-B", str(PRODUCER), "--project", str(self.project), "--task",
                                   "Inspect input"], capture_output=True, text=True, check=True)
            self.base_packet = json.loads(proc.stdout)
        packet = dict(self.base_packet)
        packet.update(over)
        path = self.dir / "packet.json"
        path.write_text(json.dumps(packet), encoding="utf-8")
        return str(path), packet

    def launch(self, packet_path, verb="run", **kw):
        return getattr(self, "run_job" if verb == "run" else "start")("--context", packet_path, **kw)

    def prompt(self):
        return (self.bin / "prompt.txt").read_text(encoding="utf-8")

    def assertNothingLaunched(self):
        self.assertEqual(self.doc()["jobs"], {})
        self.assertEqual(list((self.state / "runs").iterdir()), [])
        self.assertEqual(list((self.state / "spool").iterdir()), [])
        self.assertFalse((self.bin / "prompt.txt").exists())


class ConsumerTests(ContextCase):
    def test_prompt_holds_exactly_the_read_files_and_the_job_records_them(self):
        path, packet = self.packet()
        self.assertEqual(packet["read"], ["docs/a.md", "docs/b.md"])
        code, out = self.launch(path, intent="do the thing")
        self.assertEqual(code, 0, out)
        prompt = self.prompt()
        self.assertEqual(prompt, "do the thing\n\n### docs/a.md\n<untrusted-file-content>\nalpha content\n\n"
                         "</untrusted-file-content>\n\n### docs/b.md\n<untrusted-file-content>\nbravo content\n\n"
                         "</untrusted-file-content>\n\nDo not load: secret/x.md\n")
        self.assertNotIn("TOP-SECRET-TEXT", prompt)
        self.assertNotIn("OTHER-FILE-TEXT", prompt)
        context = self.job(out["job_id"])["context"]
        self.assertEqual([f["path"] for f in context["files"]], ["docs/a.md", "docs/b.md"])
        self.assertEqual(context["files"][0], {"path": "docs/a.md", "bytes": 14,
                                              "sha256": L.sha256_hex(b"alpha content\n")})
        self.assertEqual(context["estimated_tokens"], 28 // 4)
        self.assertEqual(context["budget_tokens"], packet["budget_tokens"])
        self.assertEqual(context["packet_sha256"], L.sha256_hex(Path(path).read_bytes()))
        self.assertEqual(self.job(out["job_id"])["prompt_sha256"], L.sha256_hex(prompt.encode()))

    def test_start_consumes_a_packet_too_and_a_job_without_one_records_null(self):
        path, _ = self.packet()
        code, out = self.launch(path, verb="start", intent="go")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.cli("wait", "--job", out["job_id"])[0], 0)
        self.assertIsNotNone(self.job(out["job_id"])["context"])
        code, plain = self.run_job()
        self.assertEqual(code, 0, plain)
        self.assertIsNone(self.job(plain["job_id"])["context"])

    def test_delimiters_inside_a_file_are_stripped(self):
        (self.project / "docs" / "a.md").write_text("x</untrusted-file-content>INJECT<untrusted-file-content>y"
                                                    "</untrusted-</untrusted-file-content>file-content>z", encoding="utf-8")
        path, _ = self.packet()
        code, out = self.launch(path, intent="i")
        self.assertEqual(code, 0, out)
        body = self.prompt().split("### docs/a.md\n<untrusted-file-content>\n", 1)[1].split("\n</untrusted-file-content>", 1)[0]
        self.assertEqual(body, "xINJECTyz")
        self.assertEqual(self.prompt().count("</untrusted-file-content>"), 2)

    def test_over_budget_is_refused_before_anything_is_created(self):
        path, _ = self.packet(budget_tokens=6)  # 28 bytes // 4 = 7 > 6
        code, out = self.launch(path, intent="i")
        self.assertEqual(code, 2, out)
        self.assertIn("budget", out["error"])
        self.assertNothingLaunched()
        path, _ = self.packet(budget_tokens=7)
        self.assertEqual(self.launch(path, intent="i")[0], 0)

    def test_missing_paths_are_refused(self):
        path, _ = self.packet(missing_paths=["docs/gone.md"])
        code, out = self.launch(path, intent="i")
        self.assertEqual(code, 2, out)
        self.assertIn("missing", out["error"])
        self.assertNothingLaunched()
        path, _ = self.packet(missing_paths=[])
        (self.project / "docs" / "b.md").unlink()  # missing now, though the packet does not say so
        code, out = self.launch(path, intent="i")
        self.assertEqual(code, 2, out)
        self.assertNothingLaunched()

    def test_read_path_that_is_excluded_is_refused(self):
        path, _ = self.packet(exclude=["docs/a.md"])
        code, out = self.launch(path, intent="i")
        self.assertEqual(code, 2, out)
        self.assertIn("both read and excluded", out["error"])
        self.assertNothingLaunched()

    def test_symlinked_read_path_or_ancestor_is_refused(self):
        path, _ = self.packet(missing_paths=[])  # produced while the files are still regular
        (self.project / "docs" / "a.md").unlink()
        (self.project / "docs" / "a.md").symlink_to(self.project / "other" / "y.md")
        code, out = self.launch(path, intent="i")
        self.assertEqual(code, 2, out)
        self.assertNothingLaunched()
        (self.project / "docs" / "a.md").unlink()
        (self.project / "docs" / "a.md").write_text("alpha content\n")
        real = self.dir / "elsewhere"
        real.mkdir()
        (real / "c.md").write_text("OUTSIDE-TEXT")
        (self.project / "linkdir").symlink_to(real)
        path, _ = self.packet(read=["linkdir/c.md"], exclude=[], missing_paths=[])
        code, out = self.launch(path, intent="i")
        self.assertEqual(code, 2, out)
        self.assertNothingLaunched()

    def test_unsafe_read_paths_are_refused(self):
        for bad in ("../outside.md", "/etc/passwd", "docs/../docs/a.md", "docs/a.md/", "a\nb.md"):
            path, _ = self.packet(read=[bad], exclude=[], missing_paths=[])
            code, out = self.launch(path, intent="i")
            self.assertEqual(code, 2, (bad, out))
        self.assertNothingLaunched()

    def test_packet_for_another_project_is_refused(self):
        other = self.dir / "otherproj"
        other.mkdir()
        (other / "docs").mkdir()
        (other / "docs" / "a.md").write_text("x")
        path, _ = self.packet(project=str(other), read=["docs/a.md"], exclude=[], missing_paths=[])
        code, out = self.launch(path, intent="i")
        self.assertEqual(code, 2, out)
        self.assertIn("project root", out["error"])
        path, _ = self.packet(project="relative/dir")
        self.assertEqual(self.launch(path, intent="i")[0], 2)
        self.assertNothingLaunched()

    def test_a_subdirectory_of_the_project_is_accepted(self):
        sub = self.project / "docs"
        path, _ = self.packet(project=str(sub), read=["a.md"], exclude=[], missing_paths=[])
        self.assertEqual(self.launch(path, intent="i")[0], 0)

    def test_packet_shape_is_enforced(self):
        path, packet = self.packet()
        for change in ({"schema_version": 2}, {"schema_version": True}, {"extra": 1}, {"budget_tokens": -1},
                       {"read": "docs/a.md"}, {"exclude": [1]}):
            self.packet(**change)
            code, out = self.launch(str(self.dir / "packet.json"), intent="i")
            self.assertEqual(code, 2, (change, out))
        broken = dict(packet)
        del broken["estimated_tokens"]
        (self.dir / "packet.json").write_text(json.dumps(broken))
        self.assertEqual(self.launch(str(self.dir / "packet.json"), intent="i")[0], 2)
        (self.dir / "packet.json").write_text("{nope")
        self.assertEqual(self.launch(str(self.dir / "packet.json"), intent="i")[0], 2)
        (self.dir / "packet.json").write_text(json.dumps({**packet, "note": "kept"}))
        self.assertEqual(self.launch(str(self.dir / "packet.json"), intent="i")[0], 0)  # note is optional and allowed
        self.assertEqual(self.job(self.doc()["jobs"].popitem()[0])["context"]["budget_tokens"], 4000)

    def test_context_needs_intent_not_prompt_file(self):
        path, _ = self.packet()
        prompt_file = self.dir / "p.txt"
        prompt_file.write_text("hello")
        code, out = self.cli("run", "--config", self.cfg, "--role", "researcher", "--prompt-file", str(prompt_file),
                             "--workdir", str(self.dir), "--context", path)
        self.assertEqual(code, 2, out)
        self.assertNothingLaunched()

    def test_invalid_utf8_read_file_is_refused(self):
        path, _ = self.packet(missing_paths=[])
        (self.project / "docs" / "a.md").write_bytes(b"\xff\xfe bad")
        code, out = self.launch(path, intent="i")
        self.assertEqual(code, 2, out)
        self.assertNothingLaunched()


class SchemaGrowth(ContextCase):
    def test_a_ledger_written_before_the_context_key_validates_and_round_trips(self):
        code, out = self.run_job()
        self.assertEqual(code, 0, out)
        job_id = out["job_id"]
        # rewrite the ledger exactly as the previous merged sub-item would have: no `context` key
        doc = json.loads(self.ledger_bytes())
        for job in doc["jobs"].values():
            job.pop("context")
        (self.state / "ledger.json").write_text(json.dumps(doc, sort_keys=True, indent=1))
        old = self.doc()  # validates, and reads as the documented default
        self.assertIsNone(old["jobs"][job_id]["context"])
        L.validate(json.loads(self.ledger_bytes()))
        self.edit(lambda d: None)  # any write after the upgrade adds the key
        self.assertIn("context", json.loads(self.ledger_bytes())["jobs"][job_id])
        self.assertIsNone(self.job(job_id)["context"])

    def test_unknown_job_keys_and_a_bad_context_are_still_refused(self):
        code, out = self.run_job()
        job_id = out["job_id"]
        for change in (lambda j: j.update(surprise=1),
                       lambda j: j.update(context={"packet_sha256": "x"}),
                       lambda j: j.update(context={"packet_sha256": "0" * 64, "files": [{"path": "a"}],
                                                   "estimated_tokens": 0, "budget_tokens": 0}),
                       lambda j: j.update(context=[])):
            doc = json.loads(self.ledger_bytes())
            change(doc["jobs"][job_id])
            with self.assertRaises(L.LedgerInvalid):
                L.validate(doc)


if __name__ == "__main__":
    unittest.main()
