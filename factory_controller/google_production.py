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

import gzip
import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence
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
    # Factory environment identity for receipts.  Platform site/channel below
    # remain what Hosting actually serves; tests and the ledger key by these.
    identity_site_id: str | None = None
    identity_channel_id: str | None = None

    @property
    def record_site_id(self) -> str:
        return self.identity_site_id or self.site_id

    @property
    def record_channel_id(self) -> str:
        return self.identity_channel_id or self.channel_id

    @property
    def record_key(self) -> str:
        return f"{self.record_site_id}:{self.record_channel_id}"

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


# The header the Hosting API requires.  Spelled plainly: this file is a named
# external seam in `tests/test_authority_boundaries.py`, so the credential-name
# scan exempts it by name rather than being defeated by a split string literal.
# The value is never sourced here -- it arrives as an argument from the operator.
_AUTH_HEADER_KEY = "Authorization"
_AUTH_SCHEME = "Bearer "


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


class FirebaseHostingRestTransport:
    """Real Firebase Hosting transport contacting the Firebase Hosting REST API v1beta1.

    Adheres strictly to zero-cost Phase-1 constraints (Firebase Hosting Spark).
    Zero external dependencies: uses urllib.request.
    Accepts an optional opener for offline deterministic test execution.
    """

    def __init__(
        self,
        *,
        token: str | None = None,
        token_provider: Callable[[], str | None] | None = None,
        opener: Callable[[urllib.request.Request], tuple[int, bytes, Mapping[str, str]]] | None = None,
        timeout_seconds: float = 30.0,
    ) -> None:
        self._token = token
        self._token_provider = token_provider
        self._opener = opener
        self._timeout = timeout_seconds

    def _resolve_token(self) -> str:
        if self._token is not None and self._token.strip():
            return self._token.strip()
        if self._token_provider is not None:
            val = self._token_provider()
            if val and val.strip():
                return val.strip()

        raise PermissionError(
            "Firebase deployment auth token missing: pass token or token_provider"
        )

    def _http_call(
        self,
        url: str,
        *,
        method: str = "GET",
        data: bytes | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> tuple[int, bytes, Mapping[str, str]]:
        req_headers = dict(headers or {})
        if _AUTH_HEADER_KEY not in req_headers:
            token = self._resolve_token()
            req_headers[_AUTH_HEADER_KEY] = f"{_AUTH_SCHEME}{token}"

        req = urllib.request.Request(
            url,
            data=data,
            headers=req_headers,
            method=method,
        )
        if self._opener is not None:
            return self._opener(req)

        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                status = getattr(resp, "status", None) or resp.getcode()
                body = resp.read()
                resp_headers = dict(resp.headers.items())
                return status, body, resp_headers
        except urllib.error.HTTPError as exc:
            err_body = exc.read()
            resp_headers = dict(exc.headers.items()) if exc.headers else {}
            if exc.code in (401, 403):
                raise PermissionError(
                    f"Firebase authentication failed ({exc.code}): {err_body.decode('utf-8', errors='replace')}"
                ) from exc
            if exc.code == 429:
                raise PermissionError(
                    f"Firebase Spark quota exceeded (429): {err_body.decode('utf-8', errors='replace')}"
                ) from exc
            raise RuntimeError(
                f"Firebase Hosting API error ({exc.code}): {err_body.decode('utf-8', errors='replace')}"
            ) from exc
        except (TimeoutError, urllib.error.URLError) as exc:
            reason = getattr(exc, "reason", None)
            if isinstance(reason, TimeoutError) or isinstance(exc, TimeoutError):
                raise TimeoutError(f"connection timed out contacting Firebase Hosting: {exc}") from exc
            raise ConnectionError(f"connection dropped contacting Firebase Hosting: {exc}") from exc

    def deploy_release(
        self,
        config: GoogleTargetConfig,
        artifact_digest: str,
        files: Mapping[str, bytes],
        operation_key: str,
    ) -> dict[str, Any]:
        base_api = f"https://firebasehosting.googleapis.com/v1beta1/sites/{config.site_id}"

        # 1. Create version
        ver_url = f"{base_api}/versions"
        ver_payload = json.dumps({"config": {}}).encode("utf-8")
        status, body, _ = self._http_call(
            ver_url, method="POST", data=ver_payload, headers={"Content-Type": "application/json"}
        )
        version_doc = json.loads(body.decode("utf-8"))
        version_name = version_doc["name"]
        version_id = version_name.split("/")[-1]

        # 2. Populate files
        pop_url = f"https://firebasehosting.googleapis.com/v1beta1/{version_name}:populateFiles"
        file_hashes = {}
        gzipped_files = {}
        for name, file_bytes in files.items():
            clean_name = "/" + name.lstrip("/")
            gz_bytes = gzip.compress(file_bytes, mtime=0)
            gzipped_files[clean_name] = gz_bytes
            file_hashes[clean_name] = hashlib.sha256(gz_bytes).hexdigest()

        pop_payload = json.dumps({"files": file_hashes}).encode("utf-8")
        status, body, _ = self._http_call(
            pop_url, method="POST", data=pop_payload, headers={"Content-Type": "application/json"}
        )
        pop_doc = json.loads(body.decode("utf-8"))
        upload_url = pop_doc.get("uploadUrl")
        required_hashes = set(pop_doc.get("uploadRequiredHashes") or [])

        # 3. Upload required files
        if upload_url and required_hashes:
            for clean_name, gz_bytes in gzipped_files.items():
                f_hash = file_hashes[clean_name]
                if f_hash in required_hashes:
                    up_url = f"{upload_url}/{f_hash}"
                    self._http_call(
                        up_url, method="POST", data=gz_bytes, headers={"Content-Type": "application/octet-stream"}
                    )

        # 4. Finalize version
        finalize_url = f"https://firebasehosting.googleapis.com/v1beta1/{version_name}?update_mask=status"
        finalize_payload = json.dumps({"status": "FINALIZED"}).encode("utf-8")
        self._http_call(
            finalize_url, method="PATCH", data=finalize_payload, headers={"Content-Type": "application/json"}
        )

        # 5. Create release
        if config.channel_id == "live":
            rel_url = f"{base_api}/releases?versionName={version_name}"
        else:
            rel_url = f"{base_api}/channels/{config.channel_id}/releases?versionName={version_name}"

        status, body, _ = self._http_call(
            rel_url, method="POST", data=b"{}", headers={"Content-Type": "application/json"}
        )
        rel_doc = json.loads(body.decode("utf-8"))
        release_name = rel_doc["name"]
        release_id = release_name.split("/")[-1]

        return {
            "version_id": version_id,
            "version_name": version_name,
            "release_id": release_id,
            "release_name": release_name,
            "artifact_digest": artifact_digest,
            "site_id": config.site_id,
            "channel_id": config.channel_id,
            "url": config.default_url,
            "files": {name: hashlib.sha256(data).hexdigest() for name, data in files.items()},
            "operation_key": operation_key,
            "created_at": time.time(),
        }

    def rollback_release(
        self,
        config: GoogleTargetConfig,
        target_version_id: str,
        operation_key: str,
    ) -> dict[str, Any]:
        base_api = f"https://firebasehosting.googleapis.com/v1beta1/sites/{config.site_id}"
        if target_version_id.startswith("sites/"):
            target_version_name = target_version_id
            version_id = target_version_id.split("/")[-1]
        else:
            target_version_name = f"sites/{config.site_id}/versions/{target_version_id}"
            version_id = target_version_id

        if config.channel_id == "live":
            rel_url = f"{base_api}/releases?versionName={target_version_name}"
        else:
            rel_url = f"{base_api}/channels/{config.channel_id}/releases?versionName={target_version_name}"

        status, body, _ = self._http_call(
            rel_url, method="POST", data=b"{}", headers={"Content-Type": "application/json"}
        )
        rel_doc = json.loads(body.decode("utf-8"))
        release_name = rel_doc["name"]
        release_id = release_name.split("/")[-1]

        return {
            "version_id": version_id,
            "version_name": target_version_name,
            "release_id": release_id,
            "release_name": release_name,
            "site_id": config.site_id,
            "channel_id": config.channel_id,
            "operation_key": operation_key,
            "rolled_back_at": time.time(),
        }


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

        key = config.record_key
        site_releases = self.releases.setdefault(key, [])
        version_num = len(site_releases) + 1
        version_id = f"v{version_num:04d}"
        release_id = f"rel_{hashlib.sha256(f'{key}:{version_id}:{artifact_digest}'.encode()).hexdigest()[:16]}"

        record = {
            "version_id": version_id,
            "release_id": release_id,
            "artifact_digest": artifact_digest,
            "site_id": config.record_site_id,
            "channel_id": config.record_channel_id,
            "url": config.default_url,
            "files": {name: hashlib.sha256(data).hexdigest() for name, data in files.items()},
            "files_content": dict(files),
            "operation_key": operation_key,
            "created_at": time.time(),
        }
        site_releases.append(record)
        self.operations[operation_key] = record
        payload = dict(files)
        self._served_content[key] = payload
        platform_key = f"{config.site_id}:{config.channel_id}"
        self._served_content[platform_key] = payload
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

        key = config.record_key
        site_releases = self.releases.get(key, [])
        matched = next((r for r in site_releases if r["version_id"] == target_version_id), None)
        if matched is None:
            raise ValueError(
                f"target_version_id {target_version_id!r} not found for {key}; "
                f"available versions: {[r['version_id'] for r in site_releases]}"
            )

        record = {
            "version_id": target_version_id,
            "release_id": matched.get("release_id", f"rel_rollback_{target_version_id}"),
            "rollback_to": matched["version_id"],
            "artifact_digest": matched.get("artifact_digest"),
            "site_id": config.record_site_id,
            "channel_id": config.record_channel_id,
            "operation_key": operation_key,
            "rolled_back_at": time.time(),
        }
        self.operations[operation_key] = record
        if "files_content" in matched:
            payload = dict(matched["files_content"])
            self._served_content[key] = payload
            self._served_content[f"{config.site_id}:{config.channel_id}"] = payload
        return record

    def get_served_file(self, site_id: str, channel_id: str, path: str) -> bytes | None:
        key = f"{site_id}:{channel_id}"
        files = self._served_content.get(key, {})
        clean_path = path.lstrip("/")
        return files.get(clean_path)


def file_system_artifact_resolver(
    artifact_digest: str,
    base_dirs: Sequence[Path | str] | None = None,
) -> Mapping[str, bytes]:
    """Resolve an unsealed artifact's files from disk by its sha256 digest.

    Fails closed if the directory is missing, empty, or unreadable.
    """
    digest_hex = artifact_digest.removeprefix("sha256:")
    candidates: list[Path] = []
    if base_dirs:
        candidates.extend(Path(p) for p in base_dirs)
    candidates.append(Path.home() / ".factory-controller" / "review" / digest_hex)
    candidates.append(Path.cwd() / ".review")

    for directory in candidates:
        if not directory.is_dir():
            continue
        files: dict[str, bytes] = {}
        try:
            for file_path in directory.rglob("*"):
                if file_path.is_file():
                    rel_path = file_path.relative_to(directory).as_posix()
                    files[rel_path] = file_path.read_bytes()
        except OSError:
            continue
        if files:
            if production.deployable_digest(files) == artifact_digest:
                return files
    return {}


DEFAULT_TARGET_CONFIGS: Mapping[str, GoogleTargetConfig] = {
    "lodus-casino-review": GoogleTargetConfig(
        project_id="astral-dogfood",
        site_id="lodus-casino-review",
        channel_id="live",
        plan=ZERO_COST_PLAN,
        identity_channel_id="review",
    ),
    "lodus-casino-cloud-review": GoogleTargetConfig(
        project_id="astral-dogfood",
        site_id="lodus-casino-review",
        channel_id="live",
        plan=ZERO_COST_PLAN,
        identity_channel_id="review",
    ),
    "lodus-casino-production": GoogleTargetConfig(
        project_id="astral-dogfood",
        site_id="lodus-casino",
        channel_id="live",
        plan=ZERO_COST_PLAN,
        identity_site_id="lodus-casino-production",
    ),
}


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
        store: Any | None = None,
    ) -> None:
        self._targets = dict(target_configs)
        # Requirement 2: Live adapter construction cannot silently select simulated transport
        self._transport = transport if transport is not None else FirebaseHostingRestTransport()
        self._artifact_resolver = artifact_resolver if artifact_resolver is not None else file_system_artifact_resolver
        self._store = store
        self._recorded_operations: dict[str, production.DeploymentOutcome] = {}
        self._target_history: dict[str, list[dict[str, Any]]] = {}

    @property
    def transport(self) -> FirebaseTransport:
        return self._transport

    def _resolve_target(self, environment: production.EnvironmentPolicy) -> GoogleTargetConfig:
        env_id = environment.environment_id
        if env_id in self._targets:
            return self._targets[env_id]
        if environment.service_ref in self._targets:
            return self._targets[environment.service_ref]
        if env_id in DEFAULT_TARGET_CONFIGS:
            return DEFAULT_TARGET_CONFIGS[env_id]
        if environment.service_ref in DEFAULT_TARGET_CONFIGS:
            return DEFAULT_TARGET_CONFIGS[environment.service_ref]
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

        # Requirement 3: Missing/empty artifact bytes fail closed
        try:
            files = self._artifact_resolver(artifact_digest)
        except Exception as exc:
            outcome = production.DeploymentOutcome(
                reached=False,
                operation_ref=f"google-firebase:{target.record_site_id}:{target.record_channel_id}:rejected",
                adapter=self.name,
                detail=json.dumps({
                    "error": "ARTIFACT_RESOLVER_FAILED",
                    "detail": str(exc),
                    "artifact_digest": artifact_digest,
                }, sort_keys=True),
            )
            self._recorded_operations[operation_key] = outcome
            return outcome

        if not files:
            outcome = production.DeploymentOutcome(
                reached=False,
                operation_ref=f"google-firebase:{target.record_site_id}:{target.record_channel_id}:rejected",
                adapter=self.name,
                detail=json.dumps({
                    "error": "EMPTY_OR_MISSING_ARTIFACT",
                    "artifact_digest": artifact_digest,
                }, sort_keys=True),
            )
            self._recorded_operations[operation_key] = outcome
            return outcome

        # Requirement 4: Re-derive exact digest before network mutation
        computed_digest = production.deployable_digest(files)
        if computed_digest != artifact_digest:
            outcome = production.DeploymentOutcome(
                reached=False,
                operation_ref=f"google-firebase:{target.record_site_id}:{target.record_channel_id}:rejected",
                adapter=self.name,
                detail=json.dumps({
                    "error": "ARTIFACT_DIGEST_MISMATCH",
                    "declared": artifact_digest,
                    "computed": computed_digest,
                }, sort_keys=True),
            )
            self._recorded_operations[operation_key] = outcome
            return outcome

        try:
            receipt = self._transport.deploy_release(
                target, artifact_digest, files, operation_key
            )
            version_id = receipt.get("version_id", "v1")
            release_id = receipt.get("release_id")
            op_ref = f"google-firebase:{target.record_site_id}:{target.record_channel_id}:{version_id}"
            detail = json.dumps({
                "artifact_digest": artifact_digest,
                "channel_id": target.channel_id,
                "plan": target.plan,
                "project_id": target.project_id,
                "release_id": release_id,
                "site_id": target.site_id,
                "target_url": target.default_url,
                "version_id": version_id,
            }, sort_keys=True)
            outcome = production.DeploymentOutcome(
                reached=True,
                operation_ref=op_ref,
                adapter=self.name,
                detail=detail,
            )
            target_key = f"{target.record_site_id}:{target.record_channel_id}"
            self._target_history.setdefault(target_key, []).append({
                "version_id": version_id,
                "release_id": release_id,
                "artifact_digest": artifact_digest,
                "target_url": target.default_url,
                "operation_key": operation_key,
                "deployed_at": time.time(),
            })
        except (TimeoutError, ConnectionError) as exc:
            outcome = production.DeploymentOutcome(
                reached=None,
                operation_ref=f"google-firebase:{target.record_site_id}:{target.record_channel_id}:uncertain",
                adapter=self.name,
                detail=json.dumps({"uncertain": str(exc), "operation_key": operation_key}, sort_keys=True),
            )
        except ZeroCostViolation as exc:
            outcome = production.DeploymentOutcome(
                reached=False,
                operation_ref=f"google-firebase:{target.record_site_id}:{target.record_channel_id}:refused",
                adapter=self.name,
                detail=json.dumps({"refusal": exc.code, "detail": exc.detail}, sort_keys=True),
            )
        except Exception as exc:  # noqa: BLE001
            if "uncertain" in str(exc).lower():
                outcome = production.DeploymentOutcome(
                    reached=None,
                    operation_ref=f"google-firebase:{target.record_site_id}:{target.record_channel_id}:uncertain",
                    adapter=self.name,
                    detail=json.dumps({"uncertain": str(exc)}, sort_keys=True),
                )
            else:
                outcome = production.DeploymentOutcome(
                    reached=False,
                    operation_ref=f"google-firebase:{target.record_site_id}:{target.record_channel_id}:failed",
                    adapter=self.name,
                    detail=json.dumps({"failed": str(exc)}, sort_keys=True),
                )

        self._recorded_operations[operation_key] = outcome
        return outcome

    def _ledger_prior_version(
        self,
        target: GoogleTargetConfig,
        operation_key: str,
        attempted_digest: str,
    ) -> dict[str, Any] | None:
        """The prior known-good platform version, read from durable Production state.

        In-process history covers only a deploy and a rollback inside one
        command.  Every real rollback is a separate Owner command in a separate
        process, so that history is empty exactly when it matters.  Nothing new
        is written for this: the ledger already chose the rollback target
        (``deployments.rollback_of``, restricted by it to a healthy or recovered
        release) and this adapter already recorded the platform version inside
        that deployment's ``operation_ref``.  This reads both back.  Returning
        ``None`` leaves the caller refusing, which is the fail-closed path.
        """

        if self._store is None:
            return None
        deployment_id = operation_key.split(":")[0]
        prefix = f"google-firebase:{target.record_site_id}:{target.record_channel_id}:"
        try:
            with self._store.transaction() as db:
                row = db.execute(
                    "SELECT rollback_of FROM deployments WHERE id=?",
                    (deployment_id,)).fetchone()
                if row is None or not row["rollback_of"]:
                    return None
                prior = db.execute(
                    "SELECT operation_ref, adapter, bundle_json FROM deployments WHERE id=?",
                    (row["rollback_of"],)).fetchone()
        except Exception:  # noqa: BLE001 -- an unreadable ledger is not a rollback target
            return None
        if prior is None or prior["adapter"] != self.name:
            return None
        recorded_ref = prior["operation_ref"] or ""
        if not recorded_ref.startswith(prefix):
            return None
        version_id = recorded_ref[len(prefix):]
        # The non-version endings this adapter writes for an outcome that never
        # produced a platform version.  None of them is a rollback target.
        if not version_id or version_id in {"uncertain", "refused", "failed", "rejected"}:
            return None
        try:
            prior_digest = json.loads(prior["bundle_json"]).get("artifact", {}).get("identity")
        except (TypeError, ValueError):
            return None
        if prior_digest == attempted_digest:
            return None
        return {
            "version_id": version_id,
            "release_id": None,
            "artifact_digest": prior_digest,
            "source": "production_ledger",
        }

    def rollback(
        self,
        bundle: production.ReleaseBundle,
        environment: production.EnvironmentPolicy,
        operation_key: str,
    ) -> production.DeploymentOutcome:
        if operation_key in self._recorded_operations:
            return self._recorded_operations[operation_key]

        target = self._resolve_target(environment)
        attempted_digest = self._extract_artifact_digest(bundle)
        target_key = f"{target.record_site_id}:{target.record_channel_id}"

        # Requirement 5: Real rollback identity, chosen by the ledger.
        #
        # The ledger already selected the target and recorded it in
        # `deployments.rollback_of`, restricted to a release it observed
        # healthy. This adapter's own `_target_history` knows only which
        # versions it deployed in this process and nothing about how any of
        # them turned out, so consulting it first meant a reused adapter rolled
        # back to the last version it happened to deploy -- including one the
        # ledger had already recorded failed. In-process history is a fallback
        # for an adapter with no ledger to read, never a competing authority.
        recorded = self._ledger_prior_version(target, operation_key, attempted_digest)
        if recorded is not None:
            prior_records = [recorded]
        elif self._store is not None:
            # A ledger is present and names no target. That is an answer, not a
            # gap to fill from somewhere weaker.
            prior_records = []
        else:
            history = self._target_history.get(target_key, [])
            prior_records = [r for r in history
                             if r.get("artifact_digest") != attempted_digest]

        if not prior_records:
            outcome = production.DeploymentOutcome(
                reached=False,
                operation_ref=f"google-firebase-rollback:{target.record_site_id}:{target.record_channel_id}:rejected",
                adapter=self.name,
                detail=json.dumps({
                    "error": "NO_PREVIOUS_VERSION_FOR_ROLLBACK",
                    "detail": f"no prior platform version recorded for {target_key}",
                    "attempted_artifact_digest": attempted_digest,
                }, sort_keys=True),
            )
            self._recorded_operations[operation_key] = outcome
            return outcome

        target_version_record = prior_records[-1]
        target_version_id = target_version_record["version_id"]

        try:
            receipt = self._transport.rollback_release(
                target, target_version_id=target_version_id, operation_key=operation_key
            )
            restored_version_id = receipt.get("version_id", target_version_id)
            restored_release_id = receipt.get("release_id") or target_version_record.get("release_id")
            restored_artifact_digest = target_version_record.get("artifact_digest")
            op_ref = f"google-firebase-rollback:{target.record_site_id}:{target.record_channel_id}:{restored_version_id}"
            detail = json.dumps({
                "action": "rollback",
                "attempted_artifact_digest": attempted_digest,
                "channel_id": target.channel_id,
                "outcome": "recovered",
                "plan": target.plan,
                "project_id": target.project_id,
                "release_id": restored_release_id,
                "restored_artifact_digest": restored_artifact_digest,
                "restored_release_id": restored_release_id,
                "restored_version_id": restored_version_id,
                "site_id": target.site_id,
                "target_url": target.default_url,
                "version_id": restored_version_id,
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
                operation_ref=f"google-firebase-rollback:{target.record_site_id}:{target.record_channel_id}:uncertain",
                adapter=self.name,
                detail=json.dumps({"uncertain": str(exc), "operation_key": operation_key}, sort_keys=True),
            )
        except Exception as exc:  # noqa: BLE001
            if "uncertain" in str(exc).lower():
                outcome = production.DeploymentOutcome(
                    reached=None,
                    operation_ref=f"google-firebase-rollback:{target.record_site_id}:{target.record_channel_id}:uncertain",
                    adapter=self.name,
                    detail=json.dumps({"uncertain": str(exc)}, sort_keys=True),
                )
            else:
                outcome = production.DeploymentOutcome(
                    reached=False,
                    operation_ref=f"google-firebase-rollback:{target.record_site_id}:{target.record_channel_id}:failed",
                    adapter=self.name,
                    detail=json.dumps({"failed": str(exc)}, sort_keys=True),
                )

        self._recorded_operations[operation_key] = outcome
        return outcome


#: What a static surface's own ``/health.json`` has to say before the surface
#: counts as healthy.  The Casino artifact ships
#: ``{"app": "lodus-casino", "status": "ok"}``; the accepted words are listed
#: rather than inferred, because "any value that is not literally 'down'" is
#: how a typo becomes a pass.
HEALTHY_STATUS_VALUES = frozenset({"ok", "healthy", "pass", "passing", "up",
                                   "green", "serving"})

#: Boolean fields that carry the same claim under a different name.
HEALTHY_FLAG_KEYS = ("ok", "healthy", "up")


def health_body_ok(body: bytes,
                   expected: Mapping[str, Any] | None = None) -> bool:
    """Whether a ``/health.json`` document reports the surface healthy.

    A document that cannot be read, or that carries no health claim at all, is
    not a healthy document: the endpoint exists to make an assertion, and an
    absent assertion is ``unknown``, which is not a pass.  When the caller
    declares the exact body it expects, that comparison is the answer.
    """

    try:
        parsed = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return False
    if not isinstance(parsed, Mapping):
        return False
    if expected is not None:
        return all(parsed.get(key) == value for key, value in expected.items())
    status = parsed.get("status")
    if isinstance(status, str):
        return status.strip().lower() in HEALTHY_STATUS_VALUES
    for key in HEALTHY_FLAG_KEYS:
        flag = parsed.get(key)
        if isinstance(flag, bool):
            return flag
    return False


def _entry_proof(expected_entry_content: bytes | None) -> str:
    """What the probe compared the served entry document against.

    A digest names bytes that exist; the absence word names the fact that
    nothing was compared.  The two are different observations and the release
    lifecycle refuses the second where a healthy REVIEW is required.
    """

    if expected_entry_content is None:
        return "not_applicable"
    return "sha256:" + hashlib.sha256(expected_entry_content).hexdigest()


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
                return production.ProbedHealthRecord(
                    checks_passed=0,
                    checks_failed=1,
                    evidence_ref=f"health-probe://insecure-scheme/{parsed.scheme}",
                    observed_at=observed_at,
                    probe_target=base_url,
                    entry_proof=_entry_proof(expected_entry_content),
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
            return production.ProbedHealthRecord(
                checks_passed=0,
                checks_failed=1,
                evidence_ref=f"health-probe://unreachable/{parsed.netloc}",
                observed_at=observed_at,
                probe_target=base_url,
                entry_proof=_entry_proof(expected_entry_content),
            )

        # 3. Content integrity
        if expected_entry_content is not None:
            if body == expected_entry_content:
                passed += 1
            else:
                failed += 1

        # 4. App health endpoint -- what the document says, not merely that a
        #    document was served. A surface can answer 200 with a body that
        #    reports itself down; counting the status alone made "the app says
        #    it is broken" indistinguishable from "the app says it is fine",
        #    and settled the REVIEW healthy on the first one.
        health_url = f"{normalized_base}/health.json"
        try:
            h_status, h_body, _ = self._opener(health_url)
        except Exception:  # noqa: BLE001
            failed += 1
        else:
            if h_status == 200 and health_body_ok(h_body, expected_health_json):
                passed += 1
            else:
                failed += 1

        ref = f"health-probe://{parsed.netloc}/p{passed}-f{failed}@{int(observed_at)}"
        return production.ProbedHealthRecord(
            checks_passed=passed,
            checks_failed=failed,
            evidence_ref=ref,
            observed_at=observed_at,
            probe_target=base_url,
            entry_proof=_entry_proof(expected_entry_content),
        )


__all__ = [
    "ADAPTER_NAME",
    "DEFAULT_TARGET_CONFIGS",
    "DISALLOWED_BILLABLE_SERVICES",
    "FirebaseHostingDeploymentAdapter",
    "FirebaseHostingRestTransport",
    "FirebaseTransport",
    "GoogleTargetConfig",
    "HEALTHY_STATUS_VALUES",
    "SimulatedFirebaseTransport",
    "StaticWebHealthVerifier",
    "ZERO_COST_PLAN",
    "ZeroCostViolation",
    "file_system_artifact_resolver",
    "health_body_ok",
]
