import datetime
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

MOD = Path(__file__).resolve().parents[1]
ROOT = MOD.parents[1]
CLI = MOD / "maintenance.py"
FIX = MOD / "tests" / "fixtures"
LOOPSYNC_FIX = ROOT / "tools" / "loop-sync" / "tests" / "fixtures"

FAKE_SYSTEMCTL = """#!/bin/bash
echo "systemctl $*" >> "$FAKE_LOG"
[ "$1" = "--user" ] && shift
case "$1" in
  show-environment|daemon-reload|enable|list-timers) exit 0 ;;
  show)
    unit="${@: -1}"
    case "$unit" in
      demo-ok.*)   printf 'LoadState=loaded\\nActiveState=active\\nUnitFileState=enabled\\n' ;;
      demo-idle.*) printf 'LoadState=loaded\\nActiveState=inactive\\nUnitFileState=disabled\\n' ;;
      *)           printf 'LoadState=not-found\\nActiveState=inactive\\nUnitFileState=\\n' ;;
    esac ;;
esac
"""
FAKE_CRONTAB = """#!/bin/bash
echo "crontab $*" >> "$FAKE_LOG"
[ "$1" = "-l" ] && exit 1
exit 0
"""
SAFE_TOOLS = ("git", "bash", "sh", "env", "cat", "ls", "rm", "mkdir", "sed", "grep", "awk", "dirname",
              "basename", "true", "false", "tee", "date", "hostname", "head", "tail", "sort", "mktemp", "chmod")


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def listing(root):
    return sorted((p.relative_to(root).as_posix(), p.is_dir() or sha(p)) for p in Path(root).rglob("*"))


def load_module():
    spec = importlib.util.spec_from_file_location("maintenance_under_test", CLI)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name).resolve()
        self.log = self.tmp / "fake.log"
        self.log.write_text("")
        self.reg = self.tmp / "loops.json"
        shutil.copy(FIX / "loops.json", self.reg)

    def path_with(self, *fakes):
        """PATH built from scratch: a temp bin with fakes plus a few safe tools only."""
        bindir = self.tmp / ("bin%d" % len(list(self.tmp.glob("bin*"))))
        bindir.mkdir()
        for tool in SAFE_TOOLS:
            found = shutil.which(tool)
            if found:
                (bindir / tool).symlink_to(found)
        for name in fakes:
            body = {"systemctl": FAKE_SYSTEMCTL, "crontab": FAKE_CRONTAB}[name]
            (bindir / name).write_text(body)
            (bindir / name).chmod(0o755)
        found = shutil.which("systemctl", path=str(bindir))
        self.assertTrue(found is None or found == str(bindir / "systemctl"))
        found = shutil.which("crontab", path=str(bindir))
        self.assertTrue(found is None or found == str(bindir / "crontab"))
        return str(bindir)

    def env(self, path):
        home = self.tmp / "home"
        home.mkdir(exist_ok=True)
        env = {
            "PATH": path, "HOME": str(home), "XDG_CONFIG_HOME": str(home / "c"),
            "XDG_STATE_HOME": str(home / "s"), "XDG_RUNTIME_DIR": str(self.tmp / "run"),
            "FAKE_LOG": str(self.log), "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": str(self.tmp / "gitconfig"),
            "GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@example.invalid",
            "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@example.invalid",
        }
        (self.tmp / "run").mkdir(exist_ok=True)
        (self.tmp / "gitconfig").write_text("")
        return env

    def run_cli(self, *args, path=None, env_extra=None):
        env = self.env(path if path is not None else self.path_with())
        env.update(env_extra or {})
        return subprocess.run([sys.executable, "-B", str(CLI), *map(str, args)], capture_output=True,
                              text=True, cwd=self.tmp, env=env, timeout=60)

    def verbs(self):
        out = []
        for line in self.log.read_text().splitlines():
            parts = line.split()
            if parts[0] == "systemctl":
                rest = [p for p in parts[1:] if p != "--user"]
                out.append(rest[0])
            else:
                out.append(("crontab", tuple(parts[1:])))
        return out


class TestA1Registry(Base):
    def test_validate_accepts_fixture(self):
        self.assertEqual(self.run_cli("validate", "--registry", self.reg).returncode, 0)

    def test_validate_rejects(self):
        base = json.loads(self.reg.read_text())
        cases = {}
        dup = self.tmp / "dup.json"
        dup.write_text('{"schema_version": 1, "schema_version": 1, "loops": []}')
        cases["dup"] = dup
        bad_kind = json.loads(json.dumps(base))
        bad_kind["loops"][0]["checks"][0]["kind"] = "cronjob"
        bad_status = json.loads(json.dumps(base))
        bad_status["loops"][0]["status"] = "maybe"
        for name, data in (("kind", bad_kind), ("status", bad_status)):
            p = self.tmp / (name + ".json")
            p.write_text(json.dumps(data))
            cases[name] = p
        for name, p in cases.items():
            r = self.run_cli("validate", "--registry", p)
            self.assertEqual(r.returncode, 2, (name, r.stdout, r.stderr))

    def test_unit_targets_are_validated_and_never_reach_systemctl(self):
        base = json.loads(self.reg.read_text())
        for bad in ("--host=evil@example.invalid", "-Hx", "-H evil.service", "a b.timer", "x/../y.service", "plain"):
            for kind in ("timer", "service"):
                data = json.loads(json.dumps(base))
                data["loops"][0]["checks"][0] = {"kind": kind, "target": bad}
                p = self.tmp / "inj.json"
                p.write_text(json.dumps(data))
                for verb in ("validate", "health"):
                    r = self.run_cli(verb, "--registry", p, path=self.path_with("systemctl"))
                    self.assertEqual(r.returncode, 2, (bad, kind, verb, r.stdout, r.stderr))
        self.assertNotIn("evil", self.log.read_text())
        self.assertNotIn("-Hx", self.log.read_text())

    def test_import_rejects_option_like_unit(self):
        md = self.tmp / "inj.md"
        md.write_text("| ID | STATUS | Hosts | Check |\n|---|---|---|---|\n"
                      "| `x` | live | any | `timer:--host=evil@example.invalid` |\n")
        r = self.run_cli("import", "--from", md)
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)

    def test_unit_names_follow_double_dash(self):
        p = self.path_with("systemctl")
        self.run_cli("health", "--registry", self.reg, path=p)
        shows = [x for x in self.log.read_text().splitlines() if " show " in x and "-p" in x]
        self.assertTrue(shows)
        for line in shows:
            self.assertRegex(line, r" -- \S+$")

    def test_manifest_names_are_validated(self):
        t = self.tmp / "T"
        t.mkdir()
        victim = self.tmp / "victim.timer"
        victim.write_text("x")
        for bad in ("../victim.timer", "--host=evil.timer", "sub/x.timer", "x.txt"):
            (t / ".aiarch-manifest.json").write_text(json.dumps(
                {"schema_version": 1, "adapter": "systemd-user", "files": {bad: sha(victim)}}))
            for verb in ("report", "rollback"):
                r = self.run_cli("scheduler", verb, "--unit-dir", t, path=self.path_with("systemctl"))
                self.assertEqual(r.returncode, 2, (bad, verb, r.stdout, r.stderr))
            self.assertTrue(victim.exists())
        self.assertNotIn("evil", self.log.read_text())

    def test_health_without_scheduler(self):
        r = self.run_cli("health", "--registry", self.reg, "--format", "json", path=self.path_with())
        self.assertEqual(r.returncode, 0, r.stderr)
        data = json.loads(r.stdout)
        by_id = {loop["id"]: loop for loop in data["loops"]}
        for loop in ("demo-ok", "demo-idle"):
            for check in by_id[loop]["checks"]:
                self.assertEqual(check["result"], "not-observable: no systemd user manager")
        self.assertEqual(by_id["demo-old"]["status"], "retired")
        self.assertFalse(by_id["demo-old"]["violation"])
        self.assertEqual(data["violations"], 0)

    def test_dead_loop_is_violation(self):
        r = self.run_cli("health", "--registry", FIX / "loops-dead.json", "--format", "json")
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertTrue(json.loads(r.stdout)["loops"][0]["violation"])

    def test_health_with_fake_systemctl(self):
        p = self.path_with("systemctl")
        r = self.run_cli("health", "--registry", self.reg, "--format", "json", path=p)
        self.assertEqual(r.returncode, 0, r.stderr)
        by_id = {loop["id"]: loop for loop in json.loads(r.stdout)["loops"]}
        self.assertEqual(by_id["demo-ok"]["checks"][0]["result"], "ok")
        self.assertEqual(by_id["demo-idle"]["checks"][0]["result"], "inactive")
        r = self.run_cli("health", "--registry", FIX / "loops-missing.json", "--format", "json", path=p)
        self.assertEqual(r.returncode, 1)
        self.assertEqual(json.loads(r.stdout)["loops"][0]["checks"][0]["result"], "missing")
        self.assertTrue(set(self.verbs()) <= {"show-environment", "show", "list-timers"})

    def test_import_from_loops_md(self):
        out = self.tmp / "imported.json"
        r = self.run_cli("import", "--from", LOOPSYNC_FIX / "loops-wiring.md", "--out", out)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.run_cli("validate", "--registry", out).returncode, 0)
        data = json.loads(out.read_text())
        ids = {loop["id"]: loop for loop in data["loops"]}
        self.assertEqual(ids["ok-timer"]["checks"], [{"kind": "timer", "target": "demo-ok.timer"}])
        self.assertEqual(ids["host-scoped"]["hosts"], ["other-host"])
        dead = self.tmp / "dead.json"
        self.run_cli("import", "--from", LOOPSYNC_FIX / "loops-dead.md", "--out", dead)
        self.assertEqual(json.loads(dead.read_text())["loops"][0]["status"], "dead")


class TestA2Scheduler(Base):
    def args(self, unit_dir, *extra, adapter="systemd-user"):
        return ["scheduler", "{}", "--registry", self.reg, "--loop", "demo-ok", "--adapter", adapter,
                "--unit-dir", unit_dir, "--calendar", "daily", "--exec", "/bin/true", *extra]

    def sched(self, verb, unit_dir, *extra, path, adapter="systemd-user"):
        a = [x if x != "{}" else verb for x in self.args(unit_dir, *extra, adapter=adapter)]
        return self.run_cli(*a, path=path)

    def test_plan_writes_nothing(self):
        t = self.tmp / "T"
        t.mkdir()
        p = self.path_with("systemctl")
        self.env(p)
        before = listing(self.tmp)
        r = self.sched("plan", t, path=p)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("demo-ok.timer", r.stdout)
        after = listing(self.tmp)
        self.assertEqual([x for x in before if not x[0].startswith("home")],
                         [x for x in after if not x[0].startswith("home")])
        self.assertEqual(list(t.iterdir()), [])

    def test_apply_report_rollback(self):
        t = self.tmp / "T"
        t.mkdir()
        foreign = t / "foreign.timer"
        foreign.write_text("[Timer]\nOnCalendar=weekly\n")
        foreign_sha = sha(foreign)
        p = self.path_with("systemctl")
        r = self.sched("apply", t, path=p)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue((t / "demo-ok.timer").is_file() and (t / "demo-ok.service").is_file())
        manifest = json.loads((t / ".aiarch-manifest.json").read_text())
        self.assertEqual(sorted(manifest["files"]), ["demo-ok.service", "demo-ok.timer"])
        self.assertLessEqual(set(self.verbs()), {"show-environment", "daemon-reload"})
        self.assertIn("daemon-reload", self.verbs())
        r = self.run_cli("scheduler", "report", "--unit-dir", t, "--format", "json", path=p)
        self.assertEqual(r.returncode, 0, r.stderr)
        units = {u["unit"]: u for u in json.loads(r.stdout)["units"]}
        self.assertEqual(units["demo-ok.timer"]["observed"]["ActiveState"], "active")
        r = self.run_cli("scheduler", "rollback", "--unit-dir", t, path=p)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(sorted(x.name for x in t.iterdir()), ["foreign.timer"])
        self.assertEqual(sha(foreign), foreign_sha)
        r = self.run_cli("scheduler", "rollback", "--unit-dir", t, path=p)
        self.assertEqual(r.returncode, 0)
        self.assertEqual(sha(foreign), foreign_sha)
        self.assertEqual(sorted(x.name for x in t.iterdir()), ["foreign.timer"])

    def test_enable_only_with_flag(self):
        t = self.tmp / "T"
        t.mkdir()
        p = self.path_with("systemctl")
        self.sched("apply", t, "--enable", path=p)
        self.assertIn("enable", self.verbs())
        self.assertRegex(self.log.read_text(), r"enable -- demo-ok\.timer")
        self.assertLessEqual(set(self.verbs()), {"show-environment", "daemon-reload", "enable"})

    def test_apply_refuses_foreign_collision(self):
        t = self.tmp / "T"
        t.mkdir()
        (t / "demo-ok.timer").write_text("user owned\n")
        r = self.sched("apply", t, path=self.path_with("systemctl"))
        self.assertEqual(r.returncode, 3)
        self.assertEqual((t / "demo-ok.timer").read_text(), "user owned\n")

    def test_unsupported_without_fake(self):
        t = self.tmp / "T"
        t.mkdir()
        r = self.sched("apply", t, path=self.path_with())
        self.assertEqual(r.returncode, 3)
        self.assertIn("unsupported", r.stdout + r.stderr)
        self.assertEqual(list(t.iterdir()), [])

    def test_cron_adapter_only_lists(self):
        f = self.tmp / "cron.txt"
        p = self.path_with("crontab")
        a = ["scheduler", "apply", "--registry", self.reg, "--loop", "demo-ok", "--adapter", "cron",
             "--crontab-file", f, "--calendar", "0 3 * * *", "--exec", "/bin/true"]
        r = self.run_cli(*a, path=p)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("0 3 * * * /bin/true", f.read_text())
        self.assertIn("crontab " + str(f), r.stdout)
        self.assertTrue(self.verbs())
        for verb in self.verbs():
            self.assertEqual(verb, ("crontab", ("-l",)))
        r = self.run_cli("scheduler", "rollback", "--crontab-file", f, path=p)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(f.exists())
        r = self.run_cli(*a, path=self.path_with())
        self.assertEqual(r.returncode, 3)

    def test_observe_reports_argv_and_unsupported(self):
        p = self.path_with("systemctl", "crontab")
        r = self.run_cli("scheduler", "observe", "--format", "json", path=p)
        self.assertEqual(r.returncode, 0, r.stderr)
        data = json.loads(r.stdout)
        self.assertEqual(data["adapters"]["systemd-user"]["state"], "observed")
        self.assertIn(["systemctl", "--user", "show-environment"], data["argv"])
        self.assertIn("windows", data["adapters"])
        self.assertEqual(data["adapters"]["windows"]["state"], "unsupported")
        self.assertTrue(set(self.verbs()) <= {"show-environment", ("crontab", ("-l",))})


class TestA4Changelog(Base):
    def make_repo(self):
        g = self.tmp / "repo"
        g.mkdir()
        env = {**os.environ, **self.env(self.path_with())}

        def git(*a, date=None):
            e = dict(env)
            if date:
                e["GIT_AUTHOR_DATE"] = e["GIT_COMMITTER_DATE"] = date
            subprocess.run(["git", "-C", str(g), *a], check=True, capture_output=True, env=e)

        git("init", "-q", "-b", "main")
        for i, (subject, date) in enumerate([
            ("feat: first", "2026-01-05T10:00:00+00:00"),
            ("autopilot: 3 files", "2026-01-06T10:00:00+00:00"),
            ("fix: second", "2026-01-14T10:00:00+00:00"),
        ]):
            (g / f"f{i}").write_text(str(i))
            git("add", "-A")
            git("commit", "-q", "-m", subject, date=date)
        return g

    def test_changelog(self):
        g = self.make_repo()
        out = self.tmp / "out" / "CHANGELOG.draft.md"
        r = self.run_cli("changelog", "--repo", g, "--since", "2026-01-01", "--out", out)
        self.assertEqual(r.returncode, 0, r.stderr)
        text = out.read_text()
        self.assertIn("feat: first", text)
        self.assertIn("fix: second", text)
        self.assertNotIn("autopilot:", text)
        self.assertIn("## Week of 2026-01-05", text)
        self.assertIn("## Week of 2026-01-12", text)
        first = out.read_bytes()
        mtime = out.stat().st_mtime_ns
        r = self.run_cli("changelog", "--repo", g, "--since", "2026-01-01", "--out", out)
        self.assertEqual(r.returncode, 0)
        self.assertEqual(out.read_bytes(), first)
        self.assertEqual(out.stat().st_mtime_ns, mtime)
        self.assertIn("unchanged", r.stdout)

    def test_out_inside_repo_refused(self):
        g = self.make_repo()
        r = self.run_cli("changelog", "--repo", g, "--since", "2026-01-01", "--out", g / "CHANGELOG.md")
        self.assertEqual(r.returncode, 2)
        self.assertFalse((g / "CHANGELOG.md").exists())
        self.assertEqual(self.run_cli("changelog", "--repo", g, "--since", "2026-01-01").returncode, 2)

    def test_retire(self):
        before = self.reg.read_bytes()
        a = ["retire", "--registry", self.reg, "--loop", "demo-idle", "--reason", "replaced", "--date", "2026-11-04"]
        r = self.run_cli(*a)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("+", r.stdout)
        self.assertIn("retired", r.stdout)
        self.assertEqual(self.reg.read_bytes(), before)
        r = self.run_cli(*a, "--apply")
        self.assertEqual(r.returncode, 0, r.stderr)
        data = json.loads(self.reg.read_text())
        loop = [x for x in data["loops"] if x["id"] == "demo-idle"][0]
        self.assertEqual(loop["status"], "retired")
        self.assertEqual(loop["retired"], {"date": "2026-11-04", "reason": "replaced"})
        self.assertEqual((self.tmp / "loops.json.bak").read_bytes(), before)
        self.assertEqual(self.run_cli("validate", "--registry", self.reg).returncode, 0)
        r = self.run_cli("retire", "--registry", self.reg, "--loop", "nope", "--reason", "x")
        self.assertEqual(r.returncode, 2)


class TestA14(Base):
    def test_descriptor_and_privacy(self):
        spec = importlib.util.spec_from_file_location("contracts_u15m", ROOT / "modules/contracts/contracts.py")
        contracts = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(contracts)
        contracts.check_module_descriptor(MOD, module="maintenance")
        self.assertEqual(json.loads((MOD / "descriptor.json").read_text())["claimed_support"], "planned")
        spec = importlib.util.spec_from_file_location("privacy_u15m", ROOT / "scripts/privacy_scan.py")
        privacy = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(privacy)
        planted = self.tmp / "planted"
        planted.mkdir()
        (planted / "x.md").write_text("see " + "/" + "home/someone/file\n")
        self.assertTrue(privacy.scan(planted))
        shutil.copytree(MOD, self.tmp / "scan" / "modules" / "maintenance", ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copytree(LOOPSYNC_FIX.parent.parent, self.tmp / "scan" / "tools" / "loop-sync")
        self.assertEqual(privacy.scan(self.tmp / "scan"), [])

    def test_modules_have_docs(self):
        for name in ("README.md", "INTEGRATION.md", "descriptor.json", "maintenance.py"):
            self.assertTrue((MOD / name).is_file(), name)


if __name__ == "__main__":
    unittest.main()
