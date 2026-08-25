"""Twenty-mission multi-provider unattended campaign and adversarial verification for Stage 3."""

from __future__ import annotations

import hashlib
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any

from factory_controller.engine import Controller, RetryPolicy
from factory_controller.store import MissionStore
from tests.support import ALPHA, BETA, LayerAdapter, ProcessDeath, RouteTestCase, mission_payload


class TwentyMissionMultiProviderCampaignTests(unittest.TestCase):
    """Executes 20 unattended missions across multiple provider profiles."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "twenty_mission_campaign.db"
        self.store = MissionStore(self.db_path)

        # Multi-profile adapter supporting ALPHA and BETA
        self.adapter = MultiProfileTestAdapter(
            unavailable_by_mission={
                # Missions 5, 8, 17 simulate ALPHA being unavailable pre-dispatch -> fallback to BETA
                "SF-135-CAMPAIGN-05": {ALPHA},
                "SF-135-CAMPAIGN-08": {ALPHA},
                "SF-135-CAMPAIGN-17": {ALPHA},
                # Mission 14 simulates BETA being unavailable pre-dispatch -> fallback to ALPHA
                "SF-135-CAMPAIGN-14": {BETA},
            }
        )
        self.controller = Controller(
            self.store, self.adapter, retry_policy=RetryPolicy(max_attempts=3, base_delay_seconds=0)
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_twenty_unattended_multi_provider_missions(self) -> None:
        """Run 20 consecutive unattended missions exercising direct selection, preference order, and safe fallback."""
        submitted_missions = []
        expected_profiles = {}

        # 1. Submit 20 missions with varying profile candidates and policies
        for i in range(1, 21):
            work_item_id = f"SF-135-CAMPAIGN-{i:02d}"
            # Vary profile candidates:
            # 1-10: ALPHA preferred, BETA fallback
            # 11-15: BETA preferred, ALPHA fallback
            # 16-20: ALPHA preferred, BETA fallback with budget ceilings
            if 1 <= i <= 10:
                candidates = [
                    {"profile": ALPHA, "capabilities": ["prototype"]},
                    {"profile": BETA, "capabilities": ["prototype"]},
                ]
                policy = {"max_route_legs": 3}
                expected = BETA if i in (5, 8) else ALPHA
            elif 11 <= i <= 15:
                candidates = [
                    {"profile": BETA, "capabilities": ["prototype"]},
                    {"profile": ALPHA, "capabilities": ["prototype"]},
                ]
                policy = {"max_route_legs": 3}
                expected = ALPHA if i == 14 else BETA
            else:
                candidates = [
                    {"profile": ALPHA, "capabilities": ["prototype"]},
                    {"profile": BETA, "capabilities": ["prototype"]},
                ]
                policy = {"budget_ceiling": 50.0, "budget_currency": "USD", "max_route_legs": 3}
                expected = BETA if i == 17 else ALPHA

            expected_profiles[work_item_id] = expected
            payload = {
                "work_item_id": work_item_id,
                "execution_mode": "fixture",
                "repository_remote_url": f"https://github.com/linc-abela/disposable-lab-{i:02d}.git",
                "acceptance_gate_ids": [f"GATE-{i:02d}"],
                "provider_candidates": candidates,
                "execution_policy": policy,
            }
            m, created = self.controller.submit(payload, f"campaign:multi:key:{i:02d}")
            self.assertTrue(created)
            self.assertEqual(m["state"], "admitted")
            submitted_missions.append(m)

        self.assertEqual(len(submitted_missions), 20)
        self.assertEqual(self.store.counts().get("admitted"), 20)

        # 2. Execute all 20 unattended without human intervention
        completed_results = []
        for i in range(20):
            worker_id = f"unattended-worker-{(i % 4) + 1}"
            res = self.controller.work_once(worker_id)
            self.assertIsNotNone(res)
            self.assertEqual(res["state"], "completed", f"Mission {i+1} failed: {res.get('terminal_reason')}")
            completed_results.append(res)

        # 3. Verify ledger state post-campaign
        counts = self.store.counts()
        self.assertEqual(counts.get("completed"), 20)
        self.assertIsNone(counts.get("admitted"))
        self.assertIsNone(counts.get("dispatching"))
        self.assertIsNone(counts.get("escalated"))
        self.assertIsNone(counts.get("failed"))
        self.assertIsNone(counts.get("blocked"))

        # 4. Verify candidate SHAs, evidence pointers, route history and telemetry for each
        seen_candidates = set()
        seen_evidence = set()
        profile_counts = {ALPHA: 0, BETA: 0}

        for res in completed_results:
            m_id = res["id"]
            payload = self.store.get(m_id)["payload"]
            work_item_id = payload["work_item_id"]
            expected_prof = expected_profiles[work_item_id]

            # Verify Candidate SHA is unique and valid
            cand_sha = res["result"]["dispatch"]["candidate_sha"]
            self.assertIsNotNone(cand_sha)
            self.assertEqual(len(cand_sha), 40)
            self.assertNotIn(cand_sha, seen_candidates)
            seen_candidates.add(cand_sha)

            # Verify Evidence Pointer is unique and valid
            ev_ptr = res["result"]["evidence"]["evidence_pointer"]
            self.assertIsNotNone(ev_ptr)
            self.assertNotIn(ev_ptr, seen_evidence)
            seen_evidence.add(ev_ptr)

            # Check Route History
            route = self.store.route_history(m_id)
            selected_prof = route["selected_provider_profile"]
            self.assertEqual(selected_prof, expected_prof)
            profile_counts[selected_prof] += 1

            if work_item_id in ("SF-135-CAMPAIGN-05", "SF-135-CAMPAIGN-08", "SF-135-CAMPAIGN-17"):
                self.assertEqual(route["fallback_count"], 1)
                self.assertEqual(route["legs"][0]["provider_profile"], ALPHA)
                self.assertEqual(route["legs"][0]["outcome"], "provider_unavailable")
                self.assertEqual(route["legs"][1]["provider_profile"], BETA)
                self.assertEqual(route["legs"][1]["outcome"], "completed")
            elif work_item_id == "SF-135-CAMPAIGN-14":
                self.assertEqual(route["fallback_count"], 1)
                self.assertEqual(route["legs"][0]["provider_profile"], BETA)
                self.assertEqual(route["legs"][0]["outcome"], "provider_unavailable")
                self.assertEqual(route["legs"][1]["provider_profile"], ALPHA)
                self.assertEqual(route["legs"][1]["outcome"], "completed")
            else:
                self.assertEqual(route["fallback_count"], 0)
                self.assertEqual(route["legs"][0]["provider_profile"], expected_prof)
                self.assertEqual(route["legs"][0]["outcome"], "completed")

            # Check Telemetry
            telemetry = self.store.telemetry(m_id)
            self.assertEqual(telemetry["outcome"], "completed")
            self.assertEqual(telemetry["provider_profile"], expected_prof)
            self.assertFalse(telemetry["owner_intervention"])

        # Check that both profiles ran successfully in the 20-mission campaign
        self.assertGreater(profile_counts[ALPHA], 0)
        self.assertGreater(profile_counts[BETA], 0)
        self.assertEqual(profile_counts[ALPHA] + profile_counts[BETA], 20)

        # 5. Subsequent work_once finds queue empty
        self.assertIsNone(self.controller.work_once("idle-worker"))


class MultiProfileTestAdapter:
    """Mock execution adapter serving distinct profiles with configurable availability."""

    def __init__(self, unavailable_by_mission: dict[str, set[str]] | None = None) -> None:
        self.unavailable_by_mission = unavailable_by_mission or {}
        self.call_log: list[dict[str, Any]] = []

    def execute(self, step: str, operation_key: str, value: dict[str, Any]) -> dict[str, Any]:
        self.call_log.append({"step": step, "key": operation_key, "value": value})
        mission = value.get("mission") or {}
        work_item_id = mission.get("work_item_id", "unknown")

        if step == "dispatch":
            route = value["route"]
            profile = route.get("provider_profile")
            unavail = self.unavailable_by_mission.get(work_item_id, set())

            receipt = {
                "provider_profile": profile,
                "provider": f"{profile}/v1",
                "execution_mode": "fixture",
                "idempotency_key": route.get("idempotency_key"),
                "duration_ms": 15,
                "usage": {"cost_amount": 1.25, "cost_currency": "USD", "cost_state": "reported"},
            }

            if profile in unavail:
                return {
                    "status": "provider_unavailable",
                    "diagnostic": "UNAVAILABLE_PRE_DISPATCH",
                    "receipt": {**receipt, "process_started": False, "refusal_code": "UNAVAILABLE_PRE_DISPATCH"},
                }

            cand_sha = hashlib.sha1(f"{work_item_id}:{profile}:{operation_key}".encode()).hexdigest()
            return {
                "status": "completed",
                "candidate_sha": cand_sha,
                "execution_id": operation_key,
                "receipt": {**receipt, "process_started": True},
            }

        if step == "verify":
            cand = value.get("dispatch", {}).get("candidate_sha")
            return {"verified": True, "evaluator": "local-safe-verifier", "candidate_sha": cand}

        if step == "evaluate":
            gates = mission.get("acceptance_gate_ids") or ["DEFAULT"]
            outcomes = [{"gate_id": g, "passed": True, "detail": "simulated multi-profile"} for g in gates]
            return {"passed": True, "gate_outcomes": outcomes}

        if step == "evidence":
            ptr = hashlib.sha256(f"evidence:{work_item_id}:{operation_key}".encode()).hexdigest()
            return {"accepted": True, "evidence_pointer": ptr, "evidence_class": "rederived"}

        return {"status": "unknown"}


class AdversarialMultiProviderSafetyTests(RouteTestCase, unittest.TestCase):
    """Adversarial fault-injection and boundary tests for multi-provider runtime."""

    def test_post_spawn_timeout_strictly_refuses_second_provider(self):
        """Once provider ALPHA spawns and times out, BETA must NOT be invoked."""
        class TimeoutPostSpawnAdapter(LayerAdapter):
            def __init__(self):
                super().__init__()
                self.spawned_profiles = []

            def _dispatch(self, operation_key, value):
                route = value["route"]
                profile = route["provider_profile"]
                self.spawned_profiles.append(profile)
                receipt = {
                    "provider_profile": profile,
                    "provider": f"{profile}/v1",
                    "execution_mode": "fixture",
                    "idempotency_key": route["idempotency_key"],
                    "process_started": True,  # Process DID start!
                    "duration_ms": 1000,
                }
                # Return failure after process started
                return {
                    "status": "refused",
                    "diagnostic": "ADAPTER_TIMEOUT",
                    "receipt": {**receipt, "refusal_code": "ADAPTER_TIMEOUT"},
                }

        adapter = TimeoutPostSpawnAdapter()
        controller, store, _ = self.build(adapter)
        mission, _ = controller.submit(
            mission_payload(provider_candidates=[{"profile": ALPHA}, {"profile": BETA}]),
            "safety:timeout:1",
        )
        result = controller.work_once("w1")
        self.assertEqual(result["state"], "refused")
        # Exactly one profile was attempted before fail-closed refusal
        self.assertEqual(adapter.spawned_profiles, [ALPHA])

    def test_post_spawn_nonzero_exit_strictly_refuses_second_provider(self):
        """Once provider ALPHA spawns and exits non-zero, BETA must NOT be invoked."""
        class NonZeroAdapter(LayerAdapter):
            def __init__(self):
                super().__init__()
                self.spawned_profiles = []

            def _dispatch(self, operation_key, value):
                route = value["route"]
                profile = route["provider_profile"]
                self.spawned_profiles.append(profile)
                receipt = {
                    "provider_profile": profile,
                    "provider": f"{profile}/v1",
                    "execution_mode": "fixture",
                    "idempotency_key": route["idempotency_key"],
                    "process_started": True,
                    "duration_ms": 50,
                }
                return {
                    "status": "refused",
                    "diagnostic": "NON_ZERO_EXIT_7",
                    "receipt": {**receipt, "refusal_code": "NON_ZERO_EXIT"},
                }

        adapter = NonZeroAdapter()
        controller, store, _ = self.build(adapter)
        mission, _ = controller.submit(
            mission_payload(provider_candidates=[{"profile": ALPHA}, {"profile": BETA}]),
            "safety:nonzero:1",
        )
        result = controller.work_once("w1")
        self.assertEqual(result["state"], "refused")
        self.assertEqual(adapter.spawned_profiles, [ALPHA])

    def test_crash_at_all_lifecycle_states_preserves_provider_stickiness(self):
        """Verify crash/restart at each state maintains provider identity and memoizes steps."""
        for crash_state in ("dispatched", "candidate_verified", "evaluated", "evidence_sealed"):
            with self.subTest(crash_state=crash_state):
                adapter = LayerAdapter()
                controller, store, db_path = self.build(adapter, lease_seconds=0.02)
                mission, _ = controller.submit(
                    mission_payload(provider_candidates=[{"profile": ALPHA}, {"profile": BETA}]),
                    f"crash:sticky:{crash_state}",
                )

                # Claim and advance to crash_state
                claimed = store.claim("w1", lease_seconds=0.02)
                token = claimed["lease_token"]
                m_id = claimed["id"]

                # Step 1: dispatch
                dispatch_output = {
                    "status": "completed", "candidate_sha": "a" * 40, "execution_id": "exec:1",
                    "receipt": {"provider_profile": ALPHA, "provider": f"{ALPHA}/v1", "process_started": True,
                                "idempotency_key": f"crash:sticky:{crash_state}", "classification": "completed",
                                "selection_trace": [], "fallback_chain": [], "duration_ms": 10,
                                "execution_mode": "fixture", "refusal_code": None,
                                "usage": {"cost_state": "unknown"}},
                }
                store.begin_step(m_id, token, "dispatch", {"mission": claimed["payload"]})
                store.record_run(
                    m_id, 1, {"reason": "first_admissible", "considered": []},
                    dispatch_output["receipt"],
                    f"crash:sticky:{crash_state}",
                )
                store.complete_step(m_id, token, "dispatch", dispatch_output)
                store.transition(m_id, token, "dispatched")

                if crash_state in ("candidate_verified", "evaluated", "evidence_sealed"):
                    store.begin_step(m_id, token, "verify", {"mission": claimed["payload"], "dispatch": dispatch_output})
                    store.complete_step(m_id, token, "verify", {"verified": True})
                    store.transition(m_id, token, "candidate_verified")

                if crash_state in ("evaluated", "evidence_sealed"):
                    store.begin_step(m_id, token, "evaluate", {
                        "mission": claimed["payload"],
                        "dispatch": dispatch_output,
                        "verification": {"verified": True},
                    })
                    store.complete_step(m_id, token, "evaluate", {
                        "passed": True, "gate_outcomes": [{"gate_id": "G", "passed": True}],
                    })
                    store.transition(m_id, token, "evaluated")

                if crash_state == "evidence_sealed":
                    store.begin_step(m_id, token, "evidence", {
                        "mission": claimed["payload"],
                        "dispatch": dispatch_output,
                        "verification": {"verified": True},
                        "evaluation": {"passed": True, "gate_outcomes": [{"gate_id": "G", "passed": True}]},
                    })
                    store.complete_step(m_id, token, "evidence", {
                        "accepted": True, "evidence_pointer": "e" * 64,
                    })
                    store.transition(m_id, token, "evidence_sealed")

                # Simulate process death: lease expires
                time.sleep(0.05)

                # While dead, change availability so ALPHA is marked unavailable
                adapter.proven_unavailable.add(ALPHA)

                # Reopen DB with fresh worker
                resumed = self.reopen(db_path, adapter)
                result = resumed.work_once("replacement-worker")

                # Must complete successfully on ALPHA without rerouting to BETA
                self.assertEqual(result["state"], "completed")
                route = resumed.store.route_history(m_id)
                self.assertEqual(route["selected_provider_profile"], ALPHA)
                # Dispatch step was memoized, zero new dispatches to BETA or ALPHA
                self.assertEqual(len(adapter.dispatches), 0)


if __name__ == "__main__":
    unittest.main()
