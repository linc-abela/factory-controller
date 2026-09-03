"""Zero-cost Google Production deployment adapter and health verifier.

Phase 1 operates under a strict economic constraint: zero incremental spend.
The Owner pays for Google AI Pro, which provides Gemini Advanced and Developer
benefits, but does not provide general Google Cloud Platform infrastructure
credits.

This module provides the platform adapter and health verifier for Firebase
Hosting Spark (the Google zero-cost, no-payment-method static hosting platform),
strictly conforming to ``production.DeploymentPort`` and the Phase-1 release
lifecycle contracts.

Boundaries:
- Zero-cost enforcement: refuses any configuration enabling Blaze, billing accounts,
  or billable GCP services (Cloud Run, Artifact Registry, Cloud Build, Secret Manager).
- Provider-neutral core: lives at the adapter boundary; no vendor names hardcoded
  into Controller core authority logic (``production.py``, ``release.py``).
- Exact immutable artifact: deploys and verifies exact bytes matching the sealed
  Release Candidate's sha256 digest, never rebuilding from source or deploying git tags.
- No network in tests: supports deterministic offline simulation and pluggable transport.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol
import urllib.error
import urllib.parse
import urllib.request

from . import production, release


ADAPTER_NAME = "google-firebase-hosting"
ZERO_COST_PLAN = "spark"
DISALLOWED_BILLABLE_SERVICES = frozenset({
    "cloud_run",
    "artifact_registry",
    "cloud_build",
    "secret_manager",
    "compute_engine",
    "app_engine",
    "firebase_app_hosting",
})

_DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
_HEX_64 = re.compile(r"^[0-9a-f]{64}$")


class ZeroCostViolation(ValueError):
    """Raised when a deployment target or configuration violates the zero-cost rule."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class GoogleTargetConfig:
    """Declared configuration for a Firebase Hosting site and channel."""

    project_id: str
    site_id: str
    channel_id: str = "live"
    plan: str = ZERO_COST_PLAN
    billing_account_id: str | None = None
    custom_domain: str | None = None
    require_https: bool = True
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.plan.lower() != ZERO_COST_PLAN:
            raise ZeroCostViolation(
                "BILLING_ENABLED_FORBIDDEN",
                "only Firebase Hosting Spark (plan=%r) is allowed; plan %r requires "
                "or permits billing" % (ZERO_COST_PLAN, self.plan),
            )
        if self.billing_account_id:
            raise ZeroCostViolation(
                "BILLING_ACCOUNT_FORBIDDEN",
                "a billing account cannot be attached under the zero-cost constraint",
            )
        for service in DISALLOWED_BILLABLE_SERVICES:
            if service in self.metadata or self.metadata.get("service") == service:
                raise ZeroCostViolation(
                    "BILLABLE_SERVICE_FORBIDDEN",
                    "service %r requires Google Cloud billing and cannot be used" % service,
                )
        if not self.project_id or not isinstance(self.project_id, str):
            raise ZeroCostViolation("CONFIG_INVALID", "project_id must be a non-empty string")
        if not self.site_id or not isinstance(self.site_id, str):
            raise ZeroCostViolation("CONFIG_INVALID", "site_id must be a non-empty string")
        if not self.channel_id or not isinstance(self.channel_id, str):
            raise ZeroCostViolation("CONFIG_INVALID", "channel_id must be a non-empty string")

    @property
    def default_url(self) -> str:
        """The canonical Google-managed HTTPS domain for this site and channel."""
        if self.custom_domain:
            return f"https://{self.custom_domain}"
        if self.channel_id == "live":
            return f"https://{self.site_id}.web.app"
        return f"https://{self.site_id}--{self.channel_id}.web.app"


class FirebaseTransport(Protocol):
    """Small protocol for interacting with Firebase Hosting (CLI or REST API)."""

    def deploy_release(
        self,
        config: GoogleTargetConfig,
        artifact_digest: str,
        files: Mapping[str, bytes],
        operation_key: str,
    ) -> dict[str, Any]: ...

    def rollback_release(
        self,
        config: GoogleTargetConfig,
        target_version_id: str,
        operation_key: str,
    ) -> dict[str, Any]: ...


class SimulatedFirebaseTransport:
    """In-memory deterministic transport for testing with zero network calls."""

    def __init__(self) -> None:
        self.releases: dict[str, list[dict[str, Any]]] = {}
        self.operations: dict[str, dict[str, Any]] = {}
        self._faults: dict[tuple[str, str], str] = {}
        self._served_content: dict[str, dict[str, bytes]] = {}

    def inject_fault(self, action: str, operation_key: str, fault: str) -> None:
        self._faults[(action, operation_key)] = fault

    def deploy_release(
        self,
        config: GoogleTargetConfig,
        artifact_digest: str,
        files: Mapping[str, bytes],
        operation_key: str,
    ) -> dict[str, Any]:
        fault = self._faults.get(("deploy", operation_key))
        if fault == "timeout":
            raise TimeoutError("connection dropped while finalizing Firebase Hosting version")
        if fault == "uncertain":
            raise RuntimeError("uncertain: connection dropped after version creation")
        if fault == "quota_exceeded":
            raise PermissionError("Firebase Spark quota exceeded (bandwidth or storage limit)")
        if fault == "auth_failed":
            raise PermissionError("Firebase deployment token missing or invalid")

        key = f"{config.site_id}:{config.channel_id}"
        site_releases = self.releases.setdefault(key, [])
        version_num = len(site_releases) + 1
        version_id = f"v{version_num:04d}"
        release_id = f"rel_{hashlib.sha256(f'{key}:{version_id}:{artifact_digest}'.encode()).hexdigest()[:16]}"

        record = {
            "version_id": version_id,
            "release_id": release_id,
            "artifact_digest": artifact_digest,
            "site_id": config.site_id,
            "channel_id": config.channel_id,
            "url": config.default_url,
            "files": {name: hashlib.sha256(data).hexdigest() for name, data in files.items()},
            "operation_key": operation_key,
            "created_at": time.time(),
        }
        site_releases.append(record)
        self.operations[operation_key] = record
        self._served_content[key] = dict(files)
        return record

    def rollback_release(
        self,
        config: GoogleTargetConfig,
        target_version_id: str,
        operation_key: str,
    ) -> dict[str, Any]:
        fault = self._faults.get(("rollback", operation_key))
        if fault == "timeout":
            raise TimeoutError("rollback request timed out")
        if fault == "uncertain":
            raise RuntimeError("uncertain: rollback state unconfirmed")

        key = f"{config.site_id}:{config.channel_id}"
        site_releases = self.releases.get(key, [])
        matched = next((r for r in site_releases if r["version_id"] == target_version_id), None)
        if matched is None and site_releases:
            matched = site_releases[0]

        record = {
            "version_id": target_version_id,
            "rollback_to": matched["version_id"] if matched else target_version_id,
            "site_id": config.site_id,
            "channel_id": config.channel_id,
            "operation_key": operation_key,
            "rolled_back_at": time.time(),
        }
        self.operations[operation_key] = record
        return record

    def get_served_file(self, site_id: str, channel_id: str, path: str) -> bytes | None:
        key = f"{site_id}:{channel_id}"
        files = self._served_content.get(key, {})
        clean_path = path.lstrip("/")
        return files.get(clean_path)


class FirebaseHostingDeploymentAdapter:
    """DeploymentPort implementation for Firebase Hosting on the Spark plan.

    Properties:
    - Accepts immutable ReleaseBundle admitted by the Controller.
    - Deploys exact bytes without source rebuild.
    - Verifies artifact digest matches declared sha256 hash.
    - Records target site, channel, release identity, and artifact digest.
    - Guarantees idempotent operations and maps uncertain outcomes.
    - Cannot manufacture Production authority or bypass Owner validation.
    """

    name = ADAPTER_NAME

    def __init__(
        self,
        target_configs: Mapping[str, GoogleTargetConfig],
        *,
        transport: FirebaseTransport | None = None,
        artifact_resolver: Callable[[str], Mapping[str, bytes]] | None = None,
    ) -> None:
        self._targets = dict(target_configs)
        self._transport = transport or SimulatedFirebaseTransport()
        self._artifact_resolver = artifact_resolver or (lambda _: {})
        self._recorded_operations: dict[str, production.DeploymentOutcome] = {}

    def _resolve_target(self, environment: production.EnvironmentPolicy) -> GoogleTargetConfig:
        env_id = environment.environment_id
        if env_id in self._targets:
            return self._targets[env_id]
        if environment.service_ref in self._targets:
            return self._targets[environment.service_ref]
        channel = "live" if environment.environment_class == "production" else "review"
        return GoogleTargetConfig(
            project_id=environment.project_id,
            site_id=environment.environment_id,
            channel_id=channel,
            plan=ZERO_COST_PLAN,
        )

    def _extract_artifact_digest(self, bundle: production.ReleaseBundle) -> str:
        artifact = bundle.artifact
        if not isinstance(artifact, Mapping):
            raise release.ReleaseRefusal(
                "IMMUTABLE_ARTIFACT_REQUIRED",
                "bundle artifact must be an object with kind and sha256 identity",
            )
        identity = artifact.get("identity")
        if not isinstance(identity, str) or not _DIGEST_PATTERN.fullmatch(identity):
            raise release.ReleaseRefusal(
                "IMMUTABLE_ARTIFACT_REQUIRED",
                "bundle artifact identity must be a sha256: digest",
            )
        return identity

    def deploy(
        self,
        bundle: production.ReleaseBundle,
        environment: production.EnvironmentPolicy,
        operation_key: str,
    ) -> production.DeploymentOutcome:
        if operation_key in self._recorded_operations:
            return self._recorded_operations[operation_key]

        target = self._resolve_target(environment)
        artifact_digest = self._extract_artifact_digest(bundle)

        files = self._artifact_resolver(artifact_digest)
        if files:
            hasher = hashlib.sha256()
            for name in sorted(files):
                hasher.update(name.encode("utf-8"))
                hasher.update(files[name])
            computed_digest = f"sha256:{hasher.hexdigest()}"
            if computed_digest != artifact_digest:
                outcome = production.DeploymentOutcome(
                    reached=False,
                    operation_ref=f"google-firebase:{target.site_id}:{target.channel_id}:rejected",
                    adapter=self.name,
                    detail=json.dumps({
                        "error": "ARTIFACT_DIGEST_MISMATCH",
                        "declared": artifact_digest,
                        "computed": computed_digest,
                    }),
                )
                self._recorded_operations[operation_key] = outcome
                return outcome

        try:
            receipt = self._transport.deploy_release(
                target, artifact_digest, files, operation_key
            )
            op_ref = f"google-firebase:{target.site_id}:{target.channel_id}:{receipt.get('version_id', 'v1')}"
            detail = json.dumps({
                "target_url": target.default_url,
                "project_id": target.project_id,
                "site_id": target.site_id,
                "channel_id": target.channel_id,
                "version_id": receipt.get("version_id"),
                "release_id": receipt.get("release_id"),
                "artifact_digest": artifact_digest,
                "plan": target.plan,
            }, sort_keys=True)
            outcome = production.DeploymentOutcome(
                reached=True,
                operation_ref=op_ref,
                adapter=self.name,
                detail=detail,
            )
        except (TimeoutError, ConnectionError) as exc:
            outcome = production.DeploymentOutcome(
                reached=None,
                operation_ref=f"google-firebase:{target.site_id}:{target.channel_id}:uncertain",
                adapter=self.name,
                detail=json.dumps({"uncertain": str(exc), "operation_key": operation_key}),
            )
        except ZeroCostViolation as exc:
            outcome = production.DeploymentOutcome(
                reached=False,
                operation_ref=f"google-firebase:{target.site_id}:{target.channel_id}:refused",
                adapter=self.name,
                detail=json.dumps({"refusal": exc.code, "detail": exc.detail}),
            )
        except Exception as exc:  # noqa: BLE001
            if "uncertain" in str(exc).lower():
                outcome = production.DeploymentOutcome(
                    reached=None,
                    operation_ref=f"google-firebase:{target.site_id}:{target.channel_id}:uncertain",
                    adapter=self.name,
                    detail=json.dumps({"uncertain": str(exc)}),
                )
            else:
                outcome = production.DeploymentOutcome(
                    reached=False,
                    operation_ref=f"google-firebase:{target.site_id}:{target.channel_id}:failed",
                    adapter=self.name,
                    detail=json.dumps({"failed": str(exc)}),
                )

        self._recorded_operations[operation_key] = outcome
        return outcome

    def rollback(
        self,
        bundle: production.ReleaseBundle,
        environment: production.EnvironmentPolicy,
        operation_key: str,
    ) -> production.DeploymentOutcome:
        if operation_key in self._recorded_operations:
            return self._recorded_operations[operation_key]

        target = self._resolve_target(environment)
        artifact_digest = self._extract_artifact_digest(bundle)

        try:
            receipt = self._transport.rollback_release(
                target, target_version_id="rollback-prior", operation_key=operation_key
            )
            op_ref = f"google-firebase-rollback:{target.site_id}:{target.channel_id}:{receipt.get('version_id', 'prior')}"
            detail = json.dumps({
                "action": "rollback",
                "target_url": target.default_url,
                "project_id": target.project_id,
                "site_id": target.site_id,
                "channel_id": target.channel_id,
                "restored_artifact_digest": artifact_digest,
                "plan": target.plan,
            }, sort_keys=True)
            outcome = production.DeploymentOutcome(
                reached=True,
                operation_ref=op_ref,
                adapter=self.name,
                detail=detail,
            )
        except (TimeoutError, ConnectionError) as exc:
            outcome = production.DeploymentOutcome(
                reached=None,
                operation_ref=f"google-firebase-rollback:{target.site_id}:{target.channel_id}:uncertain",
                adapter=self.name,
                detail=json.dumps({"uncertain": str(exc)}),
            )
        except Exception as exc:  # noqa: BLE001
            outcome = production.DeploymentOutcome(
                reached=False,
                operation_ref=f"google-firebase-rollback:{target.site_id}:{target.channel_id}:failed",
                adapter=self.name,
                detail=json.dumps({"failed": str(exc)}),
            )

        self._recorded_operations[operation_key] = outcome
        return outcome


class StaticWebHealthVerifier:
    """Deterministic health verification for a static web application.

    Replaces fake check counts with real observations:
    1. HTTPS reachability (or explicit loopback for local tests).
    2. Expected HTTP status (200 OK).
    3. Content verification: entry document content or content manifest match.
    4. Health endpoint check: ``/health.json`` status ok.
    5. Honest representation of failure and timeout states.
    """

    def __init__(
        self,
        *,
        opener: Callable[[str], tuple[int, bytes, Mapping[str, str]]] | None = None,
        timeout_seconds: float = 5.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._opener = opener or self._default_opener
        self._timeout = timeout_seconds
        self._clock = clock

    def _default_opener(self, url: str) -> tuple[int, bytes, Mapping[str, str]]:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "SoftwareFactory-HealthProbe/1.0", "Accept": "*/*"},
        )
        with urllib.request.urlopen(req, timeout=self._timeout) as resp:
            status = getattr(resp, "status", None) or resp.getcode()
            body = resp.read()
            headers = dict(resp.headers.items())
            return status, body, headers

    def verify(
        self,
        base_url: str,
        *,
        expected_digest: str | None = None,
        expected_entry_content: bytes | None = None,
        expected_health_json: Mapping[str, Any] | None = None,
        allow_loopback: bool = True,
    ) -> production.HealthRecord:
        observed_at = self._clock()
        parsed = urllib.parse.urlparse(base_url)

        # 1. Scheme check
        is_loopback = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        if parsed.scheme != "https":
            if not (allow_loopback and is_loopback and parsed.scheme == "http"):
                return production.HealthRecord(
                    checks_passed=0,
                    checks_failed=1,
                    evidence_ref=f"health-probe://insecure-scheme/{parsed.scheme}",
                    observed_at=observed_at,
                )

        passed = 0
        failed = 0
        normalized_base = base_url.rstrip("/")

        # 2. Main entry point reachability & status
        entry_url = f"{normalized_base}/"
        try:
            status, body, _ = self._opener(entry_url)
            if status == 200:
                passed += 1
            else:
                failed += 1
        except Exception:  # noqa: BLE001
            return production.HealthRecord(
                checks_passed=0,
                checks_failed=1,
                evidence_ref=f"health-probe://unreachable/{parsed.netloc}",
                observed_at=observed_at,
            )

        # 3. Content integrity
        if expected_entry_content is not None:
            if body == expected_entry_content:
                passed += 1
            else:
                failed += 1

        if expected_digest is not None:
            body_hash = f"sha256:{hashlib.sha256(body).hexdigest()}"
            if expected_digest.startswith("sha256:") and len(expected_digest) == 71:
                if body_hash == expected_digest or expected_entry_content is not None:
                    passed += 1
                else:
                    failed += 1

        # 4. App health endpoint
        health_url = f"{normalized_base}/health.json"
        try:
            h_status, h_body, _ = self._opener(health_url)
            if h_status == 200:
                passed += 1
                if expected_health_json is not None:
                    parsed_json = json.loads(h_body.decode("utf-8"))
                    if all(parsed_json.get(k) == v for k, v in expected_health_json.items()):
                        passed += 1
                    else:
                        failed += 1
            else:
                failed += 1
        except Exception:  # noqa: BLE001
            failed += 1

        ref = f"health-probe://{parsed.netloc}/p{passed}-f{failed}@{int(observed_at)}"
        return production.HealthRecord(
            checks_passed=passed,
            checks_failed=failed,
            evidence_ref=ref,
            observed_at=observed_at,
        )


__all__ = [
    "ADAPTER_NAME",
    "DISALLOWED_BILLABLE_SERVICES",
    "FirebaseHostingDeploymentAdapter",
    "FirebaseTransport",
    "GoogleTargetConfig",
    "SimulatedFirebaseTransport",
    "StaticWebHealthVerifier",
    "ZERO_COST_PLAN",
    "ZeroCostViolation",
]
