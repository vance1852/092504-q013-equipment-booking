"""运行实训设备预约与维护封锁服务的离线端到端验收。"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .clock import FixedClock
from .equipment import EquipmentService
from .errors import ConflictError
from .storage import Database


def _confirm_kwargs(candidate: dict[str, object], **extra: object) -> dict[str, object]:
    return {
        "site_id": "site-001",
        "goal": "traction_debug",
        "equipment_id": candidate["equipment_id"],
        "expected_equipment_version": candidate["equipment_version"],
        "attachments": [{"attachment_id": item["attachment_id"],
                         "expected_version": item["attachment_version"]}
                        for item in candidate["attachments"]],
        "start_at": candidate["start_at"],
        "end_at": candidate["end_at"],
        **extra,
    }


def run() -> dict[str, object]:
    """执行一条完整的登记、预约、封锁与改期链并返回结果。"""

    with tempfile.TemporaryDirectory() as directory:
        database = Database(Path(directory) / "equipment_acceptance.sqlite3")
        service = EquipmentService(database, FixedClock(datetime(2026, 9, 25, 8, 0, tzinfo=timezone.utc)))
        service.register_organization(request_id="req-org", actor_id="bootstrap",
                                      organization_id="org-001", name="示范职业院校")
        service.register_actor(request_id="req-admin", actor_id="bootstrap", new_actor_id="admin-001",
                               display_name="系统管理员", role="admin", organization_id="org-001")
        service.register_actor(request_id="req-operator", actor_id="admin-001", new_actor_id="operator-001",
                               display_name="训练申请人", role="operator", organization_id="org-001")
        service.register_actor(request_id="req-maintainer", actor_id="admin-001", new_actor_id="maintainer-001",
                               display_name="设备维护员", role="maintainer", organization_id="org-001")
        service.register_site(request_id="req-site", actor_id="admin-001", site_id="site-001",
                              organization_id="org-001", name="轨道交通实训基地", timezone_name="Asia/Shanghai")
        service.register_equipment(request_id="req-eq-rail", actor_id="admin-001", equipment_id="eq-rail-01",
                                   site_id="site-001", name="轨道车辆台架", capability_version="CV-2026.09",
                                   capabilities=["traction_debug", "brake_test"])
        service.register_equipment(request_id="req-eq-drone", actor_id="admin-001", equipment_id="eq-drone-01",
                                   site_id="site-001", name="无人机装调套件", capability_version="CV-2026.08",
                                   capabilities=["drone_assembly", "traction_debug"])
        service.register_equipment(request_id="req-eq-dental", actor_id="admin-001", equipment_id="eq-dental-01",
                                   site_id="site-001", name="口腔加工设备", capability_version="CV-2026.07",
                                   capabilities=["crown_milling"])
        service.register_attachment(request_id="req-at-torque", actor_id="admin-001", attachment_id="at-torque-01",
                                    site_id="site-001", name="力矩扳手组", capability_version="AV-1.4",
                                    compatible_equipment=["*"])
        for request_id, certificate_id, resource_id, expires in (
            ("req-cert-rail", "cert-rail-01", "eq-rail-01", "2026-10-01T00:00:00Z"),
            ("req-cert-drone", "cert-drone-01", "eq-drone-01", "2026-09-01T00:00:00Z"),
            ("req-cert-dental", "cert-dental-01", "eq-dental-01", "2026-10-01T00:00:00Z"),
        ):
            service.register_calibration(request_id=request_id, actor_id="admin-001",
                                         certificate_id=certificate_id, resource_type="equipment",
                                         resource_id=resource_id, issuer="省计量院",
                                         issued_at="2026-08-01T00:00:00Z", expires_at=expires)
        service.register_calibration(request_id="req-cert-torque", actor_id="admin-001",
                                     certificate_id="cert-torque-01", resource_type="attachment",
                                     resource_id="at-torque-01", issuer="省计量院",
                                     issued_at="2026-08-01T00:00:00Z", expires_at="2026-10-01T00:00:00Z")
        for request_id, window_id, resource_id in (
            ("req-win-rail", "win-rail-01", "eq-rail-01"),
            ("req-win-drone", "win-drone-01", "eq-drone-01"),
            ("req-win-dental", "win-dental-01", "eq-dental-01"),
        ):
            service.register_open_window(request_id=request_id, actor_id="admin-001", window_id=window_id,
                                         resource_type="equipment", resource_id=resource_id,
                                         start_at="2026-09-26T08:00:00Z", end_at="2026-09-26T18:00:00Z")
        service.register_open_window(request_id="req-win-torque", actor_id="admin-001", window_id="win-torque-01",
                                     resource_type="attachment", resource_id="at-torque-01",
                                     start_at="2026-09-26T08:00:00Z", end_at="2026-09-26T18:00:00Z")
        service.register_changeover_rule(request_id="req-rule", actor_id="admin-001", rule_id="rule-rail-default",
                                         equipment_id="eq-rail-01", from_goal="*", to_goal="*", minutes=30)

        search = service.search_slots(site_id="site-001", goal="traction_debug",
                                      required_capabilities=["traction_debug"],
                                      attachment_ids=["at-torque-01"], duration_minutes=120,
                                      search_start="2026-09-26T08:00:00Z", search_end="2026-09-26T18:00:00Z")
        candidate = search["candidates"][0]
        first = service.confirm_reservation(request_id="req-res-1", actor_id="operator-001",
                                            **_confirm_kwargs(candidate))
        replay = service.confirm_reservation(request_id="req-res-1", actor_id="operator-001",
                                             **_confirm_kwargs(candidate))
        conflict_rejected = False
        try:
            service.confirm_reservation(request_id="req-res-1", actor_id="operator-001",
                                        **_confirm_kwargs(candidate, end_at="2026-09-26T11:00:00Z"))
        except ConflictError:
            conflict_rejected = True
        service.confirm_reservation(request_id="req-res-2", actor_id="operator-001",
                                    **_confirm_kwargs(candidate, start_at="2026-09-26T11:30:00Z",
                                                      end_at="2026-09-26T13:00:00Z"))
        service.publish_maintenance_block(request_id="req-block", actor_id="maintainer-001",
                                          resource_type="equipment", resource_id="eq-rail-01",
                                          start_at="2026-09-26T15:00:00Z", end_at="2026-09-26T17:00:00Z",
                                          reason="季度保养")

        service.clock = FixedClock(datetime(2026, 9, 26, 8, 30, tzinfo=timezone.utc))
        service.emergency_deactivate(request_id="req-stop", actor_id="maintainer-001",
                                     resource_type="equipment", resource_id="eq-rail-01",
                                     end_at="2026-09-26T12:00:00Z", reason="台架异响紧急停用")
        queue = service.list_reschedule_queue("site-001")
        risks = service.list_usage_risks("site-001", status="awaiting_decision")
        service.decide_usage_risk(request_id="req-risk", actor_id="maintainer-001",
                                  risk_id=risks[0]["risk_id"], decision="continue",
                                  note="现场检查无异常，允许完成本次训练")
        service.reactivate_resource(request_id="req-reactivate", actor_id="maintainer-001",
                                    resource_type="equipment", resource_id="eq-rail-01")
        research = service.search_slots(site_id="site-001", goal="traction_debug",
                                        required_capabilities=["traction_debug"],
                                        attachment_ids=["at-torque-01"], duration_minutes=120,
                                        search_start="2026-09-26T12:00:00Z", search_end="2026-09-26T18:00:00Z")
        new_candidate = research["candidates"][0]
        service.confirm_reservation(request_id="req-res-3", actor_id="operator-001",
                                    **_confirm_kwargs(new_candidate,
                                                      reschedule_entry_id=queue[0]["entry_id"]))
        valid, event_count = service.verify_audit()
        result = {
            "status": "ok",
            "candidates": len(search["candidates"]),
            "unavailable": len(search["unavailable"]),
            "first_replayed": first.replayed,
            "second_replayed": replay.replayed,
            "conflict_rejected": conflict_rejected,
            "displaced": len(queue),
            "risks_flagged": len(risks),
            "queue_pending_after_rebook": len(service.list_reschedule_queue("site-001")),
            "manual_overrides": len(service.list_manual_overrides("site-001")),
            "audit_events": event_count,
            "audit_valid": valid,
        }
        database.close()
        return result


def main() -> int:
    """打印验收结果并设置退出码。"""

    result = run()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    ok = (result["status"] == "ok" and result["audit_valid"] and result["conflict_rejected"]
          and result["second_replayed"] and result["queue_pending_after_rebook"] == 0)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
