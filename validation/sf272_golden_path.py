"""Live SF-272 golden-path runner. Notion is not consulted."""

from __future__ import annotations

import json
from pathlib import Path

from factory_controller import golden_path, pcp_missions
from factory_controller.store import MissionStore

VAULT = Path("/Users/Shared/Projects/factory-vault-SF-272")
STATE = Path("/Users/Shared/Projects/software-factory/factory-controller-SF-272/.sf272-state")


def main() -> int:
    STATE.mkdir(parents=True, exist_ok=True)
    store = MissionStore(STATE / "controller.db")
    plane = pcp_missions.PCPMissionPlane(store, VAULT)
    executors = golden_path.FleetExecutors(vault_root=VAULT, state_dir=STATE)

    plane.sync()
    kyri = next((row for row in plane.list() if "kyriedachi" in row.package_id), None)
    if kyri is None:
        print("SF272_FACTORY_GOLDEN_PATH_REJECT — pcp")
        return 1
    plane.claim(kyri.mission_key, "sf272-live")
    try:
        store.submit(
            {
                "work_item_id": "%s:build" % kyri.package_id,
                "project_id": kyri.package_id,
                "source_pcp": kyri.canonical_path,
                "package_digest": kyri.package_digest,
                "lifecycle": kyri.lifecycle,
            },
            kyri.mission_key,
        )
    except Exception:
        pass
    evidence = golden_path.run(
        kyri, vault_root=VAULT, state_dir=STATE, executors=executors)
    plane.record_evidence(kyri.mission_key, evidence)
    url = str((evidence.get("rc_alpha") or {}).get("url") or "")
    if url:
        try:
            plane.set_rc_url(kyri.mission_key, alpha=url, evidence=evidence)
        except Exception:
            pass
    kyri = next(row for row in plane.list() if row.mission_key == kyri.mission_key)
    line = golden_path.accept_line(kyri.evidence or {})
    out = STATE / "evidence.json"
    out.write_text(json.dumps({
        "line": line,
        "mission": None if kyri is None else kyri.as_row(),
        "evidence": evidence,
    }, indent=2) + "\n", encoding="utf-8")
    print(line)
    print("evidence:", out)
    if kyri is not None:
        print("mission:", kyri.mission_key)
        print("lifecycle:", kyri.lifecycle)
        print("rc_alpha:", kyri.rc_alpha_url)
    return 0 if line.startswith("SF272_FACTORY_GOLDEN_PATH_ACCEPT") else 1


if __name__ == "__main__":
    raise SystemExit(main())
