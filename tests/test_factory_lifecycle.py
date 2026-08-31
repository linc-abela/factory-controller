"""Focused coverage for the four-command Owner lifecycle."""

from __future__ import annotations

import json
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path

from factory_controller.adapter import HostCommandResult
from factory_controller.engine import Controller
from factory_controller.factory import FactoryConfig, FactoryLifecycle, OwnerIdentity
from factory_controller.store import MissionStore


PROTOTYPE_SHA = "229b923b050fe8a4450d5597d472157bd42c8647"
BUG_SHA = "4072bfd7c008d3b227e2e164ecbe6f58013c2733"


class NoopAdapter:
    def execute(self, step, operation_key, value):
        return {"status": "completed", "candidate_sha": "a" * 40}


class FakeHost:
    """A deterministic host and Bridge boundary for lifecycle tests."""

    def __init__(self, config):
        self.config = config
        self.installed = False
        self.containment = True
        self.primary_ready = True
        self.capacity_fresh = True
        self.source_drift = False
        self.capability_admitted = False
        self.loaded = {config.legacy_label}
        self.calls = []
        self.capability_admits = 0
        self.health_error = None
        self.observed_at = time.time()
        self.checkouts = {
            "factory-prototype-lab": "/labs/factory-prototype-lab",
            "factory-bug-lab": "/labs/factory-bug-lab",
        }

    def __call__(self, command, *, cwd=None, input_text=None,
                 timeout_seconds=300):
        command = tuple(command)
        self.calls.append((command, input_text))
        if command and command[0] == "launchctl":
            return self._launchctl(command)
        if command and command[0] == str(self.config.bridge_root / "dev"):
            return self._bridge(command[1:], input_text)
        if len(command) == 2 and command[1] == "health":
            if self.health_error is not None:
                return HostCommandResult(1, "", self.health_error)
            return HostCommandResult(0, json.dumps(
                {"status": "ok", "identity": Path(command[0]).parent.name}))
        return HostCommandResult(127, "", "unknown host command")

    def _launchctl(self, command):
        action = command[1]
        if action == "print":
            label = command[2].rsplit("/", 1)[-1]
            return HostCommandResult(0 if label in self.loaded else 1)
        if action == "bootout":
            self.loaded.discard(command[2].rsplit("/", 1)[-1])
            return HostCommandResult(0)
        if action == "bootstrap":
            self.loaded.add(Path(command[3]).stem)
            return HostCommandResult(0)
        return HostCommandResult(1, "", "unsupported launchctl action")

    def _bridge(self, arguments, input_text):
        if arguments == ("doctor",):
            return HostCommandResult(0, json.dumps(self.doctor()))
        if arguments == ("readiness",):
            doctor = self.doctor()
            return HostCommandResult(1, json.dumps({
                "schema_version": "factory.bridge.readiness_report.v1",
                "status": "blocked",
                "ready_profiles": ["codex-primary"] if self.primary_ready else [],
                "profiles": doctor["provider"]["profiles"],
                "capacity": {},
            }))
        if arguments == ("install", "--dry-run"):
            return HostCommandResult(0, json.dumps({"status": "planned"}))
        if arguments == ("install",):
            self.installed = True
            return HostCommandResult(0, json.dumps({"status": "installed"}))
        if arguments == ("capability", "preview", "-"):
            request = json.loads(input_text)
            return HostCommandResult(0, json.dumps({
                "schema_version": request["schema_version"],
                "request": request,
                "admissible": True,
                "applied": False,
                "after": {"capabilities": ["prototype", "bug"]},
            }))
        if arguments == ("capability", "admit", "-"):
            self.capability_admitted = True
            self.capability_admits += 1
            return HostCommandResult(0, json.dumps({"outcome": "admitted"}))
        if arguments in (("capacity", "observe", "codex-primary"),
                         ("capacity", "status", "codex-primary")):
            if not self.capacity_fresh:
                return HostCommandResult(1, json.dumps({"state": "absent"}))
            return HostCommandResult(0, json.dumps({
                "schema_version": "factory.bridge.capacity_observation.v1",
                "profile_id": "codex-primary",
                "state": "fresh",
                "classification": "available",
                "quota_state": "available",
                "observed_at": self.observed_at,
                "remaining_seconds": 3600,
                "stale_after_seconds": 3600,
                "source_ref": "fake-capacity-reading",
            }))
        return HostCommandResult(127, "", "unknown Bridge command")

    def doctor(self):
        source_sha = "a" * 40
        installed_sha = None if not self.installed else source_sha
        version_file = "not_installed" if not self.installed else source_sha
        if self.source_drift:
            installed_sha = "b" * 40
            version_file = installed_sha
        compatible = self.installed and not self.source_drift
        drift_fields = {
            "schema_drift": "none" if compatible else "not_installed",
            "source_drift": "none" if compatible else "differs",
            "version_drift": "none" if compatible else "differs",
            "code_drift": "none" if compatible else "not_installed",
            "source_code_drift": "none" if compatible else "not_installed",
            "provider_registry_drift": "none" if compatible else "not_installed",
            "capability_registry_drift": "none" if compatible else "not_installed",
        }
        expected_schemas = {"bridge": "v1"}
        return {
            "schema_version": "factory.bridge.doctor.v1",
            "service": {
                "plist_present": self.installed,
                "plist_path": str(self.config.bridge_plist),
                "socket_path": str(self.config.bridge_socket),
                "socket_present": self.config.bridge_label in self.loaded,
            },
            "source": {
                "sha": source_sha,
                "installed_sha": installed_sha,
                "version_file": version_file,
            },
            "compatibility": {
                "status": "compatible" if compatible else "not_installed",
                "fail_closed": not compatible,
                **drift_fields,
                "expected_schemas": expected_schemas,
                "installed_schemas": expected_schemas if compatible else None,
            },
            "registry": {"digest": "d" * 64, "projects": [
                {
                    "project_id": "factory-prototype-lab",
                    "repository_remote_url": "https://github.com/linc-abela/factory-prototype-lab.git",
                    "resolution": "resolved",
                    "capabilities": ["prototype"],
                    "checkout": self.checkouts["factory-prototype-lab"],
                },
                {
                    "project_id": "factory-bug-lab",
                    "repository_remote_url": "https://github.com/linc-abela/factory-bug-lab.git",
                    "resolution": "resolved",
                    "capabilities": ["bug"],
                    "checkout": self.checkouts["factory-bug-lab"],
                },
            ]},
            "registry_drift": "none" if self.installed else "not_applicable",
            "unresolved_projects": [],
            "capabilities": ["prototype", "bug"] if self.capability_admitted
            else ["prototype"],
            "capability_admissions": {
                "serving": ["prototype", "bug"] if self.capability_admitted
                else ["prototype"],
            },
            "provider": {"profiles": [{
                "profile_id": "codex-primary",
                "status": "available" if self.primary_ready else "unavailable",
                "readiness": "available" if self.primary_ready else "auth_required",
            }]},
            "containment": {"sandbox_exec_present": self.containment},
            "legacy": {"service_loaded": self.config.legacy_label in self.loaded},
        }


class FactoryLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        base = FactoryConfig.default()
        self.config = replace(
            base,
            agents_dir=root / "LaunchAgents",
            state_dir=root / "state",
            bridge_prefix=root / "bridge",
            capability_request_path=root / "first-dogfood-capability-admission-request.json",
        )
        self.config.capability_request_path.write_text(json.dumps({
            "accepted_unknowns": [],
            "capability": "bug",
            "policy_ref": "vault://active/software-factory/implementation/factory-first-dogfood-activation-adjudication.md#5",
            "profiles": ["codex-primary"],
            "projects": ["factory-bug-lab", "factory-prototype-lab"],
            "request_ref": "SF-145A-bug-capability",
            "schema_version": "factory.bridge.capability_admission_request.v1",
        }))
        self.host = FakeHost(self.config)
        store = MissionStore(root / "controller.db")
        self.lifecycle = FactoryLifecycle(
            Controller(store, NoopAdapter()),
            config=self.config,
            runner=self.host,
            owner=OwnerIdentity(501, "owner"),
            remote_reachability={
                "factory-prototype-lab": (PROTOTYPE_SHA,),
                "factory-bug-lab": (BUG_SHA,),
            },
            reports={
                "evidence_core": {"status": "ACCEPTED", "identity": "evidence"},
                "context_broker": {"status": "ok", "identity": "context"},
            },
        )

    def test_commands_are_idempotent_and_record_one_shift(self):
        installed = self.lifecycle.dispatch("install")
        self.assertTrue(installed.ok)
        self.assertEqual(installed.render().splitlines()[0], "FACTORY INSTALLED")

        ready = self.lifecycle.dispatch("start")
        self.assertTrue(ready.ok, ready.render())
        ready_again = self.lifecycle.dispatch("start")
        self.assertTrue(ready_again.ok, ready_again.render())
        self.assertEqual(self.host.capability_admits, 1)
        self.assertEqual(len(self.lifecycle.shift.grants()), 1)
        self.assertEqual(len(self.lifecycle.store.capacity_observations()), 1)

        status = self.lifecycle.dispatch("status")
        self.assertTrue(status.ok, status.render())
        self.assertIn("FACTORY READY", status.render())

        stopped = self.lifecycle.dispatch("stop")
        self.assertTrue(stopped.ok, stopped.render())
        stopped_again = self.lifecycle.dispatch("stop")
        self.assertTrue(stopped_again.ok, stopped_again.render())
        self.assertEqual(len(self.lifecycle.shift.grants()), 1)
        self.assertEqual(len(self.lifecycle.store.capacity_observations()), 1)
        self.assertIn("FACTORY OFF", stopped.render())
        self.assertIn(self.config.bridge_label, self.host.loaded)
        self.assertFalse(any(
            command[:2] == ("launchctl", "bootout")
            and command[2].endswith(self.config.bridge_label)
            for command, _ in self.host.calls))

        acts = [row for row in self.lifecycle.store.coordination()
                if row["reason"] == "FACTORY_OWNER_ACTION"]
        self.assertEqual([row["detail"]["action"] for row in acts],
                         ["install", "start", "start", "stop", "stop"])

    def test_start_repairs_partial_bridge_and_supervisor_activation(self):
        installed = self.lifecycle.dispatch("install")
        self.assertTrue(installed.ok, installed.render())
        self.host.loaded.discard(self.config.bridge_label)
        self.host.capability_admitted = False
        self.host.loaded.discard(self.config.supervisor_label)

        result = self.lifecycle.dispatch("start")

        self.assertTrue(result.ok, result.render())
        self.assertIn(self.config.bridge_label, self.host.loaded)
        self.assertIn(self.config.supervisor_label, self.host.loaded)
        self.assertEqual(self.host.capability_admits, 1)

    def test_missing_owner_identity_blocks_before_host_inspection(self):
        lifecycle = FactoryLifecycle(
            self.lifecycle.controller,
            config=self.config,
            runner=self.host,
            owner=OwnerIdentity(0, "root"),
        )

        result = lifecycle.dispatch("start")

        self.assertFalse(result.ok)
        self.assertIn("trusted local Owner identity", result.render())
        self.assertEqual(self.host.calls, [])

    def test_start_fails_closed_for_source_drift_or_missing_containment(self):
        self.assertTrue(self.lifecycle.dispatch("install").ok)

        self.host.source_drift = True
        drift = self.lifecycle.dispatch("start")
        self.assertFalse(drift.ok)
        self.assertIn("BRIDGE_SOURCE_DRIFT", drift.details["code"])
        self.assertIn("BRIDGE software has changed".lower(), drift.render().lower())

        self.host.source_drift = False
        self.host.containment = False
        blocked = self.lifecycle.dispatch("start")
        self.assertFalse(blocked.ok)
        self.assertIn("sandbox containment", blocked.render().lower())
        self.assertEqual(self.lifecycle.shift.grants(), [])

    def test_capacity_failure_does_not_admit_the_next_capability(self):
        self.assertTrue(self.lifecycle.dispatch("install").ok)
        self.host.capacity_fresh = False

        result = self.lifecycle.dispatch("start")

        self.assertFalse(result.ok)
        self.assertIn("capacity is unavailable", result.render().lower())
        self.assertEqual(self.host.capability_admits, 0)
        self.assertEqual(self.lifecycle.shift.grants(), [])

    def test_optional_profiles_do_not_gate_a_ready_primary(self):
        result = self.lifecycle.dispatch("install")
        self.assertTrue(result.ok, result.render())
        result = self.lifecycle.dispatch("start")
        self.assertTrue(result.ok, result.render())
        self.assertEqual(self.host.capability_admits, 1)

        self.host.primary_ready = False
        self.lifecycle.shift.revoke(
            self.lifecycle.shift.grant().request_ref,
            reason="test cleanup", actor="owner")
        self.lifecycle.supervisor.transition(
            "stopped", actor="owner", reason="test cleanup")
        blocked = self.lifecycle.dispatch("start")
        self.assertFalse(blocked.ok)
        self.assertIn("unavailable", blocked.render().lower())

    def _lifecycle_reading_real_health(self):
        """The same lifecycle, but collecting health from the sibling repos."""

        return FactoryLifecycle(
            self.lifecycle.controller,
            config=self.config,
            runner=self.host,
            owner=OwnerIdentity(501, "owner"),
            remote_reachability={
                "factory-prototype-lab": (PROTOTYPE_SHA,),
                "factory-bug-lab": (BUG_SHA,),
            },
        )

    def test_a_stopped_container_runtime_is_named_instead_of_a_dead_end(self):
        self.assertTrue(self.lifecycle.dispatch("install").ok)
        lifecycle = self._lifecycle_reading_real_health()
        self.host.health_error = (
            "failed to connect to the docker API at "
            "unix:///Users/owner/.orbstack/run/docker.sock; check if the path "
            "is correct and if the daemon is running")

        result = lifecycle.dispatch("start")

        self.assertFalse(result.ok)
        self.assertEqual("PREFLIGHT_NOT_READY", result.details["code"])
        self.assertIn("container runtime is not running", result.render())
        self.assertIn("Evidence Core and Context Broker", result.render())
        self.assertEqual(lifecycle.shift.grants(), [])

    def test_an_unhealthy_service_is_named_without_blaming_the_runtime(self):
        self.assertTrue(self.lifecycle.dispatch("install").ok)
        lifecycle = self._lifecycle_reading_real_health()
        self.host.health_error = "broker health check failed"

        result = lifecycle.dispatch("start")

        self.assertFalse(result.ok)
        self.assertIn("health could not be read", result.render())
        self.assertNotIn("container runtime", result.render())

    def test_collected_health_reaches_the_preflight(self):
        self.assertTrue(self.lifecycle.dispatch("install").ok)
        lifecycle = self._lifecycle_reading_real_health()

        result = lifecycle.dispatch("start")

        self.assertTrue(result.ok, result.render())
        self.assertEqual({}, lifecycle.report_failures)

    def test_an_unmapped_readiness_failure_names_its_own_check(self):
        message = self.lifecycle._plain_preflight_blocker(
            ({"check": "SOMETHING_ELSE"},))

        self.assertIn("SOMETHING_ELSE", message)

    def test_inconsistent_state_points_at_the_command_that_recovers_it(self):
        self.assertTrue(self.lifecycle.dispatch("install").ok)
        self.assertTrue(self.lifecycle.dispatch("start").ok)
        self.host.loaded.discard(self.config.supervisor_label)

        status = self.lifecycle.dispatch("status")

        self.assertFalse(status.ok)
        self.assertEqual("INCONSISTENT_SERVICE_STATE", status.details["code"])
        self.assertIn("./dev factory stop", status.render())

    def test_registry_shape_adapter_accepts_list_and_keyed_projects(self):
        listed = FactoryLifecycle._registry_rows({
            "registry": {"projects": [{"project_id": "listed"}]}})
        keyed = FactoryLifecycle._registry_rows({
            "registry": {"projects": {"keyed": {"resolution": "resolved"}}}})
        entries = FactoryLifecycle._registry_rows({
            "registry": {"entries": {"entry": {"project_id": "entry"}}}})

        self.assertEqual(("listed",), tuple(row["project_id"] for row in listed))
        self.assertEqual(("keyed",), tuple(row["project_id"] for row in keyed))
        self.assertEqual(("entry",), tuple(row["project_id"] for row in entries))


if __name__ == "__main__":
    unittest.main()
