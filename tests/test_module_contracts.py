import hashlib
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from modules.contracts.contracts import (
    CONTRACTS,
    ROOT,
    check_contracts,
    check_evidence,
    check_module_descriptor,
    execute_adapter,
)


class ModuleContractTests(unittest.TestCase):
    def copied_contracts(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        target = Path(temporary.name) / "contracts"
        shutil.copytree(CONTRACTS, target)
        return target

    def change(self, directory, filename, mutate):
        path = directory / filename
        data = json.loads(path.read_text(encoding="utf-8"))
        mutate(data)
        path.write_text(json.dumps(data), encoding="utf-8")

    def test_contracts_cover_registry_and_define_core_protocol(self):
        entries = check_contracts()
        self.assertEqual(len(entries), 72)
        self.assertEqual(
            {entry["id"] for entry in entries}, {f"{n:02d}" for n in range(1, 73)}
        )
        protocol = json.loads((CONTRACTS / "adapter_protocol.json").read_text())
        self.assertEqual(protocol["adapter"]["required"], ["describe", "handle"])
        self.assertIn("unsupported-operation", protocol["compatibility"]["rejects"])

    def test_overclaimed_support_without_evidence_is_rejected(self):
        directory = self.copied_contracts()
        self.change(
            directory,
            "descriptor.json",
            lambda data: data.update(claimed_support="available"),
        )
        with self.assertRaisesRegex(ValueError, "overclaimed support"):
            check_contracts(directory)

    def test_evidence_requires_linked_native_execution_records(self):
        directory = self.copied_contracts()
        self.change(
            directory,
            "descriptor.json",
            lambda data: data.update(
                claimed_support="available",
                evidence=[],
            ),
        )
        with self.assertRaisesRegex(ValueError, "overclaimed support"):
            check_contracts(directory)
        self.change(
            directory,
            "descriptor.json",
            lambda data: data.update(
                evidence=[
                    {
                        "class": "fixture",
                        "claim": "available",
                        "event": {
                            "event_id": "1",
                            "kind": "support-claim",
                            "occurred_at": "now",
                            "subject": "core-contracts",
                        },
                        "result": {
                            "event_id": "1",
                            "outcome": "succeeded",
                            "evidence": [],
                            "finished_at": "now",
                        },
                        "status": {
                            "subject": "core-contracts",
                            "state": "available",
                            "observed_at": "now",
                            "reason": "fixture",
                        },
                    }
                ]
            ),
        )
        with self.assertRaisesRegex(ValueError, "without native evidence"):
            check_contracts(directory)

    def test_unsafe_dependency_spec_is_rejected(self):
        directory = self.copied_contracts()
        self.change(
            directory,
            "descriptor.json",
            lambda data: data["dependencies"].append(
                {"identifier": "file:local", "version": "1.0.0", "status": "tested"}
            ),
        )
        with self.assertRaisesRegex(ValueError, "unsafe dependency identifier"):
            check_contracts(directory)

    def test_dependencies_use_named_fields_and_portable_identifiers(self):
        directory = self.copied_contracts()
        self.change(
            directory,
            "descriptor.json",
            lambda data: data["dependencies"].append(
                {
                    "status": "declared-untested",
                    "version": "1.0.0",
                    "identifier": "portable.package",
                }
            ),
        )
        self.assertEqual(len(check_contracts(directory)), 72)
        self.change(
            directory,
            "descriptor.json",
            lambda data: data["dependencies"].append(
                {"identifier": "-option", "version": "1.0.0", "status": "tested"}
            ),
        )
        with self.assertRaisesRegex(ValueError, "unsafe dependency identifier"):
            check_contracts(directory)

    def test_unknown_license_cannot_be_redistributable(self):
        directory = self.copied_contracts()
        self.change(
            directory,
            "sources.json",
            lambda data: data["sources"][5].update(redistribution="not-vendored"),
        )
        with self.assertRaisesRegex(ValueError, "unknown license must block"):
            check_contracts(directory)

    def test_missing_registry_link_is_rejected(self):
        directory = self.copied_contracts()
        self.change(
            directory,
            "mapping.json",
            lambda data: data["capabilities"][0].update(registry_module="missing"),
        )
        with self.assertRaisesRegex(ValueError, "unresolved registry link"):
            check_contracts(directory, ROOT / "capabilities" / "registry.json")

    def test_typed_schema_protocol_and_staged_handshakes_are_checked(self):
        directory = self.copied_contracts()
        self.change(
            directory,
            "example.json",
            lambda data: data["status"].update(state="invented"),
        )
        with self.assertRaisesRegex(ValueError, "invalid status state"):
            check_contracts(directory)
        self.change(
            directory,
            "example.json",
            lambda data: data["status"].update(state="planned"),
        )
        self.change(
            directory,
            "adapter_protocol.json",
            lambda data: data["adapter"].update(protocol_version=2),
        )
        with self.assertRaisesRegex(ValueError, "invalid adapter protocol"):
            check_contracts(directory)
        self.change(
            directory,
            "adapter_protocol.json",
            lambda data: data["adapter"].update(protocol_version=1),
        )
        self.change(
            directory,
            "mapping.json",
            lambda data: data["staged"][0].update(blocked_until="none"),
        )
        with self.assertRaisesRegex(ValueError, "invalid staged association"):
            check_contracts(directory)

    def test_unknown_schema_types_and_malformed_evidence_are_rejected(self):
        directory = self.copied_contracts()
        self.change(
            directory,
            "schemas.json",
            lambda data: data["event"]["types"].update(event_id="uuid"),
        )
        with self.assertRaisesRegex(ValueError, "unsupported event schema type"):
            check_contracts(directory)
        self.change(
            directory,
            "schemas.json",
            lambda data: data["event"]["types"].update(event_id="string"),
        )
        self.change(
            directory,
            "example.json",
            lambda data: data["result"].update(evidence=["not-a-record"]),
        )
        with self.assertRaisesRegex(ValueError, "invalid result evidence"):
            check_contracts(directory)

    def test_generic_adapter_executes_only_compatible_operations(self):
        class Adapter:
            def describe(self):
                return "described"

            def handle(self):
                return "handled"

        adapter = Adapter()
        self.assertEqual(execute_adapter(adapter, "describe", 1, []), "described")
        self.assertEqual(
            execute_adapter(
                adapter,
                "handle",
                1,
                [{"identifier": "portable", "version": "1.0.0", "status": "tested"}],
                "portable",
            ),
            "handled",
        )
        with self.assertRaisesRegex(ValueError, "unknown schema"):
            execute_adapter(adapter, "describe", 2, [])
        with self.assertRaisesRegex(ValueError, "undeclared dependency"):
            execute_adapter(adapter, "handle", 1, [], "missing")
        with self.assertRaisesRegex(ValueError, "unsupported operation"):
            execute_adapter(adapter, "delete", 1, [])

    def test_companion_delivery_requires_a_nonempty_handshake(self):
        directory = self.copied_contracts()
        self.change(
            directory,
            "mapping.json",
            lambda data: data["handshakes"][0]["capabilities"].remove("64"),
        )
        with self.assertRaisesRegex(ValueError, "missing handshake"):
            check_contracts(directory)

    # --- native evidence, receipts, sources, staged coverage ---

    def native_descriptor(self, directory, receipt_overrides=None, mutate_member=None):
        receipt = {
            "schema": 1,
            "class": "native",
            "module": "core-contracts",
            "claim": "available",
            "event_id": "ev-1",
            "harness": "claude-code",
            "harness_version": "1.0.0",
            "command": "run-check",
            "exit_code": 0,
            "started_at": "2026-10-30T00:00:00Z",
            "finished_at": "2026-10-30T00:00:01Z",
            "stdout_sha256": "0" * 64,
            "dependencies": [{"identifier": "portable.package", "version": "1.0.0"}],
        }
        receipt.update(receipt_overrides or {})
        raw = json.dumps(receipt).encode()
        (directory / "receipt.json").write_bytes(raw)
        member = {
            "kind": "execution-receipt",
            "reference": "receipt.json",
            "sha256": hashlib.sha256(raw).hexdigest(),
        }
        if mutate_member:
            mutate_member(member)
        evidence = {
            "class": "native",
            "claim": "available",
            "event": {
                "event_id": "ev-1",
                "kind": "support-claim",
                "occurred_at": "now",
                "subject": "core-contracts",
            },
            "result": {
                "event_id": "ev-1",
                "outcome": "succeeded",
                "evidence": [member],
                "finished_at": "now",
            },
            "status": {
                "subject": "core-contracts",
                "state": "available",
                "observed_at": "now",
                "reason": "receipt",
            },
        }
        self.change(
            directory,
            "descriptor.json",
            lambda data: data.update(claimed_support="available", evidence=[evidence]),
        )

    def test_valid_receipt_admits_available_and_tested_dependency(self):
        directory = self.copied_contracts()
        self.native_descriptor(directory)
        self.assertEqual(len(check_contracts(directory)), 72)
        self.change(
            directory,
            "descriptor.json",
            lambda data: data["dependencies"].append(
                {"identifier": "portable.package", "version": "1.0.0", "status": "tested"}
            ),
        )
        self.assertEqual(len(check_contracts(directory)), 72)

    def test_relabelled_fixture_with_empty_evidence_is_rejected(self):
        directory = self.copied_contracts()
        self.native_descriptor(directory)
        self.change(
            directory,
            "descriptor.json",
            lambda data: data["evidence"][0]["result"].update(evidence=[]),
        )
        with self.assertRaisesRegex(ValueError, "requires an execution receipt"):
            check_contracts(directory)

    def test_receipt_path_forms_are_refused(self):
        outside = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, outside, True)
        (outside / "r.json").write_text("{}")
        cases = {
            "absolute": (str(outside / "r.json"), "unsafe receipt reference"),
            "traversal": ("../receipt.json", "unsafe receipt reference"),
            "missing": ("nope.json", "missing receipt file"),
            "symlink": ("link.json", "unsafe receipt reference"),
        }
        for name, (reference, message) in cases.items():
            with self.subTest(name):
                directory = self.copied_contracts()
                self.native_descriptor(
                    directory,
                    mutate_member=lambda m, r=reference: m.update(reference=r),
                )
                if name == "symlink":
                    os.symlink(outside / "r.json", directory / "link.json")
                with self.assertRaisesRegex(ValueError, message):
                    check_contracts(directory)

    def test_receipt_content_is_checked(self):
        directory = self.copied_contracts()
        self.native_descriptor(
            directory, mutate_member=lambda m: m.update(sha256="1" * 64)
        )
        with self.assertRaisesRegex(ValueError, "sha256 mismatch"):
            check_contracts(directory)
        for overrides, message in (
            ({"exit_code": 1}, "exit_code must be 0"),
            ({"event_id": "other"}, "disagrees with evidence"),
            ({"extra": "x"}, "fields mismatch"),
        ):
            with self.subTest(overrides):
                directory = self.copied_contracts()
                self.native_descriptor(directory, overrides)
                with self.assertRaisesRegex(ValueError, message):
                    check_contracts(directory)

    def test_tested_dependency_requires_receipt(self):
        directory = self.copied_contracts()
        self.change(
            directory,
            "descriptor.json",
            lambda data: data["dependencies"].append(
                {"identifier": "portable.package", "version": "1.0.0", "status": "tested"}
            ),
        )
        with self.assertRaisesRegex(ValueError, "tested dependency without receipt"):
            check_contracts(directory)

    def test_staged_coverage_cannot_be_emptied(self):
        directory = self.copied_contracts()
        self.change(directory, "mapping.json", lambda data: data.update(staged=[]))
        with self.assertRaisesRegex(ValueError, "nonempty association"):
            check_contracts(directory)
        directory = self.copied_contracts()
        self.change(
            directory,
            "mapping.json",
            lambda data: data["staged"][0].update(source="superpowers"),
        )
        with self.assertRaisesRegex(ValueError, "invalid staged association"):
            check_contracts(directory)

    def test_source_inventory_is_exact(self):
        directory = self.copied_contracts()
        self.change(directory, "sources.json", lambda data: data["sources"].pop(0))
        with self.assertRaisesRegex(ValueError, "exactly once"):
            check_contracts(directory)
        directory = self.copied_contracts()
        self.change(
            directory,
            "sources.json",
            lambda data: data["sources"].append(dict(data["sources"][0])),
        )
        with self.assertRaisesRegex(ValueError, "exactly once"):
            check_contracts(directory)

    def test_source_dispositions_are_restricted(self):
        for index, changes, message in (
            (1, {"version": "CONFLICT:6.3.0|7.0.0"}, "single pin"),
            (2, {"redistribution": "not-vendored"}, "restricted license must block"),
            (0, {"redistribution": "cleared"}, "invalid source redistribution"),
            (1, {"revision": "abc"}, "invalid source revision"),
        ):
            with self.subTest(changes):
                directory = self.copied_contracts()
                self.change(
                    directory,
                    "sources.json",
                    lambda data, i=index, c=changes: data["sources"][i].update(c),
                )
                with self.assertRaisesRegex(ValueError, message):
                    check_contracts(directory)

    # --- closing fixes: allowlisted pins, unique IDs, registry evidence ---

    def test_source_versions_are_an_allowlist(self):
        for version in (
            "CONFLICT",
            "conflict:6.3.0|7.0.0",
            "unknown",
            " UNKNOWN",
            "6.3.0|7.0.0",
            "latest",
        ):
            with self.subTest(version):
                directory = self.copied_contracts()
                self.change(
                    directory,
                    "sources.json",
                    lambda data, v=version: data["sources"][1].update(version=v),
                )
                with self.assertRaisesRegex(ValueError, "single pin"):
                    check_contracts(directory)
        for version in ("1.2.3", "1.2.3-rc.1+build", "ca45fc496a34"):
            with self.subTest(version):
                directory = self.copied_contracts()
                self.change(
                    directory,
                    "sources.json",
                    lambda data, v=version: data["sources"][1].update(version=v),
                )
                self.assertEqual(len(check_contracts(directory)), 72)

    def test_staged_and_none_versions_need_matching_dispositions(self):
        for index, changes, message in (
            (1, {"version": "STAGED"}, "STAGED version requires staged"),
            (1, {"version": "NONE"}, "NONE version requires excluded"),
            (5, {"version": "1.0.0"}, "STAGED version requires staged"),
            (4, {"version": "1.0.0"}, "NONE version requires excluded"),
        ):
            with self.subTest(changes, index=index):
                directory = self.copied_contracts()
                self.change(
                    directory,
                    "sources.json",
                    lambda data, i=index, c=changes: data["sources"][i].update(c),
                )
                with self.assertRaisesRegex(ValueError, message):
                    check_contracts(directory)

    def test_invalid_source_disposition_state_is_refused(self):
        directory = self.copied_contracts()
        self.change(
            directory,
            "sources.json",
            lambda data: data["sources"][1].update(disposition="bogus"),
        )
        with self.assertRaisesRegex(ValueError, "invalid source disposition state"):
            check_contracts(directory)

    def test_unresolved_epic_infographics_is_excluded_not_pinned(self):
        sources = json.loads((CONTRACTS / "sources.json").read_text())["sources"]
        epic = next(s for s in sources if s["identifier"] == "epic-infographics")
        self.assertEqual(epic["origin"], "none")
        self.assertEqual(epic["version"], "NONE")
        self.assertEqual(epic["disposition"], "excluded")
        self.assertEqual(epic["redistribution"], "blocked")
        self.assertEqual(epic["license"], "MIT-package-only")

    def test_duplicate_mapping_id_is_refused(self):
        directory = self.copied_contracts()
        self.change(
            directory,
            "mapping.json",
            lambda data: data["capabilities"].append(dict(data["capabilities"][0])),
        )
        with self.assertRaisesRegex(ValueError, "duplicate mapping ID"):
            check_contracts(directory)

    def test_registry_support_without_evidence_is_refused(self):
        for support in ("available", "partial"):
            with self.subTest(support):
                directory = self.copied_contracts()
                registry = json.loads(
                    (ROOT / "capabilities" / "registry.json").read_text()
                )
                registry["capabilities"][0].update(
                    support=support, evidence_class="none"
                )
                path = directory / "registry.json"
                path.write_text(json.dumps(registry), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "registry support without"):
                    check_contracts(directory, path)

    def rewrite_receipt(self, raw):
        directory = self.copied_contracts()
        self.native_descriptor(
            directory,
            mutate_member=lambda m: m.update(sha256=hashlib.sha256(raw).hexdigest()),
        )
        (directory / "receipt.json").write_bytes(raw)
        return directory

    def test_malformed_receipts_are_refused(self):
        for raw in (b"not json", b"\xff\xfe"):
            with self.subTest(raw):
                with self.assertRaisesRegex(ValueError, "invalid receipt JSON"):
                    check_contracts(self.rewrite_receipt(raw))
        for overrides, message in (
            ({"schema": 2}, "invalid receipt identity"),
            ({"class": "fixture"}, "invalid receipt identity"),
            ({"harness_version": "  "}, "invalid receipt harness_version"),
            ({"command": 5}, "invalid receipt command"),
            ({"harness": "other"}, "invalid receipt harness$"),
            ({"stdout_sha256": "xyz"}, "invalid receipt stdout_sha256"),
            ({"dependencies": "x"}, "invalid receipt dependencies"),
            (
                {"dependencies": [{"identifier": "a", "version": "latest"}]},
                "invalid receipt dependencies",
            ),
            (
                {"dependencies": [{"identifier": "-a", "version": "1.0.0"}]},
                "invalid receipt dependencies",
            ),
        ):
            with self.subTest(overrides):
                directory = self.copied_contracts()
                self.native_descriptor(directory, overrides)
                with self.assertRaisesRegex(ValueError, message):
                    check_contracts(directory)

    # --- U02 hardening residue ---

    def test_handshake_capability_must_have_companion_delivery(self):
        directory = self.copied_contracts()
        self.change(
            directory,
            "mapping.json",
            lambda data: data["handshakes"][0]["capabilities"].append("01"),
        )
        with self.assertRaisesRegex(ValueError, "handshake capability lacks companion"):
            check_contracts(directory)

    def test_license_values_are_validated(self):
        for license_value in ("mit or whatever", "/etc/passwd", "unknown", "proprietary"):
            with self.subTest(license_value):
                directory = self.copied_contracts()
                self.change(
                    directory,
                    "sources.json",
                    lambda d, v=license_value: d["sources"][0].update(license=v),
                )
                with self.assertRaisesRegex(ValueError, "invalid source license"):
                    check_contracts(directory)

    def test_origin_values_are_validated(self):
        for origin in (
            "/etc/passwd",
            "~/private/repo",
            "../escape",
            "C:\\work\\repo",
            "file:///tmp/x",
            "a b",
            "https://",
            "/",
        ):
            with self.subTest(origin):
                directory = self.copied_contracts()
                self.change(
                    directory,
                    "sources.json",
                    lambda d, v=origin: d["sources"][0].update(origin=v),
                )
                with self.assertRaisesRegex(ValueError, "invalid source origin"):
                    check_contracts(directory)

    def test_receipt_symlink_loop_is_a_clean_error(self):
        directory = self.copied_contracts()
        self.native_descriptor(
            directory, mutate_member=lambda m: m.update(reference="loop.json")
        )
        os.symlink("loop.json", directory / "loop.json")
        with self.assertRaisesRegex(ValueError, "receipt file"):
            check_contracts(directory)

    def test_wrong_shape_descriptor_fields_are_clean_errors(self):
        cases = (
            ("claimed_support", ["planned"]),
            ("evidence", {"a": 1}),
            ("dependencies", "x"),
            ("schemas", 3),
            ("version", [1]),
            ("module", {}),
        )
        for field, value in cases:
            with self.subTest(field):
                directory = self.copied_contracts()
                self.change(
                    directory,
                    "descriptor.json",
                    lambda d, f=field, v=value: d.update({f: v}),
                )
                with self.assertRaises(ValueError):
                    check_contracts(directory)
        directory = self.copied_contracts()
        self.native_descriptor(directory)
        self.change(
            directory,
            "descriptor.json",
            lambda d: d["evidence"][0].update({"class": ["native"]}),
        )
        with self.assertRaises(ValueError):
            check_contracts(directory)
        directory = self.copied_contracts()
        self.native_descriptor(directory, {"harness": ["claude-code"]})
        with self.assertRaises(ValueError):
            check_contracts(directory)
        directory = self.copied_contracts()
        self.change(
            directory,
            "mapping.json",
            lambda d: d["capabilities"][0].update(delivery_type=[["core"]]),
        )
        with self.assertRaises(ValueError):
            check_contracts(directory)

    def test_receipt_timestamps_must_be_ordered_and_valid(self):
        for overrides, message in (
            (
                {
                    "started_at": "2026-10-30T00:00:05Z",
                    "finished_at": "2026-10-30T00:00:01Z",
                },
                "timestamps out of order",
            ),
            ({"started_at": "yesterday"}, "invalid receipt timestamp"),
            ({"finished_at": "2026-10-30T00:00:01"}, "invalid receipt timestamp"),
        ):
            with self.subTest(overrides):
                directory = self.copied_contracts()
                self.native_descriptor(directory, overrides)
                with self.assertRaisesRegex(ValueError, message):
                    check_contracts(directory)
        directory = self.copied_contracts()
        self.native_descriptor(
            directory,
            {"started_at": "2026-10-30T00:00:01Z", "finished_at": "2026-10-30T00:00:01Z"},
        )
        self.assertEqual(len(check_contracts(directory)), 72)

    def test_duplicate_json_keys_are_refused(self):
        directory = self.copied_contracts()
        path = directory / "descriptor.json"
        text = path.read_text(encoding="utf-8")
        path.write_text(text.replace('"version": 1,', '"version": 1, "version": 1,', 1))
        with self.assertRaisesRegex(ValueError, "duplicate JSON key"):
            check_contracts(directory)
        directory = self.copied_contracts()
        self.native_descriptor(directory)
        raw = (directory / "receipt.json").read_bytes()
        raw = raw.replace(b'"schema": 1,', b'"schema": 1, "schema": 1,', 1)
        directory = self.rewrite_receipt(raw)
        with self.assertRaisesRegex(ValueError, "duplicate JSON key"):
            check_contracts(directory)

    # --- non-core module descriptors ---

    def module_dir(self, mutate=None):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        target = Path(temporary.name) / "orchestration"
        target.mkdir()
        descriptor = {
            "version": 1,
            "module": "orchestration",
            "claimed_support": "planned",
            "evidence": [],
            "dependencies": [
                {"identifier": "core-contracts", "version": "1.0.0", "status": "declared-untested"}
            ],
            "schemas": ["result"],
        }
        if mutate:
            mutate(descriptor)
        (target / "descriptor.json").write_text(json.dumps(descriptor), encoding="utf-8")
        return target

    def native_evidence(self, directory, subject="orchestration", status_subject=None, receipt_module="orchestration"):
        receipt = {
            "schema": 1, "class": "native", "module": receipt_module, "claim": "available", "event_id": "ev-9",
            "harness": "opencode", "harness_version": "1.0.0", "command": "dispatch run", "exit_code": 0,
            "started_at": "2026-10-30T00:00:00Z", "finished_at": "2026-10-30T00:00:01Z",
            "stdout_sha256": "0" * 64, "dependencies": [],
        }
        raw = json.dumps(receipt).encode()
        (directory / "receipt.json").write_bytes(raw)
        member = {"kind": "execution-receipt", "reference": "receipt.json", "sha256": hashlib.sha256(raw).hexdigest()}
        return [{
            "class": "native", "claim": "available",
            "event": {"event_id": "ev-9", "kind": "support-claim", "occurred_at": "now", "subject": subject},
            "result": {"event_id": "ev-9", "outcome": "succeeded", "evidence": [member], "finished_at": "now"},
            "status": {"subject": status_subject or subject, "state": "available", "observed_at": "now", "reason": "receipt"},
        }]

    def test_module_descriptor_planned_passes_and_default_module_unchanged(self):
        directory = self.module_dir()
        self.assertEqual(check_module_descriptor(directory, module="orchestration")["module"], "orchestration")
        with self.assertRaisesRegex(ValueError, "invalid descriptor identity"):
            check_module_descriptor(directory, module="other")
        with self.assertRaisesRegex(ValueError, "invalid module name"):
            check_module_descriptor(directory, module="core-contracts")
        self.assertEqual(len(check_contracts()), 72)

    def test_native_receipt_for_the_module_passes(self):
        directory = self.module_dir()
        evidence = self.native_evidence(directory)
        self.assertEqual(check_evidence(directory, evidence, "available", "orchestration"), set())
        directory = self.module_dir(lambda d: d.update(claimed_support="available"))
        evidence = self.native_evidence(directory)
        self.change(directory, "descriptor.json", lambda d: d.update(evidence=evidence))
        self.assertEqual(check_module_descriptor(directory, module="orchestration")["claimed_support"], "available")

    def test_core_subject_or_mismatched_receipt_module_fails_for_a_module(self):
        directory = self.module_dir()
        for label, kwargs in {
            "event subject core-contracts": dict(subject="core-contracts", status_subject="orchestration"),
            "status subject core-contracts": dict(subject="orchestration", status_subject="core-contracts"),
            "both core-contracts": dict(subject="core-contracts"),
            "receipt module core-contracts": dict(receipt_module="core-contracts"),
            "receipt module other": dict(receipt_module="other-module"),
        }.items():
            with self.subTest(label):
                evidence = self.native_evidence(directory, **kwargs)
                with self.assertRaisesRegex(ValueError, "unlinked or fabricated|receipt disagrees"):
                    check_evidence(directory, evidence, "available", "orchestration")

    def test_core_default_still_rejects_other_subjects(self):
        directory = self.module_dir()
        evidence = self.native_evidence(directory)
        with self.assertRaisesRegex(ValueError, "unlinked or fabricated"):
            check_evidence(directory, evidence, "available")

    def test_module_schema_inventory_is_checked(self):
        for schemas in (["nope"], ["result", "result"], "result", [["result"]], [1]):
            with self.subTest(schemas=schemas):
                directory = self.module_dir(lambda d: d.update(schemas=schemas))
                with self.assertRaisesRegex(ValueError, "module schema inventory mismatch|malformed"):
                    check_module_descriptor(directory, module="orchestration")
        directory = self.module_dir(lambda d: d.update(schemas=["event", "result", "status"]))
        check_module_descriptor(directory, module="orchestration")

    def test_module_descriptor_shape_and_dependency_errors(self):
        for label, mutate, pattern in (
            ("extra field", lambda d: d.update(extra=1), "descriptor fields mismatch"),
            ("bad support", lambda d: d.update(claimed_support="great"), "invalid descriptor support"),
            ("overclaim", lambda d: d.update(claimed_support="available"), "overclaimed support"),
            ("unsafe dependency", lambda d: d.update(dependencies=[{"identifier": "-x", "version": "1.0.0", "status": "tested"}]), "unsafe dependency identifier"),
            ("tested without receipt", lambda d: d.update(dependencies=[{"identifier": "core-contracts", "version": "1.0.0", "status": "tested"}]), "without receipt"),
            ("dependencies not list", lambda d: d.update(dependencies={}), "invalid descriptor dependencies"),
        ):
            with self.subTest(label):
                directory = self.module_dir(mutate)
                with self.assertRaisesRegex(ValueError, pattern):
                    check_module_descriptor(directory, module="orchestration")


if __name__ == "__main__":
    unittest.main()
