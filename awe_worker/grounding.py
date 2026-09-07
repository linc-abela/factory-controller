"""Factory Context Broker grounding adapter and fallback inspection.

Context Broker is the preferred fast bounded grounding source when its manifest
and overview are fresh, source-revision bound, and sufficient. Direct git inspection
is the mandatory fallback when the broker is unavailable, stale, or incomplete.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Mapping

from .model import AWEWorkItem, GroundingResult


def get_repo_head_sha(repo_path: str | Path) -> str:
    """Read the authoritative current HEAD commit SHA of a local Git checkout."""
    try:
        res = subprocess.run(
            ["git", "-C", str(repo_path), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        )
        return res.stdout.strip()
    except subprocess.SubprocessError as exc:
        raise RuntimeError(f"Failed to read HEAD from git repo {repo_path}: {exc}") from exc


def get_repo_identity(repo_path: str | Path) -> str:
    """Read or normalize repository identity from git remote or local path."""
    try:
        res = subprocess.run(
            ["git", "-C", str(repo_path), "config", "--get", "remote.origin.url"],
            capture_output=True,
            text=True,
            check=False,
        )
        if res.returncode == 0 and res.stdout.strip():
            url = res.stdout.strip()
            return url.removesuffix(".git").rstrip("/")
    except Exception:
        pass
    return f"local:{Path(repo_path).resolve().name}"


class ContextBrokerGrounder:
    """Grounding manager that interfaces with Context Broker with strict freshness verification."""

    def __init__(
        self,
        broker_bin_or_dev: str | Path | None = None,
        cache_dir: str | Path | None = None,
    ) -> None:
        self.broker_bin_or_dev = broker_bin_or_dev
        self.cache_dir = Path(cache_dir or os.environ.get("FACTORY_CONTEXT_BROKER_CACHE", tempfile.gettempdir() + "/.broker-cache"))
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def ground_task(
        self,
        repo_path: str | Path,
        task: AWEWorkItem,
        requested_paths: list[str] | None = None,
        max_bytes: int = 131072,
        max_files: int = 64,
        force_fallback: bool = False,
    ) -> GroundingResult:
        """Ground a task using Context Broker if fresh and valid, or fall back to direct Git."""
        repo = Path(repo_path).resolve()
        start_time = time.time()

        try:
            head_sha = get_repo_head_sha(repo)
            repo_id = get_repo_identity(repo)
        except Exception as exc:
            return GroundingResult(
                ok=False,
                source="error",
                repo_identity="unknown",
                head_sha="unknown",
                fallback_reason=f"Repository access error: {exc}",
            )

        if not force_fallback:
            # Attempt Broker-first grounding
            broker_res = self._try_broker(
                repo=repo,
                repo_id=repo_id,
                head_sha=head_sha,
                requested_paths=requested_paths or [],
                max_bytes=max_bytes,
                max_files=max_files,
            )
            if broker_res is not None:
                latency_ms = (time.time() - start_time) * 1000.0
                full_b = int(broker_res.get("full_eligible_bytes", 0) or 0)
                sel_b = int(broker_res.get("selected_bytes", 0) or 0)
                if full_b <= 0 or sel_b < 0 or sel_b > full_b:
                    broker_res = None
                else:
                    reduction = 1.0 - (sel_b / full_b)
                    return GroundingResult(
                        ok=True,
                        source="broker",
                        repo_identity=repo_id,
                        head_sha=head_sha,
                        manifest_digest=broker_res.get("manifest_digest", ""),
                        manifest_ref=broker_res.get("manifest_ref", {}),
                        selected_paths=broker_res.get("selected_paths", []),
                        overview=broker_res.get("overview", {}),
                        full_eligible_bytes=full_b,
                        selected_bytes=sel_b,
                        reduction_ratio=reduction,
                        latency_ms=latency_ms,
                    )

        # Fallback to direct Git repository inspection
        fallback_res = self._direct_git_fallback(repo, head_sha, repo_id, requested_paths)
        latency_ms = (time.time() - start_time) * 1000.0
        full_b = int(fallback_res.get("full_eligible_bytes", 0) or 0)
        sel_b = int(fallback_res.get("selected_bytes", 0) or 0)
        reduction = fallback_res.get("reduction_ratio")
        if reduction is None:
            reduction = (1.0 - (sel_b / full_b)) if full_b > 0 else 0.0
        return GroundingResult(
            ok=True,
            source="direct_git_fallback",
            repo_identity=repo_id,
            head_sha=head_sha,
            selected_paths=fallback_res.get("selected_paths", []),
            overview=fallback_res.get("overview", {}),
            full_eligible_bytes=full_b,
            selected_bytes=sel_b,
            reduction_ratio=float(reduction),
            latency_ms=latency_ms,
            fallback_reason="broker_unavailable_or_stale" if not force_fallback else "forced_fallback",
        )

    def _try_broker(
        self,
        repo: Path,
        repo_id: str,
        head_sha: str,
        requested_paths: list[str],
        max_bytes: int,
        max_files: int,
    ) -> dict[str, Any] | None:
        """Attempt to build and validate a context manifest using factory_context_broker."""
        # 1. Try python module import directly if available
        try:
            import factory_context_broker.broker as fcb
            request_data = {
                "repo_identity": repo_id,
                "baseline": head_sha,
                "head": head_sha,
                "required_anchors": requested_paths[:10] if requested_paths else ["README.md"],
                "always_include": requested_paths[:10] if requested_paths else ["README.md"],
                "requested_paths": requested_paths,
                "overview": ["authoritative", "runtime", "execution", "tests"],
                "max_bytes": max_bytes,
                "max_files": max_files,
            }
            broker = fcb.Broker(cache_dir=self.cache_dir)
            receipt = broker.build(repo, request_data)
            manifest = receipt.manifest

            # Strict freshness, identity, provenance, and path bounds
            if not self._manifest_is_valid(manifest, repo=repo, repo_id=repo_id, head_sha=head_sha):
                return None

            economics = manifest.get("economics", {})
            selected = [item["path"] for item in manifest.get("selected", [])]
            return {
                "manifest_digest": manifest.get("manifest_digest", ""),
                "manifest_ref": receipt.manifest_ref.as_dict() if hasattr(receipt.manifest_ref, "as_dict") else {},
                "selected_paths": selected,
                "overview": manifest.get("overview", {}),
                "full_eligible_bytes": economics.get("full_eligible_bytes", 0),
                "selected_bytes": economics.get("selected_bytes", 0),
            }
        except Exception:
            pass

        # 2. Try CLI / dev script
        dev_script = self.broker_bin_or_dev
        if not dev_script:
            candidate = Path("/Users/Shared/Projects/software-factory/factory-context-broker/dev")
            if candidate.exists() and os.access(candidate, os.X_OK):
                dev_script = candidate

        if dev_script and Path(dev_script).exists():
            try:
                # If using factory-context-broker/dev, map host paths to container mounts
                is_fcb_dev = "factory-context-broker" in str(dev_script)
                if is_fcb_dev:
                    broker_root = Path(dev_script).resolve().parent
                    container_cache = Path("/workspace/.broker-cache")
                    host_cache = broker_root / ".broker-cache"
                    host_cache.mkdir(parents=True, exist_ok=True)
                    req_host_path = host_cache / f"req_{int(time.time()*1000)}.json"
                    req_container_path = container_cache / req_host_path.name
                    # Map repo: e.g. /Users/Shared/Projects/software-factory/X -> /projects/X
                    container_repo = f"/projects/{repo.name}"
                else:
                    req_host_path = self.cache_dir / f"req_{int(time.time()*1000)}.json"
                    req_container_path = req_host_path
                    container_repo = str(repo)
                    container_cache = self.cache_dir

                request_payload = {
                    "repo_identity": repo_id,
                    "baseline": head_sha,
                    "head": head_sha,
                    "required_anchors": requested_paths[:10] if requested_paths else ["README.md"],
                    "always_include": requested_paths[:10] if requested_paths else ["README.md"],
                    "requested_paths": requested_paths,
                    "overview": ["authoritative", "runtime", "execution", "tests"],
                    "max_bytes": max_bytes,
                    "max_files": max_files,
                    "max_file_bytes": 262144,
                    "admit_oversized_paths": ["*"],
                }

                req_host_path.write_text(json.dumps(request_payload), encoding="utf-8")

                cmd = [
                    str(dev_script),
                    "build",
                    "--repo",
                    container_repo,
                    "--request",
                    str(req_container_path),
                    "--cache-dir",
                    str(container_cache),
                ]
                proc = subprocess.run(
                    cmd,
                    cwd=str(broker_root) if is_fcb_dev else None,
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                try:
                    req_host_path.unlink(missing_ok=True)
                except Exception:
                    pass

                if proc.returncode == 0:
                    out = json.loads(proc.stdout)
                    manifest = out.get("manifest", {})
                    # Freshness check
                    if not self._manifest_is_valid(manifest, repo=repo, repo_id=repo_id, head_sha=head_sha):
                        return None
                    economics = manifest.get("economics", {})
                    selected = [item["path"] for item in manifest.get("selected", [])]
                    return {
                        "manifest_digest": manifest.get("manifest_digest", ""),
                        "manifest_ref": out.get("manifest_ref", {}),
                        "selected_paths": selected,
                        "overview": manifest.get("overview", {}),
                        "full_eligible_bytes": economics.get("full_eligible_bytes", 0),
                        "selected_bytes": economics.get("selected_bytes", 0),
                    }
            except Exception:
                pass

        return None

    def _manifest_is_valid(
        self,
        manifest: Mapping[str, Any] | Any,
        repo: Path,
        repo_id: str,
        head_sha: str,
    ) -> bool:
        if not isinstance(manifest, dict):
            return False
        if manifest.get("head") != head_sha:
            return False
        identity = str(manifest.get("repo_identity") or manifest.get("repository") or "")
        if identity and identity.rstrip("/") != repo_id.rstrip("/"):
            return False
        digest = str(manifest.get("manifest_digest") or "")
        if not digest:
            return False
        selected_items = manifest.get("selected") or []
        for item in selected_items:
            path = item.get("path") if isinstance(item, dict) else None
            if not path or Path(str(path)).is_absolute() or ".." in Path(str(path)).parts:
                return False
        economics = manifest.get("economics") or {}
        try:
            full_b = int(economics.get("full_eligible_bytes", 0) or 0)
            sel_b = int(economics.get("selected_bytes", 0) or 0)
        except (TypeError, ValueError):
            return False
        if full_b <= 0 or sel_b < 0 or sel_b > full_b:
            return False
        if economics.get("estimated") is True:
            return False
        return True

    def _direct_git_fallback(
        self,
        repo: Path,
        head_sha: str,
        repo_id: str,
        requested_paths: list[str] | None,
    ) -> dict[str, Any]:
        """Direct Git inspection fallback: reads git tracked files and bounded overview."""
        selected_paths: list[str] = []
        total_bytes = 0
        selected_bytes = 0

        # Read top-level tracked files
        try:
            res = subprocess.run(
                ["git", "-C", str(repo), "ls-tree", "-r", "-l", head_sha],
                capture_output=True,
                text=True,
                check=True,
            )
            all_files: list[str] = []
            sizes: dict[str, int] = {}
            for line in res.stdout.splitlines():
                # <mode> <type> <object> <size>\t<path>
                try:
                    meta, path = line.split("\t", 1)
                    parts = meta.split()
                    size = int(parts[3]) if len(parts) >= 4 and parts[3] != "-" else 0
                except ValueError:
                    continue
                path = path.strip()
                if not path:
                    continue
                all_files.append(path)
                sizes[path] = size
                total_bytes += size
        except Exception:
            all_files = []
            sizes = {}

        # Find authoritative files like README, MISSION, etc.
        authoritative = [
            f for f in all_files if any(p in f.lower() for p in ("readme", "mission", "contributing", "design"))
        ]
        target_selection = list(requested_paths or [])
        for a in authoritative:
            if a not in target_selection:
                target_selection.append(a)

        selected_paths = target_selection[:20]
        selected_bytes = sum(sizes.get(p, 0) for p in selected_paths)
        if total_bytes <= 0:
            total_bytes = selected_bytes
        reduction = (1.0 - (selected_bytes / total_bytes)) if total_bytes > 0 else 0.0
        return {
            "selected_paths": selected_paths,
            "overview": {
                "tracked_file_count": len(all_files),
                "authoritative": authoritative[:10],
                "economics_mode": "measured_git_ls_tree",
            },
            "full_eligible_bytes": total_bytes,
            "selected_bytes": selected_bytes,
            "reduction_ratio": reduction,
        }
