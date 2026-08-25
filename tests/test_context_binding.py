"""One mission, one context manifest, and no way to quietly change it.

These are engine tests rather than contract tests: what is being proved is that
the binding survives a restart, that every refusal happens *before* a provider
process can start, and that a replay against different context is not a replay
at all but a different mission identity the store already refuses.
"""

from __future__ import annotations

import time
import unittest

from factory_controller import context
from factory_controller.store import ConflictError
from tests.support import ALPHA, LayerAdapter, ProcessDeath, RouteTestCase, mission_payload


CORPUS = "vault://software-factory@800f2563ca52f01f2be807fa733d3c8c70dd8a47"
POLICY = "SF-136:STAGE-4-CONTEXT"
REMOTE = "git@example.com:project-alpha.git"
HEAD = "a" * 40
ANCHOR = "MISSION.md"


class BrokerAdapter(LayerAdapter):
    """The execution layer, plus a Context Broker that answers deterministically.

    It selects exactly the refs it is told to select and reports exactly the
    measurements it is given.  Nothing here reads a repository, because nothing
    on the Controller's side of this seam is allowed to.
    """

    def __init__(self, *, selected=(ANCHOR,), measurement=None, status="built",
                 refusal_code=None, corpus=None, mission_input_hash=None, **kwargs):
        super().__init__(**kwargs)
        self.selected = tuple(selected)
        self.measurement = dict(measurement or {})
        self.broker_status = status
        self.refusal_code = refusal_code
        self.corpus = corpus
        self.mission_input_hash = mission_input_hash
        self.context_calls: list[dict] = []

    def execute(self, step, operation_key, value):
        if step != "context":
            return super().execute(step, operation_key, value)
        if step == self.crash_on and not self.crashed:
            self.crashed = True
            raise ProcessDeath(step)
        request = value["context_request"]
        self.context_calls.append(dict(request))
        if self.broker_status != "built":
            return {"status": self.broker_status, "refusal_code": self.refusal_code}
        return self.build(request)

    def build(self, request: dict) -> dict:
        unhashed = {
            "schema_version": context.CONTEXT_SCHEMA_VERSION,
            "mission_input_hash": self.mission_input_hash or request["mission_input_hash"],
            "corpus_identity": self.corpus or request["corpus_identity"],
            "policy_identity": request["policy_identity"],
            "selected_refs": list(self.selected),
            "unresolved_questions": [],
        }
        digest = context.sha256_hex(unhashed)
        return {
            "status": "built",
            "manifest": {**unhashed, "manifest_hash": digest},
            "receipt": {"context_manifest_hash": digest,
                        "selected_refs": list(self.selected), "excluded_refs": []},
            "measurement": {"repository_remote_url": request.get("repository_remote_url"),
                            "head_sha": request.get("baseline_sha"),
                            "cache_state": "miss", "cache_identity": digest[:16],
                            **self.measurement},
        }


def payload(**extra):
    request = {"corpus_identity": CORPUS, "policy_identity": POLICY,
               "required_anchors": [ANCHOR]}
    request.update(extra.pop("context_request", {}))
    return mission_payload(
        work_item_id="SF-136-CTX",
        repository_remote_url=REMOTE,
        baseline_sha=HEAD,
        capability="implement",
        provider_candidates=[ALPHA],
        context_request=request,
        **extra)


def manifest_hash_for(payload_value: dict, selected=(ANCHOR,)) -> str:
    """What a broker selecting ``selected`` must produce for this mission."""

    request = context.ContextRequest.from_payload(payload_value)
    return context.sha256_hex({
        "schema_version": context.CONTEXT_SCHEMA_VERSION,
        "mission_input_hash": request.mission_input_hash,
        "corpus_identity": request.corpus_identity,
        "policy_identity": request.policy_identity,
        "selected_refs": list(selected),
        "unresolved_questions": [],
    })


class BindingTests(RouteTestCase, unittest.TestCase):
    def run_mission(self, adapter, value=None, key="ctx:1"):
        controller, store, path = self.build(adapter)
        mission, _ = controller.submit(value or payload(), key)
        return controller.work_once("w1"), store, path, controller

    def test_a_bound_mission_records_which_manifest_it_used(self):
        adapter = BrokerAdapter(measurement={"baseline_context_bytes": 4000,
                                             "selected_context_bytes": 400,
                                             "selected_context_files": 1,
                                             "manifest_build_ms": 7})
        result, store, _, _ = self.run_mission(adapter)
        self.assertEqual(result["state"], "completed")
        view = store.context_history(result["id"])
        self.assertEqual(view["context_state"], "bound")
        self.assertEqual(view["selected_refs"], [ANCHOR])
        self.assertTrue(view["required_anchors_covered"])
        self.assertEqual(view["reduction"]["reduction_ratio"], 0.9)
        self.assertEqual(view["context_refusals"], [])
        bound = [event for event in store.history(result["id"])
                 if event["kind"] == "CONTEXT_BOUND"]
        self.assertEqual(len(bound), 1)
        self.assertEqual(bound[0]["detail"]["context_manifest_hash"],
                         view["context_manifest_hash"])

    def test_telemetry_carries_measured_context_economics(self):
        adapter = BrokerAdapter(measurement={"baseline_context_bytes": 1000,
                                             "selected_context_bytes": 100,
                                             "selected_context_files": 1})
        result, store, _, _ = self.run_mission(adapter)
        block = store.telemetry(result["id"])["context"]
        self.assertEqual(block["state"], "bound")
        self.assertEqual(block["selected_context_bytes"], 100)
        self.assertEqual(block["baseline_context_bytes"], 1000)
        self.assertEqual(block["cache_state"], "miss")
        self.assertEqual(block["reduction"]["saved_bytes"], 900)

    def test_a_mission_without_a_context_request_is_not_reported_as_zero(self):
        controller, store, _ = self.build(BrokerAdapter())
        mission, _ = controller.submit(mission_payload(), "no-context:1")
        result = controller.work_once("w1")
        self.assertEqual(result["state"], "completed")
        self.assertEqual(store.telemetry(result["id"])["context"]["state"], "not_applicable")
        self.assertEqual(store.context_history(result["id"])["context_state"],
                         "not_applicable")

    def test_the_broker_is_told_the_entitlement_and_nothing_about_the_mission(self):
        adapter = BrokerAdapter()
        self.run_mission(adapter)
        self.assertEqual(len(adapter.context_calls), 1)
        for forbidden in ("idempotency_key", "provider_candidates", "execution_policy",
                          "lease_token", "attempt"):
            self.assertNotIn(forbidden, adapter.context_calls[0])


class RefusalTests(RouteTestCase, unittest.TestCase):
    def refuse(self, adapter, value=None, key="ctx:refuse"):
        controller, store, _ = self.build(adapter)
        controller.submit(value or payload(), key)
        result = controller.work_once("w1")
        return result, store, adapter

    def assertRefusedBeforeDispatch(self, result, adapter, code):
        self.assertEqual(result["state"], "refused")
        self.assertIn(code, result["terminal_reason"])
        self.assertEqual(adapter.dispatches, [], "a provider was reached anyway")

    def test_a_missing_required_anchor_refuses_before_any_provider_runs(self):
        result, store, adapter = self.refuse(BrokerAdapter(selected=()))
        self.assertRefusedBeforeDispatch(result, adapter, "CONTEXT_ANCHOR_MISSING")
        self.assertEqual([event["detail"]["code"] for event in store.history(result["id"])
                          if event["kind"] == "CONTEXT_REFUSED"],
                         ["CONTEXT_ANCHOR_MISSING"])

    def test_a_denied_path_refuses_before_any_provider_runs(self):
        adapter = BrokerAdapter(selected=(ANCHOR, "deploy/keys.json"))
        result, _, adapter = self.refuse(adapter, payload(
            context_request={"denied_paths": ["deploy"]}))
        self.assertRefusedBeforeDispatch(result, adapter, "CONTEXT_DENIED_PATH_SELECTED")

    def test_a_manifest_from_another_repository_refuses(self):
        adapter = BrokerAdapter(corpus="vault://other@" + "c" * 40)
        result, _, adapter = self.refuse(adapter)
        self.assertRefusedBeforeDispatch(result, adapter, "CONTEXT_REPOSITORY_MISMATCH")

    def test_a_manifest_bound_to_another_mission_refuses(self):
        adapter = BrokerAdapter(mission_input_hash="f" * 64)
        result, _, adapter = self.refuse(adapter)
        self.assertRefusedBeforeDispatch(result, adapter, "CONTEXT_MISSION_MISMATCH")

    def test_a_tampered_manifest_refuses(self):
        class Tamperer(BrokerAdapter):
            def build(self, request):
                answer = super().build(request)
                answer["manifest"]["selected_refs"].append("secrets.env")
                return answer

        result, _, adapter = self.refuse(Tamperer())
        self.assertRefusedBeforeDispatch(result, adapter, "INVALID_CONTEXT_MANIFEST")

    def test_over_budget_context_refuses_before_any_provider_runs(self):
        adapter = BrokerAdapter(measurement={"selected_context_bytes": 5000,
                                             "selected_context_files": 1})
        result, _, adapter = self.refuse(adapter, payload(
            context_budget={"max_bytes": 1000}))
        self.assertRefusedBeforeDispatch(result, adapter, "CONTEXT_BUDGET_EXCEEDED")

    def test_an_unmeasured_build_under_a_declared_ceiling_refuses(self):
        result, _, adapter = self.refuse(BrokerAdapter(), payload(
            context_budget={"max_bytes": 1000}))
        self.assertRefusedBeforeDispatch(result, adapter, "CONTEXT_BUDGET_UNMEASURED")

    def test_a_stale_manifest_refuses_before_the_boundary(self):
        adapter = BrokerAdapter(measurement={"built_at": 1.0})
        result, _, adapter = self.refuse(adapter, payload(
            context_request={"max_age_seconds": 5}))
        self.assertRefusedBeforeDispatch(result, adapter, "CONTEXT_MANIFEST_STALE")

    def test_an_unusable_context_request_is_refused_at_submission(self):
        controller, _, _ = self.build(BrokerAdapter())
        with self.assertRaises(Exception) as raised:
            controller.submit(payload(context_request={"max_age_seconds": -1}), "bad:1")
        self.assertIn("INVALID_CONTEXT_REQUEST", str(raised.exception))

    def test_an_unavailable_broker_is_retried_rather_than_memoized(self):
        """Silence must not become this mission's durable context."""

        adapter = BrokerAdapter(status="unavailable")
        controller, store, _ = self.build(adapter)
        mission, _ = controller.submit(payload(), "ctx:retry")
        first = controller.work_once("w1")
        self.assertEqual(first["state"], "admitted")
        self.assertIsNone(store.step_output(mission["id"], "context"))
        adapter.broker_status = "built"
        second = controller.work_once("w1")
        self.assertEqual(second["state"], "completed")
        self.assertEqual(store.context_history(mission["id"])["context_state"], "bound")


class StickinessTests(RouteTestCase, unittest.TestCase):
    def test_a_restart_after_dispatch_reuses_the_manifest_it_ran_on(self):
        """The broker is not asked again, so it cannot answer differently."""

        adapter = BrokerAdapter(crash_on="verify")
        controller, store, path = self.build(adapter, lease_seconds=0.02)
        mission, _ = controller.submit(payload(), "ctx:crash")
        with self.assertRaises(ProcessDeath):
            controller.work_once("w1")
        bound = store.context_history(mission["id"])["context_manifest_hash"]
        self.assertEqual(len(adapter.context_calls), 1)
        time.sleep(0.05)

        # A replacement worker whose broker would now select something else.
        replacement = BrokerAdapter(selected=(ANCHOR, "src/new.py"))
        resumed = self.reopen(path, replacement, lease_seconds=1).work_once("w2")
        self.assertEqual(resumed["state"], "completed")
        self.assertEqual(replacement.context_calls, [],
                         "the broker was consulted after the boundary")
        self.assertEqual(store.context_history(mission["id"])["context_manifest_hash"],
                         bound)

    def test_a_stale_manifest_is_not_re_judged_after_the_boundary(self):
        adapter = BrokerAdapter(crash_on="verify",
                                measurement={"built_at": time.time()})
        controller, store, path = self.build(adapter, lease_seconds=0.02)
        mission, _ = controller.submit(
            payload(context_request={"max_age_seconds": 0.08}), "ctx:stale-after")
        with self.assertRaises(ProcessDeath):
            controller.work_once("w1")
        # The manifest is now older than the mission's own freshness requirement.
        time.sleep(0.1)
        later = self.reopen(path, BrokerAdapter(), lease_seconds=1)
        request = context.ContextRequest.from_payload(store.get(mission["id"])["payload"])
        package = context.package_from_row(store.step_output(mission["id"], "context"))
        self.assertEqual(context.verify(request, package, now=time.time()),
                         "CONTEXT_MANIFEST_STALE")
        resumed = later.work_once("w2")
        self.assertEqual(resumed["state"], "completed")


class ReplayIdentityTests(RouteTestCase, unittest.TestCase):
    """A real mission's key already names its manifest, so a different manifest
    is a different mission -- not a replay the Controller has to detect."""

    def real_payload(self, selected=(ANCHOR,), **extra):
        base = payload(execution_mode="real", acceptance_gate_ids=["G"], **extra)
        base["context_manifest_hash"] = manifest_hash_for(base, selected)
        return base

    def key_for(self, value):
        return "%s:%s" % (value["work_item_id"], value["context_manifest_hash"])

    def test_a_real_mission_runs_when_the_built_manifest_is_the_declared_one(self):
        value = self.real_payload()
        controller, store, _ = self.build(BrokerAdapter(mode="real"))
        controller.submit(value, self.key_for(value))
        result = controller.work_once("w1")
        self.assertEqual(result["state"], "completed")
        self.assertEqual(store.context_history(result["id"])["context_manifest_hash"],
                         value["context_manifest_hash"])

    def test_a_manifest_that_is_not_the_declared_one_refuses_before_dispatch(self):
        value = self.real_payload()
        adapter = BrokerAdapter(mode="real", selected=(ANCHOR, "src/extra.py"))
        controller, _, _ = self.build(adapter)
        controller.submit(value, self.key_for(value))
        result = controller.work_once("w1")
        self.assertEqual(result["state"], "refused")
        self.assertIn("CONTEXT_HASH_MISMATCH", result["terminal_reason"])
        self.assertEqual(adapter.dispatches, [])

    def test_the_same_work_against_different_context_is_a_different_identity(self):
        first = self.real_payload()
        second = self.real_payload(selected=(ANCHOR, "src/extra.py"))
        second["context_manifest_hash"] = manifest_hash_for(
            second, (ANCHOR, "src/extra.py"))
        self.assertNotEqual(first["context_manifest_hash"], second["context_manifest_hash"])
        self.assertNotEqual(self.key_for(first), self.key_for(second))

        controller, store, _ = self.build(BrokerAdapter(mode="real"))
        alpha, created = controller.submit(first, self.key_for(first))
        self.assertTrue(created)
        beta, created = controller.submit(second, self.key_for(second))
        self.assertTrue(created)
        self.assertNotEqual(alpha["id"], beta["id"])

    def test_reusing_one_key_for_different_context_is_refused_by_the_store(self):
        first = self.real_payload()
        second = self.real_payload()
        second["context_request"] = {**second["context_request"],
                                     "required_anchors": [ANCHOR, "README.md"]}
        controller, _, _ = self.build(BrokerAdapter(mode="real"))
        controller.submit(first, self.key_for(first))
        with self.assertRaises(ConflictError):
            controller.submit(second, self.key_for(first))


class TokenBudgetTests(RouteTestCase, unittest.TestCase):
    def test_a_reported_token_overrun_refuses_the_next_dispatch(self):
        adapter = BrokerAdapter()
        controller, store, _ = self.build(adapter)
        mission, _ = controller.submit(
            payload(context_budget={"max_reported_input_tokens": 100}), "ctx:tokens")
        store.record_run(
            mission["id"], 0, {"reason": "seed", "considered": []},
            {"provider_profile": ALPHA, "provider": None, "selection_reason": "seed",
             "fallback_chain": [], "selection_trace": [], "process_started": False,
             "duration_ms": None, "classification": "provider_unavailable",
             "refusal_code": None, "execution_mode": "fixture",
             "idempotency_key": "ctx:tokens", "evidence_class": "reported_claim",
             "usage": {"input_tokens": 500, "output_tokens": None, "cost_amount": None,
                       "cost_currency": None, "cost_state": "unknown"}},
            "ctx:tokens")
        result = controller.work_once("w1")
        self.assertEqual(result["state"], "refused")
        self.assertIn("CONTEXT_TOKEN_BUDGET_EXCEEDED", result["terminal_reason"])
        self.assertEqual(adapter.context_calls, [], "the broker was asked anyway")

    def test_an_unreported_token_count_never_blocks(self):
        controller, store, _ = self.build(BrokerAdapter())
        controller.submit(
            payload(context_budget={"max_reported_input_tokens": 1}), "ctx:tokens-unknown")
        result = controller.work_once("w1")
        self.assertEqual(result["state"], "completed")
        self.assertEqual(store.telemetry(result["id"])["reported_input_tokens"], "unknown")


if __name__ == "__main__":
    unittest.main()
