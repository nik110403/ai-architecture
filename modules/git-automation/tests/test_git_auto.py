import fcntl
import importlib.util
import json
import os
import pty
import re
import shutil
import subprocess
import sys
import tempfile
import termios
import types
import unittest
from pathlib import Path

MOD = Path(__file__).resolve().parents[1]
ROOT = MOD.parents[1]
CLI = MOD / "git_auto.py"
REAL_GIT = shutil.which("git")
SAFE_TOOLS = ("bash", "sh", "env", "cat", "ls", "rm", "mkdir", "sed", "grep", "awk", "dirname", "basename",
              "true", "false", "tee", "date", "head", "tail", "sort", "mktemp", "chmod")
WRAPPER = """#!/bin/bash
printf '%s\\n' "$*" >> "$WRAP_LOG"
case " $* " in *" push "*)
  if [ -n "$WRAP_RACE" ]; then "$REAL_GIT" -C "$WRAP_RACE" -c user.name=T -c user.email=t@example.invalid commit -q --allow-empty -m race; fi;;
esac
exec "$REAL_GIT" "$@"
"""
SITECUSTOMIZE = """import ipaddress, os, sys
LOG = os.environ.get("NET_LOG")
def hook(event, args):
    if event == "socket.connect" and LOG:
        address = args[1]
        host = address[0] if isinstance(address, tuple) else str(address)
        try:
            loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            loopback = host in ("localhost",) or not isinstance(address, tuple)
        if not loopback:
            open(LOG, "a").write(repr(address) + "\\n")
sys.addaudithook(hook)
"""


def load_module():
    spec = importlib.util.spec_from_file_location("git_auto_under_test", CLI)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name).resolve()
        self.log = self.tmp / "git.log"
        self.log.write_text("")
        self.netlog = self.tmp / "net.log"
        audit = self.tmp / "audit"
        audit.mkdir()
        (audit / "sitecustomize.py").write_text(SITECUSTOMIZE)
        bindir = self.tmp / "bin"
        bindir.mkdir()
        for tool in SAFE_TOOLS:
            found = shutil.which(tool)
            if found:
                (bindir / tool).symlink_to(found)
        (bindir / "git").write_text(WRAPPER)
        (bindir / "git").chmod(0o755)
        self.assertEqual(shutil.which("git", path=str(bindir)), str(bindir / "git"))
        ident = "[user]\n\tname = T\n\temail = t@example.invalid\n"
        (self.tmp / "gitconfig").write_text(ident)
        home = self.tmp / "home"
        home.mkdir()
        (home / ".gitconfig").write_text(ident)  # the module drops GIT_* env, so identity comes from HOME
        self.env = {
            "PATH": str(bindir), "HOME": str(home), "XDG_CONFIG_HOME": str(home / "c"),
            "XDG_STATE_HOME": str(home / "s"), "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": str(self.tmp / "gitconfig"), "WRAP_LOG": str(self.log), "REAL_GIT": REAL_GIT,
            "GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@example.invalid",
            "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@example.invalid",
            "PYTHONPATH": str(audit), "NET_LOG": str(self.netlog),
        }
        self.origin = self.tmp / "origin.git"
        self.git(self.tmp, "init", "-q", "--bare", "-b", "main", self.origin)
        self.A = self.clone("A")
        (self.A / "base.txt").write_text("base\n")
        self.git(self.A, "add", "-A")
        self.git(self.A, "commit", "-q", "-m", "init")
        self.git(self.A, "push", "-q", "origin", "main")
        self.B = self.clone("B")

    def tearDown(self):
        text = self.log.read_text()
        for bad in (r"--force", r"\s\+\S", r"--hard", r" -f\b", r"--mirror", r"--delete"):
            self.assertIsNone(re.search(bad, text), (bad, text))
        self.assertFalse(set(self.verbs()) & {"reset", "clean", "pull", "rebase", "restore", "stash"}, text)
        self.assertFalse(self.netlog.exists() and self.netlog.read_text(), "non-loopback connect")

    def git(self, cwd, *args):
        """Harness git: absolute binary, not through the PATH wrapper (so the log shows only module calls)."""
        env = {**os.environ, **self.env}
        r = subprocess.run([REAL_GIT, "-C", str(cwd), *map(str, args)], capture_output=True, text=True, env=env)
        self.assertEqual(r.returncode, 0, (args, r.stderr))
        return r.stdout.strip()

    def clone(self, name):
        dest = self.tmp / name
        env = {**os.environ, **self.env}
        subprocess.run([REAL_GIT, "clone", "-q", str(self.origin), str(dest)], check=True, env=env,
                       capture_output=True)
        return dest

    def registry(self, repos, host="host-a", name="reg.json"):
        data = {"schema_version": 1, "committer_host": host, "repos": repos}
        path = self.tmp / name
        path.write_text(json.dumps(data))
        return path

    def repo(self, path, mode="commit", visibility="private", name="alpha", remote=None):
        return {"name": name, "path": str(path), "mode": mode, "visibility": visibility,
                "remote": remote if remote is not None else str(self.origin),
                "allow_protocols": "file"}  # tests stay on the file transport

    def cli(self, *args, tty=None, extra_env=None):
        env = {**self.env, **(extra_env or {})}
        cmd = [sys.executable, "-B", str(CLI), *map(str, args)]
        if tty is None:
            # own session: no controlling terminal even if the runner has one
            return subprocess.run(cmd, capture_output=True, text=True, env=env, cwd=self.tmp, timeout=60,
                                  stdin=subprocess.DEVNULL, start_new_session=True)
        master, slave = pty.openpty()
        os.write(master, tty.encode())
        # No preexec_fn (unsafe with threads): a tiny launcher makes the pty the controlling terminal, then execs.
        launcher = ("import fcntl, os, sys, termios\n"
                    "fcntl.ioctl(int(sys.argv[1]), termios.TIOCSCTTY, 0)\n"
                    "os.execv(sys.argv[2], sys.argv[2:])\n")
        try:
            return subprocess.run(
                [sys.executable, "-c", launcher, str(slave), *cmd], capture_output=True, text=True, env=env,
                cwd=self.tmp, timeout=60, stdin=subprocess.DEVNULL, start_new_session=True, pass_fds=(slave,))
        finally:
            os.close(master)
            os.close(slave)

    def snapshot(self, repo, remotes=True):
        refs = self.git(repo, "for-each-ref", "refs/heads", *(["refs/remotes"] if remotes else []))
        index = (Path(repo) / ".git" / "index").read_bytes()
        tree = sorted((p.relative_to(repo).as_posix(), p.read_bytes() if p.is_file() else b"")
                      for p in Path(repo).rglob("*") if ".git" not in p.relative_to(repo).parts)
        return refs, self.git(repo, "rev-parse", "HEAD"), index, tree, self.git(self.origin, "for-each-ref")

    def verbs(self):
        out = []
        for line in self.log.read_text().splitlines():
            parts = line.split()
            if parts and parts[0] == "-C":
                parts = parts[2:]
            out.append(parts[0] if parts else "")
        return out

    def dirty(self, repo, name="new.txt", text="hello\n"):
        (Path(repo) / name).write_text(text)


class TestA10Run(Base):
    def test_status_and_dry_run_change_nothing(self):
        self.dirty(self.A)
        reg = self.registry([self.repo(self.A)])
        before = self.snapshot(self.A)
        for args in (("status", "--registry", reg), ("run", "--registry", reg, "--host", "host-a")):
            r = self.cli(*args)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertEqual(self.snapshot(self.A), before)
        self.assertNotIn("commit", self.verbs())
        self.assertNotIn("push", self.verbs())
        self.assertNotIn("fetch", self.verbs())

    def test_run_apply_commits_and_pushes_once(self):
        self.dirty(self.A)
        self.dirty(self.A, "two.txt", "2\n")
        (self.A / ".gitignore").write_text("ignored.log\n")
        (self.A / "ignored.log").write_text("noise\n")
        reg = self.registry([self.repo(self.A)])
        r = self.cli("run", "--apply", "--registry", reg, "--host", "host-a")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.git(self.A, "log", "-1", "--format=%s"), "autopilot: 3 files")
        self.assertEqual(self.git(self.origin, "rev-parse", "main"), self.git(self.A, "rev-parse", "HEAD"))
        self.assertNotIn("ignored.log", self.git(self.A, "ls-files"))
        self.assertIn("commit", self.verbs())
        self.assertIn("push", self.verbs())
        before = self.snapshot(self.A, remotes=False)
        r = self.cli("run", "--apply", "--registry", reg, "--host", "host-a")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.snapshot(self.A, remotes=False)[:2], before[:2])
        self.assertEqual(self.git(self.origin, "rev-parse", "main"), before[1])

    def test_host_mismatch_exit_3(self):
        self.dirty(self.A)
        reg = self.registry([self.repo(self.A)])
        before = self.snapshot(self.A)
        r = self.cli("run", "--apply", "--registry", reg, "--host", "host-other")
        self.assertEqual(r.returncode, 3)
        self.assertEqual(self.snapshot(self.A), before)
        self.assertEqual(self.verbs(), [])

    def test_secret_files_stop_the_run(self):
        reg = self.registry([self.repo(self.A)])
        for name, text in ((".env", "K=v\n"), ("k.txt", "-----BEGIN RSA " + "PRIVATE KEY-----\nabc\n")):
            self.dirty(self.A, name, text)
            before = self.snapshot(self.A, remotes=False)
            r = self.cli("run", "--apply", "--registry", reg, "--host", "host-a")
            self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
            self.assertIn(name, r.stdout + r.stderr)
            self.assertEqual(self.snapshot(self.A, remotes=False), before)
            (self.A / name).unlink()
        self.assertNotIn("commit", self.verbs())

    def test_remote_mismatch_and_network_fence(self):
        self.dirty(self.A)
        reg = self.registry([self.repo(self.A, remote="/not/the/origin")])
        r = self.cli("run", "--apply", "--registry", reg, "--host", "host-a")
        self.assertEqual(r.returncode, 3)
        self.assertIn("remote mismatch", r.stdout + r.stderr)
        self.assertNotIn("commit", self.verbs())
        self.git(self.A, "remote", "set-url", "origin", "ssh://example.invalid/x")
        reg = self.registry([self.repo(self.A, remote="ssh://example.invalid/x")])
        r = self.cli("run", "--apply", "--registry", reg, "--host", "host-a",
                     extra_env={"GIT_ALLOW_PROTOCOL": "all", "GIT_SSH_COMMAND": "ssh"})
        self.assertNotEqual(r.returncode, 0)
        self.assertNotIn("push", self.verbs())


class TestA11Divergence(Base):
    def b_pushes(self, name="b.txt"):
        (self.B / name).write_text("b\n")
        self.git(self.B, "add", "-A")
        self.git(self.B, "commit", "-q", "-m", "from b")
        self.git(self.B, "push", "-q", "origin", "main")

    def stops(self, expect="stop"):
        reg = self.registry([self.repo(self.A)])
        before = self.snapshot(self.A, remotes=False)
        origin = self.git(self.origin, "for-each-ref")
        r = self.cli("run", "--apply", "--registry", reg, "--host", "host-a")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn(expect, r.stdout + r.stderr)
        self.assertEqual(self.snapshot(self.A, remotes=False)[:3], before[:3])
        self.assertEqual(self.git(self.origin, "for-each-ref"), origin)
        for verb in ("commit", "push", "merge", "pull", "rebase"):
            self.assertNotIn(verb, self.verbs())

    def test_diverged_after_other_push(self):
        self.b_pushes()
        self.dirty(self.A)
        self.stops("diverged")

    def test_behind_clean_stops(self):
        self.b_pushes()
        self.stops("behind")

    def test_merge_in_progress(self):
        self.dirty(self.A)
        head = self.git(self.A, "rev-parse", "HEAD")
        (self.A / ".git" / "MERGE_HEAD").write_text(head + "\n")
        self.stops("merge")

    def test_conflict_markers(self):
        self.dirty(self.A, "c.txt", "<<<<<<< HEAD\nx\n=======\ny\n>>>>>>> other\n")
        self.stops("conflict")

    def test_detached_head(self):
        self.dirty(self.A)
        self.git(self.A, "checkout", "-q", "--detach")
        self.stops("detached")

    def test_rewritten_origin(self):
        self.git(self.B, "commit", "-q", "--amend", "-m", "rewritten", "--date", "2020-01-01T00:00:00")
        self.git(self.B, "push", "-q", "--force", "origin", "main")
        self.dirty(self.A)
        self.stops("rewritten")


class TestA12Follow(Base):
    def test_follow_fast_forwards_and_is_idempotent(self):
        self.dirty(self.A)
        regA = self.registry([self.repo(self.A)], name="a.json")
        self.assertEqual(self.cli("run", "--apply", "--registry", regA, "--host", "host-a").returncode, 0)
        regB = self.registry([self.repo(self.B, mode="follow-only")], name="b.json")
        before = self.snapshot(self.B, remotes=False)
        r = self.cli("follow", "--registry", regB, "--host", "host-b")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.snapshot(self.B, remotes=False), before)
        r = self.cli("follow", "--apply", "--registry", regB, "--host", "host-b")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.git(self.B, "rev-parse", "HEAD"), self.git(self.A, "rev-parse", "HEAD"))
        self.assertTrue((self.B / "new.txt").exists())
        after = self.snapshot(self.B, remotes=False)
        r = self.cli("follow", "--apply", "--registry", regB, "--host", "host-b")
        self.assertEqual(r.returncode, 0)
        self.assertEqual(self.snapshot(self.B, remotes=False), after)

    def test_follow_refuses_unpushed_local_commit(self):
        self.dirty(self.A)
        self.cli("run", "--apply", "--registry", self.registry([self.repo(self.A)], name="a.json"),
                 "--host", "host-a")
        self.dirty(self.B, "mine.txt", "m\n")
        self.git(self.B, "add", "-A")
        self.git(self.B, "commit", "-q", "-m", "local")
        regB = self.registry([self.repo(self.B, mode="follow-only")], name="b.json")
        before = self.snapshot(self.B, remotes=False)
        r = self.cli("follow", "--apply", "--registry", regB, "--host", "host-b")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertEqual(self.snapshot(self.B, remotes=False), before)

    def test_follow_refuses_dirty_overlap(self):
        self.dirty(self.A, "base.txt", "changed upstream\n")
        self.cli("run", "--apply", "--registry", self.registry([self.repo(self.A)], name="a.json"),
                 "--host", "host-a")
        self.dirty(self.B, "base.txt", "local edit\n")
        regB = self.registry([self.repo(self.B, mode="follow-only")], name="b.json")
        before = self.snapshot(self.B, remotes=False)
        r = self.cli("follow", "--apply", "--registry", regB, "--host", "host-b")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertEqual(self.snapshot(self.B, remotes=False), before)


class TestGitGuard(Base):
    FORBIDDEN = [
        ["push", "--force"], ["push", "-f"], ["push", "--force-with-lease"], ["push", "--force-with-lease=main"],
        ["push", "--mirror"], ["push", "--all"], ["push", "--delete", "origin", "x"], ["push", "--tags"],
        ["push", "origin", "+main"], ["reset", "--hard"], ["reset", "--hard", "origin/main"], ["clean", "-fd"],
        ["checkout", "--", "f"], ["restore", "f"], ["stash", "drop"], ["branch", "-D", "x"], ["rebase", "main"],
        ["pull"], ["pull", "--rebase"], ["-C", "/x", "push", "--force"],
    ]

    def test_each_forbidden_operation_exits_4_without_spawning(self):
        module = load_module()
        calls = []
        module.subprocess = types.SimpleNamespace(run=lambda *a, **k: calls.append(a) or self.fail("spawned"))
        for argv in self.FORBIDDEN:
            with self.assertRaises(SystemExit, msg=argv) as ctx:
                module._git(argv)
            self.assertEqual(ctx.exception.code, 4, argv)
        self.assertEqual(calls, [])

    def test_paths_after_double_dash_are_not_flags(self):
        module = load_module()
        module._forbidden(["add", "--", "+odd", "-f"])

    def test_env_is_pinned(self):
        module = load_module()
        seen = {}

        class R:
            returncode, stdout, stderr = 0, "", ""
        module.subprocess = types.SimpleNamespace(run=lambda cmd, **k: seen.update(env=k["env"], cmd=cmd) or R())
        hostile = {"GIT_ALLOW_PROTOCOL": "all", "GIT_SSH_COMMAND": "ssh", "GIT_CONFIG_COUNT": "1",
                   "GIT_CONFIG_KEY_0": "remote.origin.pushurl", "GIT_CONFIG_VALUE_0": "evil.git",
                   "GIT_DIR": "/elsewhere", "GIT_EXEC_PATH": "/evil", "GIT_SSH": "/evil/ssh"}
        os.environ.update(hostile)
        self.addCleanup(lambda: [os.environ.pop(k) for k in hostile])
        module._git(["status"], cwd="/x")
        env = seen["env"]
        self.assertEqual(env["GIT_ALLOW_PROTOCOL"], "file:ssh:https")
        self.assertEqual(env["GIT_TERMINAL_PROMPT"], "0")
        self.assertEqual(env["GIT_CONFIG_NOSYSTEM"], "1")
        self.assertEqual(env["GIT_OPTIONAL_LOCKS"], "0")
        self.assertEqual(sorted(k for k in env if k.startswith("GIT_")),
                         ["GIT_ALLOW_PROTOCOL", "GIT_CONFIG_NOSYSTEM", "GIT_OPTIONAL_LOCKS", "GIT_TERMINAL_PROMPT"])

    def test_allow_protocols_validation(self):
        for bad in ("ext", "git", "file:ext", "file:git", "", "file:file", ["file"], "all"):
            repo = self.repo(self.A)
            repo["allow_protocols"] = bad
            r = self.cli("validate", "--registry", self.registry([repo]))
            self.assertEqual(r.returncode, 2, (bad, r.stdout + r.stderr))
        repo = self.repo(self.A)
        repo["allow_protocols"] = "file:https"
        self.assertEqual(self.cli("validate", "--registry", self.registry([repo])).returncode, 0)


class TestRemoteGate(Base):
    def assert_refused(self, cmd, evil, extra_env=None):
        self.dirty(self.A)
        reg = self.registry([self.repo(self.A)])
        before = self.git(self.origin, "for-each-ref")
        r = self.cli(*cmd(reg), extra_env=extra_env)
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
        self.assertIn("remote mismatch", r.stdout + r.stderr)
        self.assertEqual(self.git(self.origin, "for-each-ref"), before)
        self.assertFalse(self.git(evil, "for-each-ref"))
        for verb in ("commit", "push"):
            self.assertNotIn(verb, self.verbs())

    def evil(self):
        evil = self.tmp / "evil.git"
        self.git(self.tmp, "init", "-q", "--bare", "-b", "main", evil)
        return evil

    def run_cmd(self, reg):
        return ("run", "--apply", "--registry", reg, "--host", "host-a")

    def test_pushurl_config(self):
        evil = self.evil()
        self.git(self.A, "config", "remote.origin.pushurl", str(evil))
        self.assert_refused(self.run_cmd, evil)

    def test_pushurl_via_caller_env(self):
        # the caller's GIT_CONFIG_* is dropped: the push goes to the registered origin, never to evil
        evil = self.evil()
        self.dirty(self.A)
        reg = self.registry([self.repo(self.A)])
        r = self.cli(*self.run_cmd(reg), extra_env={
            "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "remote.origin.pushurl", "GIT_CONFIG_VALUE_0": str(evil)})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse(self.git(evil, "for-each-ref"))
        self.assertEqual(self.git(self.origin, "rev-parse", "main"), self.git(self.A, "rev-parse", "HEAD"))

    def test_env_cannot_make_a_mismatch_pass(self):
        # a hostile caller env must not rewrite what the gate sees either
        self.dirty(self.A)
        reg = self.registry([self.repo(self.A, remote="/not/the/origin")])
        r = self.cli("run", "--apply", "--registry", reg, "--host", "host-a", extra_env={
            "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "remote.origin.url", "GIT_CONFIG_VALUE_0": "/not/the/origin"})
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)

    def test_second_url_value(self):
        evil = self.evil()
        self.git(self.A, "config", "--add", "remote.origin.url", str(evil))
        self.assert_refused(self.run_cmd, evil)

    def test_follow_checks_push_url_too(self):
        evil = self.evil()
        self.git(self.A, "config", "remote.origin.pushurl", str(evil))
        self.assert_refused(lambda reg: ("follow", "--apply", "--registry", reg, "--host", "host-b"), evil)

    def test_push_on_mismatched_remote(self):
        evil = self.evil()
        self.dirty(self.A)
        reg = self.registry([self.repo(self.A, visibility="public")])
        self.cli("run", "--apply", "--registry", reg, "--host", "host-a")
        head = self.git(self.A, "rev-parse", "HEAD")
        self.git(self.A, "config", "remote.origin.pushurl", str(evil))
        before = self.git(self.origin, "for-each-ref")
        r = self.cli("push", "alpha", "--registry", reg, "--approve-head", head, tty="alpha\n")
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
        self.assertIn("remote mismatch", r.stdout + r.stderr)
        self.assertEqual(self.git(self.origin, "for-each-ref"), before)
        self.assertFalse(self.git(evil, "for-each-ref"))
        self.assertNotIn("push", self.verbs())


class TestA13Public(Base):
    def public_registry(self):
        return self.registry([self.repo(self.A, visibility="public")])

    def test_run_never_pushes_public(self):
        self.dirty(self.A)
        origin = self.git(self.origin, "for-each-ref")
        r = self.cli("run", "--apply", "--registry", self.public_registry(), "--host", "host-a")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("public: push requires --approve-head", r.stdout)
        self.assertEqual(self.git(self.origin, "for-each-ref"), origin)
        self.assertNotIn("push", self.verbs())

    def test_push_needs_head_and_terminal(self):
        self.dirty(self.A)
        reg = self.public_registry()
        self.cli("run", "--apply", "--registry", reg, "--host", "host-a")
        head = self.git(self.A, "rev-parse", "HEAD")
        origin = self.git(self.origin, "for-each-ref")
        self.assertEqual(self.cli("push", "alpha", "--registry", reg).returncode, 3)
        self.assertEqual(self.cli("push", "alpha", "--registry", reg, "--approve-head", "0" * 40).returncode, 3)
        r = self.cli("push", "alpha", "--registry", reg, "--approve-head", head)
        self.assertEqual(r.returncode, 3)
        self.assertIn("approval requires an interactive terminal", r.stdout + r.stderr)
        r = self.cli("push", "alpha", "--registry", reg, "--approve-head", head, tty="wrong\n")
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
        self.assertEqual(self.git(self.origin, "for-each-ref"), origin)
        self.assertNotIn("push", self.verbs())
        r = self.cli("push", "alpha", "--registry", reg, "--approve-head", head, tty="alpha\n")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.verbs().count("push"), 1)
        self.assertEqual(self.git(self.origin, "rev-parse", "main"), head)

    def public_pushed_state(self):
        self.dirty(self.A)
        reg = self.public_registry()
        self.cli("run", "--apply", "--registry", reg, "--host", "host-a")
        return reg, self.git(self.A, "rev-parse", "HEAD")

    def test_commit_during_prompt_is_not_pushed(self):
        reg, head = self.public_pushed_state()
        origin = self.git(self.origin, "rev-parse", "main")
        master, slave = pty.openpty()
        launcher = ("import fcntl, os, sys, termios\n"
                    "fcntl.ioctl(int(sys.argv[1]), termios.TIOCSCTTY, 0)\n"
                    "os.execv(sys.argv[2], sys.argv[2:])\n")
        cmd = [sys.executable, "-B", str(CLI), "push", "alpha", "--registry", str(reg), "--approve-head", head]
        proc = subprocess.Popen([sys.executable, "-c", launcher, str(slave), *cmd], env=self.env, cwd=self.tmp,
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                start_new_session=True, pass_fds=(slave,))
        os.close(slave)
        try:
            seen = b""
            while b"Type the repo name" not in seen:  # the prompt is up: HEAD was read and approved
                chunk = os.read(master, 4096)
                self.assertTrue(chunk, seen)
                seen += chunk
            self.dirty(self.A, "late.txt", "unapproved\n")
            self.git(self.A, "add", "-A")
            self.git(self.A, "commit", "-q", "-m", "late")
            unapproved = self.git(self.A, "rev-parse", "HEAD")
            os.write(master, b"alpha\n")
            stdout, _ = proc.communicate(timeout=60)
        finally:
            os.close(master)
            proc.kill()
        self.assertNotEqual(unapproved, head)
        remote = self.git(self.origin, "rev-parse", "main")
        self.assertNotEqual(remote, unapproved, stdout)
        self.assertIn(remote, (origin, head))
        self.assertEqual(proc.returncode, 3, stdout)
        self.assertNotIn("push", self.verbs())

    def test_push_binds_the_approved_sha(self):
        # a commit lands after the final HEAD check, right as the push runs (wrapper hook)
        reg, head = self.public_pushed_state()
        r = self.cli("push", "alpha", "--registry", reg, "--approve-head", head, tty="alpha\n",
                     extra_env={"WRAP_RACE": str(self.A)})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotEqual(self.git(self.A, "rev-parse", "HEAD"), head)
        self.assertEqual(self.git(self.origin, "rev-parse", "main"), head)
        self.assertIn(f"pushed main at {head}", r.stdout)

    def test_no_generated_artifact_contains_push(self):
        reg = self.registry([self.repo(self.A)])
        r = self.cli("unit", "--registry", reg, "--host", "host-a")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("run --apply", r.stdout)
        self.assertNotIn("push", r.stdout)
        steward = self.tmp / "steward.json"
        steward.write_text(json.dumps({"schema_version": 1, "projects": [{"root": str(self.A)}]}))
        out = self.tmp / "imported.json"
        r = self.cli("import", "--from-steward", steward, "--host", "host-a", "--out", out)
        self.assertEqual(r.returncode, 0, r.stderr)
        data = json.loads(out.read_text())
        self.assertEqual([(x["mode"], x["visibility"]) for x in data["repos"]], [("off", "public")])
        self.assertNotIn("push", out.read_text())
        self.assertEqual(self.cli("validate", "--registry", out).returncode, 0)

    def test_visibility_is_required(self):
        repo = self.repo(self.A)
        del repo["visibility"]
        self.assertEqual(self.cli("validate", "--registry", self.registry([repo])).returncode, 2)
        dup = self.tmp / "dup.json"
        dup.write_text('{"schema_version": 1, "schema_version": 1, "committer_host": "h", "repos": []}')
        self.assertEqual(self.cli("validate", "--registry", dup).returncode, 2)
        self.assertEqual(self.cli("validate", "--registry", self.registry([self.repo(self.A)])).returncode, 0)


class TestA14(Base):
    def test_descriptor_and_privacy(self):
        def load(name, rel):
            spec = importlib.util.spec_from_file_location(name, ROOT / rel)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod
        contracts = load("contracts_u15g", "modules/contracts/contracts.py")
        contracts.check_module_descriptor(MOD, module="git-automation")
        self.assertEqual(json.loads((MOD / "descriptor.json").read_text())["claimed_support"], "planned")
        privacy = load("privacy_u15g", "scripts/privacy_scan.py")
        planted = self.tmp / "planted"
        planted.mkdir()
        (planted / "x.md").write_text("see " + "/" + "home/someone/file\n")
        self.assertTrue(privacy.scan(planted))
        shutil.copytree(MOD, self.tmp / "scan" / "modules" / "git-automation",
                        ignore=shutil.ignore_patterns("__pycache__"))
        self.assertEqual(privacy.scan(self.tmp / "scan"), [])


if __name__ == "__main__":
    unittest.main()
