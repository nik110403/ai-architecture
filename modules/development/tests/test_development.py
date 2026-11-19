"""U19 acceptance A1..A13 plus the amendment cells. Offline; temp HOME; one shared baseline run."""

import ast
import atexit
import copy
import getpass
import hashlib
import itertools
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEVDIR = HERE.parent
ROOT = DEVDIR.parents[1]
DEV = DEVDIR / "development.py"
EXDIR = ROOT / "examples" / "development"
RUN = EXDIR / "run.py"
DISPATCH = ROOT / "modules" / "orchestration" / "dispatch.py"
PY = sys.executable
KINDS = ["debug", "domain", "secure-build", "security-audit", "incident"]
GIT_ENV = {
    "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "fixture", "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
    "GIT_COMMITTER_NAME": "fixture", "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
    "GIT_AUTHOR_DATE": "2026-01-01T00:00:00Z", "GIT_COMMITTER_DATE": "2026-01-01T00:00:00Z",
}
HARDEN = ["-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false", "-c", "gc.auto=0"]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
_counter = itertools.count()


def sha(data):
    return hashlib.sha256(data if isinstance(data, bytes) else data.encode()).hexdigest()


def canon(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def dump(obj):
    return json.dumps(obj, indent=2, sort_keys=True) + "\n"


def git(repo, *argv, env=None, data=None):
    proc = subprocess.run(["git", "-C", str(repo), *HARDEN, *argv], capture_output=True, input=data,
                          env={**os.environ, **GIT_ENV, **(env or {})}, timeout=60)
    assert proc.returncode == 0, (argv, proc.stderr)
    return proc.stdout


def variant(repo, parent, tree_commit, files):
    """A new commit object (no ref moves, no worktree touched): tree of tree_commit with `files` replaced."""
    index = Path(tempfile.mkdtemp(prefix="idx-")) / "index"
    env = {"GIT_INDEX_FILE": str(index)}
    git(repo, "read-tree", tree_commit, env=env)
    for path, data in files.items():
        blob = git(repo, "hash-object", "-w", "--stdin", data=data).decode().strip()
        git(repo, "update-index", "--add", "--cacheinfo", f"100644,{blob},{path}", env=env)
    tree = git(repo, "write-tree", env=env).decode().strip()
    shutil.rmtree(index.parent, ignore_errors=True)
    return git(repo, "commit-tree", tree, "-p", parent, "-m", "variant").decode().strip()


def blob(repo, commit, path):
    return git(repo, "show", f"{commit}:{path}")


_xdg = []


def xdg_runs():
    """Two bare SPEC-command runs (temp XDG_STATE_HOME) into one dir; cached for A1 and A11."""
    if not _xdg:
        b = B()
        state = b.tmp / "xdg"
        state.mkdir()
        procs = [b.run_script(["--offline"], XDG_STATE_HOME=str(state)) for _ in range(2)]
        _xdg.append((state / "ai-architecture" / "development", procs))
    return _xdg[0]


class Baseline:
    def __init__(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dev-test-")).resolve()
        atexit.register(shutil.rmtree, self.tmp, True)
        self.home = self.tmp / "home"
        self.home.mkdir()
        hooks = self.tmp / "hooks"
        hooks.mkdir()
        self.sentinels = self.tmp / "sentinels"
        self.sentinels.mkdir()
        hook = hooks / "post-commit"
        hook.write_text(f"#!/bin/sh\ntouch '{self.sentinels}/hook'\n")
        hook.chmod(0o755)
        (self.home / ".gitconfig").write_text(
            f"[commit]\n\tgpgsign = true\n[gpg]\n\tprogram = /bin/false\n[core]\n\thooksPath = {hooks}\n")
        self.fakes = self.tmp / "fakes"
        self.fakes.mkdir()
        for name in ("claude", "opencode", "curl", "wget"):
            path = self.fakes / name
            path.write_text(f"#!/bin/sh\ntouch '{self.sentinels}/{name}'\nexit 1\n")
            path.chmod(0o755)
        self.out = self.tmp / "out"
        self.proc = self.run_script(["--offline", "--out", str(self.out)])
        self.run = self.latest(self.out)
        self.artifacts = {k: json.loads((self.run / "artifacts" / f"{k}.json").read_text()) for k in KINDS}
        self.receipt = json.loads((self.run / "receipt.json").read_text())

    def env(self, **extra):
        env = {**os.environ, "HOME": str(self.home), "PATH": f"{self.fakes}{os.pathsep}{os.environ['PATH']}"}
        env.pop("XDG_STATE_HOME", None)
        env.update(extra)
        return env

    def run_script(self, argv, **extra):
        return subprocess.run([PY, "-B", str(RUN), *argv], capture_output=True, text=True, env=self.env(**extra),
                              timeout=300)

    @staticmethod
    def latest(out):
        return out / json.loads((out / "latest.json").read_text())["run"]

    def repo(self, name):
        return self.run / "repos" / name


_baseline = None


def B():
    global _baseline
    if _baseline is None:
        _baseline = Baseline()
    return _baseline


def tool(*argv, env=None):
    return subprocess.run([PY, "-B", str(DEV), *argv], capture_output=True, text=True,
                          env=env or B().env(), timeout=120)


def check(kind, art, kind_flag=None):
    alt = B().run / "alt"
    alt.mkdir(exist_ok=True)
    path = alt / f"a{next(_counter)}.json"
    path.write_text(dump(art))
    proc = tool("check", "--kind", kind_flag or kind, str(path), "--run", str(B().run))
    try:
        reason = json.loads(proc.stdout.strip().splitlines()[-1]).get("reason") or ""
    except (ValueError, IndexError):
        reason = f"<unparsed: {proc.stdout!r} {proc.stderr!r}>"
    return proc.returncode, reason.lower()


def art(kind):
    return copy.deepcopy(B().artifacts[kind])


def jobs_by_role(a, role):
    return [j for j in a["jobs"] if j["role"] == role]


class Cell(unittest.TestCase):
    def fails(self, kind, a, needle, kind_flag=None):
        code, reason = check(kind, a, kind_flag)
        self.assertEqual(code, 1, reason)
        self.assertIn(needle, reason)

    def passes(self, kind):
        code, reason = check(kind, art(kind))
        self.assertEqual(code, 0, reason)


# ------------------------------------------------------------------------------------------------ A1
class A1Verify(Cell):
    def test_baseline_five_artifacts_and_receipt(self):
        b = B()
        self.assertEqual(b.proc.returncode, 0, b.proc.stderr)
        for kind in KINDS:
            self.assertTrue((b.run / "artifacts" / f"{kind}.json").is_file())
        self.assertEqual(sorted(e["kind"] for e in b.receipt["artifacts"]), sorted(KINDS))
        for kind in KINDS:
            self.passes(kind)
        proc = tool("verify-receipt", str(b.run / "receipt.json"))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_spec_command_twice_into_same_dir(self):
        out, procs = xdg_runs()
        for proc in procs:
            self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(len(list((out / "runs").iterdir())), 2)

    def test_requires_offline_and_writes_nothing(self):
        b = B()
        out = b.tmp / "refused-out"
        proc = b.run_script(["--out", str(out)])
        self.assertEqual(proc.returncode, 2)
        self.assertFalse(out.exists())

    def test_refuses_symlinked_out(self):
        b = B()
        real = b.tmp / "real-out"
        real.mkdir()
        link = b.tmp / "link-out"
        link.symlink_to(real)
        proc = b.run_script(["--offline", "--out", str(link)])
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(list(real.iterdir()), [])


# ------------------------------------------------------------------------------------------------ A2
class A2Debug(Cell):
    def repo(self):
        return B().repo("bug")

    def test_fresh_verifier_and_distinct_jobs(self):
        a = art("debug")
        builder, verifier = jobs_by_role(a, "builder")[0], jobs_by_role(a, "verifier")[0]
        self.assertNotEqual(builder["job_id"], verifier["job_id"])
        self.assertNotEqual(builder["pid"], verifier["pid"])
        self.assertNotEqual(builder["worktree"]["path"], verifier["worktree"]["path"])
        wt = B().run / verifier["worktree"]["path"]
        head = git(wt, "rev-parse", "HEAD").decode().strip()
        self.assertEqual(head, a["evidence"]["green_commit"])
        self.passes("debug")

    def test_red_commit_that_passes_fails_even_with_recorded_failure(self):
        a = art("debug")
        a["evidence"]["red_commit"] = a["evidence"]["green_commit"]
        self.assertEqual(a["evidence"]["recorded"]["red_exit"], 1)
        self.fails("debug", a, "red commit passes")

    def test_green_that_fails_is_caught_not_trusted(self):
        a = art("debug")
        a["evidence"]["green_commit"] = a["evidence"]["base_commit"]
        self.assertEqual(a["evidence"]["recorded"]["green_exit"], 0)
        code, reason = check("debug", a)
        self.assertEqual(code, 1, reason)

    def test_red_for_wrong_reason(self):
        a, repo = art("debug"), self.repo()
        e = a["evidence"]
        bad = b"import module_that_does_not_exist\n" + blob(repo, e["red_commit"], "check_slug.py")
        e["red_commit"] = variant(repo, e["base_commit"], e["red_commit"], {"check_slug.py": bad})
        self.fails("debug", a, "red for wrong reason")

    def test_test_file_changed_between_red_and_green(self):
        a, repo = art("debug"), self.repo()
        e = a["evidence"]
        weak = blob(repo, e["green_commit"], "check_slug.py") + b"\n# weakened\n"
        e["green_commit"] = variant(repo, e["green_commit"], e["green_commit"], {"check_slug.py": weak})
        self.fails("debug", a, "test changed")

    def test_undeclared_import_in_diff(self):
        a, repo = art("debug"), self.repo()
        e = a["evidence"]
        code = blob(repo, e["green_commit"], "slug.py")
        extra = b"try:\n    import yaml\nexcept ImportError:\n    yaml = None\n" + code
        e["green_commit"] = variant(repo, e["green_commit"], e["green_commit"], {"slug.py": extra})
        self.fails("debug", a, "undeclared import")
        e["new_dependencies"] = ["yaml"]
        code, reason = check("debug", a)
        self.assertNotIn("undeclared import", reason)

    def test_touched_file_outside_scope(self):
        a = art("debug")
        a["evidence"]["scope"] = ["slug.py"]
        self.fails("debug", a, "outside declared scope")

    def test_reuse_name_absent_or_unused(self):
        a = art("debug")
        a["evidence"]["reuse"] = ["textutil.no_such_helper"]
        self.fails("debug", a, "reuse name absent at base")
        a["evidence"]["reuse"] = ["textutil.title_case"]
        self.fails("debug", a, "reuse name unused in diff")

    def test_reproduce_record_must_show_symptom_at_base(self):
        a = art("debug")
        a["evidence"]["reproduce"]["symptom"] = "this-never-appears"
        self.fails("debug", a, "symptom not reproduced")

    def test_dependency_decision_required(self):
        a = art("debug")
        a["evidence"]["dependency_decisions"] = []
        self.fails("debug", a, "dependency decision")

    def test_verifier_stdout_sha_mismatch(self):
        a = art("debug")
        jobs_by_role(a, "verifier")[0]["stdout_sha256"] = "0" * 64
        self.fails("debug", a, "job record disagrees")

    def test_job_pid_disagreeing_with_result_files(self):
        a = art("debug")
        jobs_by_role(a, "builder")[0]["pid"] += 1
        self.fails("debug", a, "job record disagrees")

    def test_verifier_record_copied_from_builder(self):
        a = art("debug")
        a["jobs"][1] = copy.deepcopy(a["jobs"][0])
        self.fails("debug", a, "expected role verifier")

    def test_dirty_verifier_worktree(self):
        a = art("debug")
        wt = B().run / jobs_by_role(a, "verifier")[0]["worktree"]["path"]
        stray = wt / "stray.txt"
        stray.write_text("x")
        try:
            self.fails("debug", a, "verifier worktree dirty")
        finally:
            stray.unlink()

    def test_verifier_worktree_head_must_equal_green(self):
        a, repo = art("debug"), self.repo()
        e = a["evidence"]
        code = blob(repo, e["green_commit"], "slug.py") + b"# later\n"
        e["green_commit"] = variant(repo, e["green_commit"], e["green_commit"], {"slug.py": code})
        self.fails("debug", a, "verifier worktree head")


# ------------------------------------------------------------------------------------------------ A3
class A3Domain(Cell):
    def test_reproducible_contradiction(self):
        a = art("domain")
        f = a["evidence"]["finding"]
        self.assertNotEqual(f["expected"], f["observed"])
        self.passes("domain")

    def test_scenario_swapped_for_agreeing_one(self):
        a = art("domain")
        a["evidence"]["finding"]["scenario_id"] = "S2"
        self.fails("domain", a, "no contradiction reproduced")

    def test_missing_refuter(self):
        a = art("domain")
        e = a["evidence"]
        a["jobs"] = [j for j in a["jobs"] if j["job_id"] != e["refuter_job"]]
        self.fails("domain", a, "refuter job missing")

    def test_refuted_verdict(self):
        a = art("domain")
        a["evidence"]["verdict"] = "REFUTED"
        self.fails("domain", a, "refuter verdict")

    def test_improvement_spanning_two_files(self):
        a = art("domain")
        a["evidence"]["improvement"]["file"] = ["metrics.py", "glossary.md"]
        self.fails("domain", a, "exactly one file")

    def test_improvement_acceptance_check_missing(self):
        a = art("domain")
        del a["evidence"]["improvement"]["acceptance_check"]
        self.fails("domain", a, "acceptance check")

    def test_refuter_is_maker_job(self):
        a = art("domain")
        a["evidence"]["refuter_job"] = a["evidence"]["maker_job"]
        self.fails("domain", a, "refuter is the maker job")

    def test_refuter_bound_to_different_finding(self):
        a = art("domain")
        other = next(j for j in art("security-audit")["jobs"] if j["role"] == "refuter")
        a["jobs"] = [j for j in a["jobs"] if j["role"] != "refuter"] + [other]
        a["evidence"]["refuter_job"] = other["job_id"]
        self.fails("domain", a, "bound to a different finding")

    def test_file_line_must_contain_term(self):
        a = art("domain")
        a["evidence"]["finding"]["glossary_ref"]["line"] = 1
        self.fails("domain", a, "does not contain")
        a = art("domain")
        a["evidence"]["finding"]["code_ref"]["line"] = 1
        self.fails("domain", a, "does not contain")


# ------------------------------------------------------------------------------------------------ A4
class A4Secure(Cell):
    def repo(self):
        return B().repo("secure")

    def variant_art(self, mutate):
        a, repo = art("secure-build"), self.repo()
        e = a["evidence"]
        text = blob(repo, e["build_commit"], "fileserver.py").decode()
        e["build_commit"] = variant(repo, e["build_commit"], e["build_commit"], {"fileserver.py": mutate(text).encode()})
        return a

    @staticmethod
    def drop(marker):
        return lambda text: "".join(line for line in text.splitlines(True) if f"GUARD {marker}" not in line)

    def test_replays_every_attack_and_serves_benign(self):
        a = art("secure-build")
        self.assertGreaterEqual(len(a["evidence"]["threats"]), 3)
        self.assertEqual({t["id"] for t in a["evidence"]["threats"]}, {"traversal", "absolute", "symlink-escape"})
        self.passes("secure-build")

    def test_missing_symlink_guard_names_threat(self):
        self.fails("secure-build", self.variant_art(self.drop("symlink-escape")), "symlink-escape")

    def test_missing_traversal_guard_names_threat(self):
        self.fails("secure-build", self.variant_art(self.drop("traversal")), "traversal")

    def test_missing_absolute_guard_names_threat(self):
        self.fails("secure-build", self.variant_art(self.drop("absolute")), "absolute")

    def test_refuse_everything_is_not_a_secure_build(self):
        deny = lambda text: "class Refused(Exception):\n    pass\n\n\ndef read_file(base_dir, relative):\n    raise Refused('no')\n"
        self.fails("secure-build", self.variant_art(deny), "benign request not served")

    def test_base_that_already_refuses_is_not_a_construction(self):
        a = art("secure-build")
        a["evidence"]["base_commit"] = a["evidence"]["build_commit"]
        self.fails("secure-build", a, "base already")

    def test_security_reviewer_job_required(self):
        a = art("secure-build")
        a["jobs"] = [j for j in a["jobs"] if j["role"] != "security-reviewer"]
        self.fails("secure-build", a, "security-reviewer job missing")

    def test_review_verdict_bound_to_commit(self):
        a, repo = art("secure-build"), self.repo()
        e = a["evidence"]
        harmless = blob(repo, e["build_commit"], "fileserver.py") + b"# note\n"
        e["build_commit"] = variant(repo, e["build_commit"], e["build_commit"], {"fileserver.py": harmless})
        self.fails("secure-build", a, "not bound to the build commit")


# ------------------------------------------------------------------------------------------------ A5
class A5Audit(Cell):
    def finding(self, a, fid):
        return next(f for f in a["evidence"]["findings"] if f["id"] == fid)

    def test_confirmed_exploit_and_decoy_hardening(self):
        a = art("security-audit")
        statuses = {f["id"]: f["status"] for f in a["evidence"]["findings"]}
        self.assertEqual(sorted(statuses.values()), ["confirmed", "hardening"])
        self.assertEqual({"assets", "trust_boundaries", "inputs"}, set(a["evidence"]["map"]))
        self.passes("security-audit")

    def test_decoy_marked_confirmed(self):
        a = art("security-audit")
        decoy = next(f for f in a["evidence"]["findings"] if f["status"] == "hardening")
        decoy["status"] = "confirmed"
        self.fails("security-audit", a, f"confirmed finding {decoy['id'].lower()} canary not observed")

    def test_confirmed_without_evidence(self):
        a = art("security-audit")
        real = next(f for f in a["evidence"]["findings"] if f["status"] == "confirmed")
        del real["evidence_id"]
        self.fails("security-audit", a, "confirmed without evidence")

    def test_evidence_id_whose_replay_shows_no_canary(self):
        a = art("security-audit")
        real = next(f for f in a["evidence"]["findings"] if f["status"] == "confirmed")
        decoy = next(f for f in a["evidence"]["findings"] if f["status"] == "hardening")
        real["evidence_id"] = decoy["evidence_id"]
        self.fails("security-audit", a, "canary not observed")

    def test_finding_without_refuter_verdict(self):
        a = art("security-audit")
        a["evidence"]["findings"][0].pop("refuter_job")
        self.fails("security-audit", a, "refuter verdict")

    def test_empty_attack_scenario_is_no_finding(self):
        a = art("security-audit")
        a["evidence"]["findings"][0]["attack_scenario"] = "  "
        self.fails("security-audit", a, "empty attack scenario")

    def test_edited_observation_is_caught_by_replay(self):
        a = art("security-audit")
        real = next(f for f in a["evidence"]["findings"] if f["status"] == "confirmed")
        real["observed"] = {"canary_observed": False}
        code, reason = check("security-audit", a)
        self.assertEqual(code, 1, reason)


# ------------------------------------------------------------------------------------------------ A6
class A6Incident(Cell):
    def repo(self):
        return B().repo("incident")

    def test_remeasured_and_rolled_back(self):
        a = art("incident")
        e = a["evidence"]
        self.assertGreaterEqual(len(e["candidates"]), 2)
        self.assertEqual([m["outcome"] for m in e["measurements"]], ["failed", "succeeded"])
        self.passes("incident")

    def test_two_changes_in_one_measurement(self):
        a, repo = art("incident"), self.repo()
        e = a["evidence"]
        state = json.loads(blob(repo, e["base_commit"], "state.json"))
        state.update(workers=9, timeout_s=9)
        e["measurements"][0]["apply_commit"] = variant(
            repo, e["base_commit"], e["base_commit"], {"state.json": dump(state).encode()})
        self.fails("incident", a, "more than one change")

    def test_edited_after_metric(self):
        a = art("incident")
        a["evidence"]["measurements"][1]["after"] = 100
        self.fails("incident", a, "after metric")

    def test_change_after_two_failures(self):
        a = art("incident")
        ms = a["evidence"]["measurements"]
        ms.insert(1, copy.deepcopy(ms[0]))
        self.fails("incident", a, "change after two failures")

    def test_fewer_than_two_candidates(self):
        a = art("incident")
        a["evidence"]["candidates"] = a["evidence"]["candidates"][:1]
        self.fails("incident", a, "fewer than 2 candidates")

    def test_rollback_leaving_one_changed_byte(self):
        a, repo = art("incident"), self.repo()
        e = a["evidence"]
        m = e["measurements"][0]
        dirty = blob(repo, e["base_commit"], "state.json") + b" "
        m["rollback_commit"] = variant(repo, m["apply_commit"], e["base_commit"], {"state.json": dirty})
        self.fails("incident", a, "rollback")

    def test_tribune_ranking_job_required(self):
        a = art("incident")
        a["jobs"] = [j for j in a["jobs"] if j["role"] != "tribune"]
        self.fails("incident", a, "tribune")

    def test_fact_line_sha_mismatch(self):
        a = art("incident")
        a["evidence"]["facts"][0]["sha256"] = "0" * 64
        self.fails("incident", a, "fact line sha")

    def test_change_without_prerequisite_recheck(self):
        a = art("incident")
        del a["evidence"]["measurements"][0]["prerequisite_recheck"]
        self.fails("incident", a, "prerequisite")
        a = art("incident")
        a["evidence"]["measurements"][0]["prerequisite_recheck"]["state_sha256"] = "0" * 64
        self.fails("incident", a, "prerequisite")


# ------------------------------------------------------------------------------------------------ A7
def build_receipt(name, artifacts, drop=()):
    """Write alt artifacts + a receipt with every hash consistent; return the receipt path (run-relative root)."""
    b = B()
    alt = b.run / "alt" / name
    alt.mkdir(parents=True, exist_ok=True)
    entries = []
    for kind, a in artifacts.items():
        if kind in drop:
            continue
        path = alt / f"{kind}.json"
        path.write_text(dump(a))
        entries.append({"kind": kind, "journey_id": kind, "path": f"alt/{name}/{kind}.json",
                        "sha256": sha(path.read_bytes()), "stable_sha256": development_stable(a)})
    receipt = {"version": 1, "kind": "development-receipt", "artifacts": entries, "upstream": "not exercised"}
    receipt["self_sha256"] = sha(canon(receipt))
    path = alt / "receipt.json"
    path.write_text(dump(receipt))
    return path


def development_stable(a):
    from modules.development import development
    return development.stable_sha256(a)


def verify(path):
    proc = tool("verify-receipt", str(path), "--run", str(B().run))
    return proc.returncode, (proc.stdout + proc.stderr).lower()


class A7Separately(Cell):
    def all_arts(self):
        return {k: art(k) for k in KINDS}

    def test_three_distinct_artifacts(self):
        r = B().receipt["artifacts"]
        picked = [e for e in r if e["kind"] in ("secure-build", "security-audit", "incident")]
        self.assertEqual(len({e["sha256"] for e in picked}), 3)
        self.assertEqual(len({e["kind"] for e in picked}), 3)

    def test_rebuilt_receipt_verifies(self):
        code, out = verify(build_receipt("ok", self.all_arts()))
        self.assertEqual(code, 0, out)

    def test_one_artifact_removed(self):
        code, out = verify(build_receipt("drop", self.all_arts(), drop=("incident",)))
        self.assertEqual(code, 1, out)

    def test_two_journeys_pointing_at_one_artifact(self):
        arts = self.all_arts()
        arts["incident"] = copy.deepcopy(arts["security-audit"])
        code, out = verify(build_receipt("dup", arts))
        self.assertEqual(code, 1, out)

    def test_audit_artifact_checked_as_secure_build(self):
        self.fails("security-audit", art("security-audit"), "kind mismatch", kind_flag="secure-build")

    def test_shared_job_between_incident_and_audit(self):
        arts = self.all_arts()
        arts["incident"]["jobs"].append(copy.deepcopy(next(j for j in arts["security-audit"]["jobs"] if j["role"] == "refuter")))
        code, out = verify(build_receipt("sharedjob", arts))
        self.assertEqual(code, 1, out)
        self.assertIn("shared evidence", out)

    def test_shared_fixture_file_between_incident_and_audit(self):
        arts = self.all_arts()
        arts["incident"]["evidence"]["inputs"].append(copy.deepcopy(arts["security-audit"]["evidence"]["inputs"][0]))
        code, out = verify(build_receipt("sharedfile", arts))
        self.assertEqual(code, 1, out)
        self.assertIn("shared evidence", out)


# ------------------------------------------------------------------------------------------------ A8
class A8Labels(Cell):
    LABELS = {
        "route": {"route": {"result": "no-port", "intent": "security audit"}},
        "plan": {"plan": {"role": "security-reviewer", "seat": "x", "write": False}},
        "roles": {"intent_match": {"role": "security-reviewer", "intent": ["security", "vulnerability"]}},
        "trigger": {"trigger_text": "use vibesec when writing code"},
        "envelope": {"envelope": {"result": {"outcome": "succeeded", "evidence": []}}},
    }

    def test_label_only_evidence_cannot_close_43(self):
        for kind in ("secure-build", "security-audit", "incident"):
            for name, evidence in self.LABELS.items():
                a = art(kind)
                a["evidence"] = copy.deepcopy(evidence)
                with self.subTest(kind=kind, label=name):
                    self.fails(kind, a, "no-artifact-evidence")


# ------------------------------------------------------------------------------------------------ A9 / A10
def skills_dir(name, ids):
    d = B().tmp / name
    for i in ids:
        (d / i / "skills" / "x").mkdir(parents=True)
        (d / i / "skills" / "x" / "SKILL.md").write_text("MARKER-" + "SKILLTEXT claims every capability\n")
    return d


class A9Degrade(Cell):
    def test_deps_without_skills_dir(self):
        proc = tool("deps", "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        report = json.loads(proc.stdout)
        self.assertEqual(sorted(j["journey"] for j in report["journeys"]), sorted(KINDS))
        for journey in report["journeys"]:
            for dep in journey["dependencies"]:
                self.assertEqual(dep["state"], "unavailable")
                self.assertTrue(dep["reason"])
                self.assertEqual(dep["effect"], "generic contract only")

    def test_run_prints_one_degraded_line_per_journey(self):
        lines = [l for l in B().proc.stdout.splitlines() if l.startswith("DEGRADED: ")]
        self.assertEqual(len(lines), 5)
        self.assertEqual(sorted(l.split()[1] for l in lines), sorted(KINDS))
        for line in lines:
            self.assertRegex(line, r"^DEGRADED: \S+ upstream \S+ unavailable$")

    def test_receipt_says_not_exercised(self):
        self.assertEqual(B().receipt["upstream"], "not exercised")
        for kind in KINDS:
            self.assertEqual(B().artifacts[kind]["upstream"]["claim"], "not exercised")
            self.assertEqual(B().artifacts[kind]["upstream"]["state"], "unavailable")

    def test_matching_dir_for_unlicensed_slot_stays_unavailable(self):
        d = skills_dir("sk-slot", ["secure-new-code", "security-audit"])
        report = json.loads(tool("deps", "--skills-dir", str(d), "--json").stdout)
        for journey in report["journeys"]:
            for dep in journey["dependencies"]:
                self.assertEqual(dep["state"], "unavailable", dep)


class A10NoTriggerClaims(Cell):
    def test_present_skill_is_present_untested_only(self):
        d = skills_dir("sk-sp", ["superpowers"])
        report = json.loads(tool("deps", "--skills-dir", str(d), "--json").stdout)
        states = {dep["id"]: dep["state"] for j in report["journeys"] for dep in j["dependencies"]}
        self.assertEqual(states["superpowers"], "present-untested")
        self.assertEqual({v for k, v in states.items() if k != "superpowers"}, {"unavailable"})
        sp = next(dep for j in report["journeys"] for dep in j["dependencies"] if dep["id"] == "superpowers")
        self.assertEqual((sp["source"], sp["license"]), ("superpowers", "MIT"))

    def test_artifact_claiming_used_or_available_fails(self):
        for state in ("used", "available"):
            a = art("debug")
            a["upstream"]["state"] = state
            self.fails("debug", a, "upstream state")

    def test_skill_text_never_reaches_out(self):
        b = B()
        d = skills_dir("sk-run", ["superpowers"])
        out = b.tmp / "skills-out"
        proc = b.run_script(["--offline", "--out", str(out), "--skills-dir", str(d)])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("present-untested", json.loads((b.latest(out) / "artifacts" / "debug.json").read_text())["upstream"]["state"])
        for path in out.rglob("*"):
            if path.is_file() and ".git/" not in str(path):
                self.assertNotIn(b"SKILLTEXT", path.read_bytes(), str(path))


# ------------------------------------------------------------------------------------------------ A11
class A11Offline(Cell):
    def test_no_paid_or_network_binary_ran(self):
        b = B()
        self.assertEqual(sorted(p.name for p in b.sentinels.iterdir()), [], "a fake binary or hostile git hook ran")

    def test_every_seat_unpaid(self):
        b = B()
        configs = sorted((b.run / "config").glob("*.json"))
        self.assertTrue(configs)
        for cfg in configs:
            proc = subprocess.run([PY, "-B", str(DISPATCH), "status", "--config", str(cfg)], capture_output=True,
                                  text=True, env=b.env(), timeout=60)
            seats = json.loads(proc.stdout)["seats"]
            self.assertTrue(seats)
            for name, seat in seats.items():
                self.assertFalse(seat["paid"], (cfg.name, name))
                self.assertEqual(seat["harness"], "local-command")

    def test_forbidden_imports_and_binaries(self):
        banned = {"socket", "urllib", "http", "ssl", "requests"}
        names = {"claude", "opencode", "curl", "wget"}
        for base in (DEVDIR, EXDIR):
            for path in base.rglob("*.py"):
                tree = ast.parse(path.read_text(), str(path))
                for node in ast.walk(tree):
                    if isinstance(node, ast.Import):
                        mods = {n.name.split(".")[0] for n in node.names}
                    elif isinstance(node, ast.ImportFrom):
                        mods = {(node.module or "").split(".")[0]}
                    else:
                        mods = set()
                        if "tests" not in path.parts and isinstance(node, ast.Constant) and node.value in names:
                            self.fail(f"{path}: names {node.value}")
                    self.assertFalse(mods & banned, f"{path}: {mods & banned}")

    def test_stable_hash_equal_across_runs(self):
        b = B()
        out, procs = xdg_runs()
        mine = {e["kind"]: e["stable_sha256"] for e in b.receipt["artifacts"]}
        for run in sorted((out / "runs").iterdir()):
            other = json.loads((run / "receipt.json").read_text())
            self.assertEqual(mine, {e["kind"]: e["stable_sha256"] for e in other["artifacts"]})
        from modules.development import development
        for kind in KINDS:
            self.assertEqual(development.stable_sha256(b.artifacts[kind]), mine[kind])

    def test_stable_hash_exclusion_set_is_closed(self):
        from modules.development import development
        contract = json.loads((DEVDIR / "journeys.json").read_text())
        self.assertEqual(sorted(contract["stable_exclude"]),
                         sorted(["job_id", "pid", "started_at", "finished_at", "branch"]))
        a = art("security-audit")
        base = development.stable_sha256(a)
        for key in ("job_id", "pid", "started_at", "finished_at"):
            b2 = copy.deepcopy(a)
            b2["jobs"][0][key] = "changed" if key != "pid" else 1
            self.assertEqual(development.stable_sha256(b2), base, key)
        b2 = copy.deepcopy(a)
        b2["jobs"][0]["worktree"]["branch"] = "other"
        self.assertEqual(development.stable_sha256(b2), base)
        for mutate in (lambda x: x["evidence"]["findings"][0].__setitem__("status", "hardening"),
                       lambda x: x["jobs"][0].__setitem__("stdout_sha256", "1" * 64),
                       lambda x: x["jobs"][0].__setitem__("role", "builder"),
                       lambda x: x["upstream"].__setitem__("state", "present-untested"),
                       lambda x: x["evidence"]["map"].__setitem__("assets", ["z"])):
            b2 = copy.deepcopy(a)
            mutate(b2)
            self.assertNotEqual(development.stable_sha256(b2), base)


# ------------------------------------------------------------------------------------------------ A12
class A12Binding(Cell):
    def test_edited_value_with_receipt_self_hash_recomputed(self):
        arts = {k: art(k) for k in KINDS}
        arts["security-audit"]["evidence"]["findings"][0]["attack_scenario"] = "edited scenario text"
        path = build_receipt("tamper1", arts)
        receipt = json.loads(path.read_text())
        original = {e["kind"]: e for e in B().receipt["artifacts"]}
        for e in receipt["artifacts"]:  # the receipt still records the sha of the unedited artifact
            e["sha256"] = original[e["kind"]]["sha256"]
        receipt.pop("self_sha256")
        receipt["self_sha256"] = sha(canon(receipt))
        path.write_text(dump(receipt))
        code, out = verify(path)
        self.assertEqual(code, 1, out)

    def test_edited_evidence_with_every_hash_consistent(self):
        arts = {k: art(k) for k in KINDS}
        real = next(f for f in arts["security-audit"]["evidence"]["findings"] if f["status"] == "confirmed")
        real["observed"] = {"canary_observed": False}
        code, out = verify(build_receipt("tamper2", arts))
        self.assertEqual(code, 1, out)
        arts = {k: art(k) for k in KINDS}
        arts["incident"]["evidence"]["measurements"][1]["after"] = 100
        code, out = verify(build_receipt("tamper3", arts))
        self.assertEqual(code, 1, out)

    def test_receipt_self_hash_edit_detected(self):
        path = B().run / "alt" / "selfhash.json"
        receipt = copy.deepcopy(B().receipt)
        receipt["upstream"] = "exercised"
        path.write_text(dump(receipt))
        code, out = verify(path)
        self.assertEqual(code, 1, out)


# ------------------------------------------------------------------------------------------------ A13
class A13Constraints(Cell):
    def test_descriptor_valid(self):
        from modules.contracts.contracts import check_module_descriptor
        check_module_descriptor(DEVDIR, module="development")

    def test_privacy_scan_clean(self):
        from scripts import privacy_scan
        for base in (DEVDIR, EXDIR):
            self.assertEqual(privacy_scan.scan(base), [])

    def test_no_host_identity_in_artifacts_or_receipt(self):
        b = B()
        forbidden = [str(b.out.resolve()), str(b.run.resolve()), os.environ.get("HOME", ""), str(b.home),
                     getpass.getuser(), os.uname().nodename]
        files = [b.run / "receipt.json", *sorted((b.run / "artifacts").glob("*.json"))]
        self.assertEqual(len(files), 6)
        for path in files:
            text = path.read_text()
            for item in filter(None, forbidden):
                self.assertNotIn(item, text, f"{path.name} leaks {item!r}")

    def test_paths_in_artifacts_are_run_relative(self):
        for kind in KINDS:
            text = canon(B().artifacts[kind])
            self.assertNotIn('"/', text)

    def test_check_refuses_artifact_outside_run(self):
        outside = B().tmp / "elsewhere.json"
        outside.write_text(dump(art("debug")))
        proc = tool("check", "--kind", "debug", str(outside), "--run", str(B().run))
        self.assertEqual(proc.returncode, 2)

    def test_check_refuses_unknown_kind_and_malformed(self):
        self.assertEqual(tool("check", "--kind", "nonsense", str(B().run / "artifacts" / "debug.json"),
                              "--run", str(B().run)).returncode, 2)
        bad = B().run / "alt" / "malformed.json"
        bad.write_text("{not json")
        self.assertEqual(tool("check", "--kind", "debug", str(bad), "--run", str(B().run)).returncode, 2)

    def test_path_escape_inside_artifact_refused(self):
        a = art("debug")
        a["evidence"]["inputs"][0]["path"] = "../../etc/passwd"
        code, reason = check("debug", a)
        self.assertEqual(code, 2, reason)


if __name__ == "__main__":
    unittest.main()
