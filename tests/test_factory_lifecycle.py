"""Focused coverage for the four-command Owner lifecycle."""

from __future__ import annotations

import json
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path

from factory_controller import context_adapter
from factory_controller.adapter import HostCommandResult
from factory_controller.engine import Controller
from factory_controller.factory import FactoryConfig, FactoryLifecycle, OwnerIdentity
from factory_controller.store import MissionStore


PROTOTYPE_SHA = "229b923b050fe8a4450d5597d472157bd42c8647"
BUG_SHA = "4072bfd7c008d3b227e2e164ecbe6f58013c2733"


def fake_context(wire):
    """Keep lifecycle tests deterministic without bypassing the context seam."""

    selected = list(dict.fromkeys(wire.get("required_anchors") or []))
    manifest = context_adapter.evidence_core_manifest(wire, selected)
    return {
        "status": "built",
        "manifest": manifest,
        "receipt": {
            "schema_version": "1.0",
            "context_manifest_hash": manifest["manifest_hash"],
            "selected_refs": selected,
            "excluded_refs": [],
            "mandatory_fact_coverage": selected,
            "refusal_code": None,
        },
        "measurement": {
            "baseline_context_bytes": 1,
            "baseline_context_files": 1,
            "selected_context_bytes": 1,
            "selected_context_files": len(selected),
            "cache_state": "miss",
            "cache_identity": "f" * 64,
            "head_sha": wire.get("baseline_sha"),
            "repository_remote_url": wire.get("repository_remote_url"),
        },
    }


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
        # Profiles whose runtime reports a reading that is not usable. Per
        # profile because a contract that declares a failover has to be
        # testable against one constrained runtime and one that is not.
        self.capacity_constrained = set()
        self.source_drift = False
        self.capability_admitted = False
        # What the execution layer has been told to serve, per profile.
        self.capability_admissions = []
        self.serving_drift = "none"
        self.loaded = {config.legacy_label}
        self.calls = []
        self.capability_admits = 0
        self.health_error = None
        self.observed_at = time.time()
        self.checkouts = {
            "factory-prototype-lab": "/labs/factory-prototype-lab",
            "factory-bug-lab": "/labs/factory-bug-lab",
        }
        # Registered projects and provider profiles beyond the frozen dogfood
        # pair.  Empty by default so every existing test sees the host it saw
        # before; the product path fills them, because a product project is
        # exactly what the internal portfolio does not have.
        self.extra_projects = []
        self.extra_profiles = []
        # What the revision seam was asked for, and what it may refuse with.
        self.revision_requests = []
        self.revision_error = None
        # Where the execution layer keeps one checkout per opened base.
        self.revision_root = "/state/revisions"
        # What the prototype lab's own gates say at the frozen baseline,
        # copied from a real run of them: two tests, five of five labels
        # linked, no false matches.  The improvement slot measures its
        # baseline by running these, so a fake that answered nothing would
        # make every measurement `not_measurable` and prove nothing.
        self.baseline_ok = True
        self.gate_streams = {
            "test": ("", "test_x (t.T) ... ok\n\n"
                         "----------\nRan 2 tests in 0.001s\n\nOK\n"),
            "check": ("", ""),
            "evaluate": (json.dumps({"correct": 5, "false_matches": 0,
                                     "proceed": True, "total": 5}), ""),
        }

    def __call__(self, command, *, cwd=None, input_text=None,
                 timeout_seconds=300):
        command = tuple(command)
        self.calls.append((command, input_text))
        if command and command[0] == "launchctl":
            return self._launchctl(command)
        if command and command[0] == str(self.config.bridge_root / "dev"):
            return self._bridge(command[1:], input_text)
        if command[:1] == ("git",):
            return self._git(command)
        if len(command) == 2 and command[1].split("/")[-1] in self.gate_streams \
                and command[0].endswith("/dev"):
            if not self.baseline_ok:
                return HostCommandResult(1, "", "gate failed")
            stdout, stderr = self.gate_streams[command[1]]
            return HostCommandResult(0, stdout, stderr)
        if len(command) == 2 and command[1] == "health":
            if self.health_error is not None:
                return HostCommandResult(1, "", self.health_error)
            return HostCommandResult(0, json.dumps(
                {"status": "ok", "identity": Path(command[0]).parent.name}))
        return HostCommandResult(127, "", "unknown host command")

    def _git(self, command):
        """Only the two worktree verbs the measurement seam materializes with."""

        if "worktree" in command and "add" in command:
            target = Path(command[command.index("--detach") + 1])
            target.mkdir(parents=True, exist_ok=True)
            (target / "dev").write_text("#!/bin/sh\n")
            return HostCommandResult(0)
        if "worktree" in command and "remove" in command:
            return HostCommandResult(0)
        return HostCommandResult(127, "", "unknown git command")

    def _launchctl(self, command):
        action = command[1]
        if action == "print":
            label = command[2].rsplit("/", 1)[-1]
            return HostCommandResult(0 if label in self.loaded else 1)
        if action == "bootout":
            self.loaded.discard(command[2].rsplit("/", 1)[-1])
            return HostCommandResult(0)
        if action == "bootstrap":
            label = Path(command[3]).stem
            self.loaded.add(label)
            # A restarted Bridge binds the files that are there now, which is
            # the whole point of restarting it.
            if label == self.config.bridge_label:
                self.serving_drift = "none"
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
            request = json.loads(input_text)
            self.capability_admitted = True
            self.capability_admits += 1
            self.capability_admissions = [
                row for row in self.capability_admissions
                if row["capability"] != request["capability"]]
            self.capability_admissions.append({
                "capability": request["capability"],
                "profiles": list(request.get("profiles") or ()),
                "projects": list(request.get("projects") or ())})
            return HostCommandResult(0, json.dumps({"outcome": "admitted"}))
        if arguments[:1] == ("capacity",) and arguments[1:2] in (
                ("observe",), ("status",)) and arguments[2:3] and (
                arguments[2] == "codex-primary"
                or arguments[2] in {row["profile_id"] for row in self.extra_profiles}):
            if not self.capacity_fresh:
                return HostCommandResult(1, json.dumps({"state": "absent"}))
            constrained = arguments[2] in self.capacity_constrained
            return HostCommandResult(0, json.dumps({
                "schema_version": "factory.bridge.capacity_observation.v1",
                "profile_id": arguments[2],
                "state": "fresh",
                "classification": "available",
                "quota_state": "exhausted" if constrained else "available",
                "observed_at": self.observed_at,
                "remaining_seconds": 3600,
                "stale_after_seconds": 3600,
                "source_ref": "fake-capacity-reading",
            }))
        if arguments[:2] == ("artifact", "build"):
            return self._artifact(arguments)
        if arguments[:2] == ("revision", "base"):
            return self._revision(arguments)
        return HostCommandResult(127, "", "unknown Bridge command")

    def _revision(self, arguments):
        """A derived commit id, because the Controller must not mint one.

        The real module commits the predecessor's mission statement with the
        caller's text appended.  What matters on this side of the seam is that
        the id is a function of the predecessor and the text, so a repeated
        Owner command lands on one base -- the same property the real one has.
        """

        import hashlib

        project_id, predecessor = arguments[2], arguments[3]
        ref = arguments[arguments.index("--ref") + 1]
        path = arguments[arguments.index("--mission-file") + 1]
        if project_id not in self.checkouts:
            return HostCommandResult(1, "", "unknown project")
        if self.revision_error is not None:
            return HostCommandResult(2, json.dumps(self.revision_error), "")
        addendum = Path(path).read_text()
        self.revision_requests.append(
            {"predecessor_sha": predecessor, "ref": ref, "addendum": addendum})
        seed = ("%s|%s" % (predecessor, addendum)).encode()
        return HostCommandResult(0, json.dumps({
            "schema_version": "factory.bridge.revision_base.v1",
            "project_id": project_id,
            "repository_remote_url":
                "https://github.com/linc-abela/%s.git" % project_id,
            "predecessor_sha": predecessor,
            "revision_sha": hashlib.sha1(seed).hexdigest(),
            "ref": ref, "mission_path": arguments[
                arguments.index("--mission-path") + 1],
            "mission_digest": "sha256:" + hashlib.sha256(
                addendum.encode()).hexdigest(),
            "mission_bytes": len(addendum.encode()),
            "created": True,
            # The checkout the base is grounded on. It is a different local
            # copy from the registered one on purpose: the registered checkout
            # is on the product branch and the base is on no branch, so a fake
            # that returned the registered path would hide the whole seam.
            "revision_checkout": "%s/%s/%s" % (
                self.revision_root, project_id, hashlib.sha1(seed).hexdigest()),
            "revision_checkout_created": True,
        }))

    def _artifact(self, arguments):
        """A real archive, because the review path really unpacks one.

        Returning only a digest would leave `_materialize_review` untested
        against anything, and the bytes an Owner reviews are the whole point of
        the identity.
        """

        import hashlib
        import io
        import tarfile

        project_id, candidate = arguments[2], arguments[3]
        prefix = arguments[arguments.index("--prefix") + 1].rstrip("/") + "/"
        if project_id not in self.checkouts:
            return HostCommandResult(1, "", "unknown project")
        root = self.config.state_dir / "fake-artifacts"
        root.mkdir(parents=True, exist_ok=True)
        body = b"<!doctype html><title>%s</title>\n" % candidate.encode()
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w") as archive:
            info = tarfile.TarInfo(prefix + "index.html")
            info.size = len(body)
            archive.addfile(info, io.BytesIO(body))
        raw = buffer.getvalue()
        identity = hashlib.sha256(raw).hexdigest()
        path = root / (identity + ".tar")
        path.write_bytes(raw)
        return HostCommandResult(0, json.dumps({
            "schema_version": "factory.bridge.candidate_artifact.v1",
            "project_id": project_id, "candidate_sha": candidate,
            "publish_prefix": prefix,
            "artifact": {"kind": "static-bundle",
                         "identity": "sha256:" + identity},
            "archive_path": str(path), "archive_bytes": len(raw),
            "content_bytes": len(body), "file_count": 1,
            "files": [prefix + "index.html"],
            "repository_remote_url":
                "https://github.com/linc-abela/%s.git" % project_id,
        }))

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
                *self.extra_projects,
            ]},
            "registry_drift": "none" if self.installed else "not_applicable",
            "serving_drift": self.serving_drift,
            "unresolved_projects": [],
            "capabilities": ["prototype", "bug"] if self.capability_admitted
            else ["prototype"],
            "capability_admissions": {
                # Derived from the admissions, as the real report is: a
                # capability is served because something admitted it.
                "serving": (["prototype"] + [
                    row["capability"] for row in self.capability_admissions]
                    if self.capability_admissions
                    else ["prototype", "bug"] if self.capability_admitted
                    else ["prototype"]),
                # Per-profile, as the real report is: an admission names the
                # runtimes it widened, and a second declared runtime is only
                # served once one of these rows says so.
                "admissions": list(self.capability_admissions),
            },
            "provider": {"profiles": [{
                "profile_id": "codex-primary",
                "status": "available" if self.primary_ready else "unavailable",
                "readiness": "available" if self.primary_ready else "auth_required",
            }, *self.extra_profiles]},
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
            context_builder=fake_context,
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
        # The Bridge is never left down, and it is only ever taken down for a
        # reason the Owner's own command created.  It used to be that the
        # lifecycle never touched a healthy Bridge at all -- which is how
        # `start` came to widen a capability the running service could not see:
        # an admission is an overlay the Bridge reads once, at start, so
        # applying one and not reloading records a posture nobody serves.
        bootouts = [command for command, _ in self.host.calls
                    if command[:2] == ("launchctl", "bootout")
                    and command[2].endswith(self.config.bridge_label)]
        self.assertEqual(len(bootouts), 1)
        self.assertEqual(self.host.capability_admits, 1)

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
            context_builder=fake_context,
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

    def test_the_supervisor_job_names_the_path_it_runs_under(self):
        """The SF-157 root cause: an inherited PATH is not a declared one.

        launchd hands a job that names no PATH the bare
        ``/usr/bin:/bin:/usr/sbin:/sbin``.  Under it the labs' containerised
        evaluators exit 127 and the provider CLI resolves to nothing -- so a
        healthy mission was recorded as failing its own gates and a signed-in
        provider was reported as needing sign-in.  Neither said "environment".
        """

        self.assertTrue(self.lifecycle.dispatch("install").ok)
        plan = self.lifecycle._service_plan()

        entries = dict(plan.environment)["PATH"].split(":")

        for directory in ("/usr/local/bin", "/opt/homebrew/bin", "/usr/bin",
                          "/bin", "/usr/sbin", "/sbin"):
            self.assertIn(directory, entries)
        self.assertEqual(entries[0], str(Path(plan.interpreter).parent))
        self.assertIn(str(Path.home() / ".local" / "bin"), entries)
        self.assertEqual(len(entries), len(set(entries)))

    def test_the_installed_definition_carries_that_path_and_is_drift_checked(self):
        self.assertTrue(self.lifecycle.dispatch("install").ok)
        plan = self.lifecycle._service_plan()
        body = Path(plan.definition_path).read_text()

        self.assertIn("<key>PATH</key>", body)
        self.assertIn(dict(plan.environment)["PATH"], body)
        # The environment is inside the plan digest, so a job installed
        # without it reads as drift rather than as a job that merely exists.
        stripped = replace(plan, environment=tuple(
            item for item in plan.environment if item[0] != "PATH"))
        self.assertNotEqual(stripped.digest, plan.digest)

    def test_a_stale_serving_bridge_is_reloaded_rather_than_refused(self):
        """SF-157: the Bridge reads its registries once, at start.

        An install or an admission rewrites those files under a service that
        keeps answering from what it read.  Every diagnostic is a fresh process
        that re-reads them, so the tooling and the server disagree in the one
        direction nobody checks -- live, that was a mission refused
        `UNSUPPORTED_CAPABILITY` for a capability `doctor` reported as served.
        A stale posture is a restart, not an install, and not an Owner errand.
        """

        self.assertTrue(self.lifecycle.dispatch("install").ok)
        self.host.serving_drift = (
            "the running service bound capabilities before the current file(s)")

        started = self.lifecycle.dispatch("start")

        self.assertTrue(started.ok, started.render())
        self.assertIn(self.config.bridge_label, self.host.loaded)
        self.assertTrue(any(
            command[:2] == ("launchctl", "bootout")
            and command[2].endswith(self.config.bridge_label)
            for command, _ in self.host.calls))

    def test_a_stale_serving_bridge_is_named_in_the_status_the_owner_reads(self):
        self.assertTrue(self.lifecycle.dispatch("install").ok)
        self.assertTrue(self.lifecycle.dispatch("start").ok)
        self.host.serving_drift = "the running service bound an older posture"

        status = self.lifecycle.dispatch("status")

        self.assertIn("Needs attention", status.render())
        code, detail = self.lifecycle._bridge_problem(self.host.doctor())
        self.assertEqual(code, "BRIDGE_SERVING_DRIFT")
        self.assertIn("older configuration", detail)

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
