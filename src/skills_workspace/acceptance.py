"""运行基础服务的离线端到端验收。"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .booking import BookingService
from .clock import FixedClock
from .storage import Database


def run() -> dict[str, object]:
    """执行一条完整登记链并返回结果。"""

    with tempfile.TemporaryDirectory() as directory:
        database = Database(Path(directory) / "acceptance.sqlite3")
        service = BookingService(database, FixedClock(datetime(2026, 9, 25, 8, 0, tzinfo=timezone.utc)))
        service.register_organization(request_id="req-org", actor_id="bootstrap",
                                      organization_id="org-001", name="示范训练机构")
        service.register_actor(request_id="req-admin", actor_id="bootstrap", new_actor_id="admin-001",
                               display_name="系统管理员", role="admin", organization_id="org-001")
        service.register_actor(request_id="req-operator", actor_id="admin-001", new_actor_id="operator-001",
                               display_name="训练负责人", role="operator", organization_id="org-001")
        service.register_site(request_id="req-site", actor_id="operator-001", site_id="site-001",
                              organization_id="org-001", name="一号生产场所", timezone_name="Asia/Shanghai")
        first = service.record_domain_data(request_id="req-data", actor_id="operator-001", site_id="site-001",
                                           category="institution_profile", external_key="record-001",
                                           data={"name": "基础资料", "enabled": True})
        replay = service.record_domain_data(request_id="req-data", actor_id="operator-001", site_id="site-001",
                                            category="institution_profile", external_key="record-001",
                                            data={"name": "基础资料", "enabled": True})

        # 登记轨道车辆台架、可组合附件、校准证书、开放窗口与转换规则。
        service.register_equipment(request_id="req-equipment", actor_id="operator-001", site_id="site-001",
                                   equipment_id="rail-bench-001", name="轨道车辆台架", kind="rail_bench",
                                   capabilities=["bogie_run", "brake_test"], capability_version=3)
        service.register_accessory(request_id="req-accessory", actor_id="operator-001", site_id="site-001",
                                   accessory_id="sensor-pack-001", name="振动采集套件",
                                   capabilities=["vibration"], capability_version=2,
                                   compatible_equipment=["rail-bench-001"])
        service.register_certificate(request_id="req-cert-eq", actor_id="operator-001",
                                     target_type="equipment", target_id="rail-bench-001",
                                     certificate_no="JL-2026-0001", basis="JJF 轨道台架校准规范",
                                     calibrated_at="2026-09-01T00:00Z", expires_at="2026-12-01T00:00Z")
        service.register_certificate(request_id="req-cert-acc", actor_id="operator-001",
                                     target_type="accessory", target_id="sensor-pack-001",
                                     certificate_no="JL-2026-0002", basis="JJF 传感器校准规范",
                                     calibrated_at="2026-09-01T00:00Z", expires_at="2026-12-01T00:00Z")
        service.register_open_window(request_id="req-window", actor_id="operator-001",
                                     equipment_id="rail-bench-001",
                                     starts_at="2026-09-26T00:00Z", ends_at="2026-10-02T00:00Z")
        service.register_transition_rule(request_id="req-rule", actor_id="operator-001", site_id="site-001",
                                         equipment_id="rail-bench-001", from_setup="bogie",
                                         to_setup="brake", minutes=30)

        # 提交训练目标，取得候选时段。
        inquiry = service.submit_inquiry(
            actor_id="operator-001", site_id="site-001", objective="转向架与制动联动训练",
            setup="bogie", duration_minutes=90, earliest_start="2026-09-26T01:00Z",
            latest_end="2026-09-26T06:00Z", capabilities=["bogie_run"],
            accessory_requirements=[{"capability": "vibration"}], priority_tier=1)
        candidate = next(item for item in inquiry["candidates"]
                         if item["starts_at"] == "2026-09-26T01:00Z")
        booked = service.confirm_reservation(
            request_id="req-book", actor_id="operator-001", inquiry_id=inquiry["inquiry_id"],
            equipment_id="rail-bench-001", accessory_ids=candidate["accessory_ids"],
            starts_at=candidate["starts_at"], ends_at=candidate["ends_at"],
            expected_equipment_version=3, expected_accessory_versions=candidate["accessory_versions"])
        book_replay = service.confirm_reservation(
            request_id="req-book", actor_id="operator-001", inquiry_id=inquiry["inquiry_id"],
            equipment_id="rail-bench-001", accessory_ids=candidate["accessory_ids"],
            starts_at=candidate["starts_at"], ends_at=candidate["ends_at"],
            expected_equipment_version=3, expected_accessory_versions=candidate["accessory_versions"])

        # 紧急停用：未来预约进入改期队列，再原子改期到封锁之后。
        service.publish_blockade(request_id="req-blockade", actor_id="admin-001",
                                 equipment_id="rail-bench-001", kind="emergency",
                                 reason="台架异响紧急停用",
                                 starts_at="2026-09-26T01:30Z", ends_at="2026-09-26T12:00Z")
        queue = service.list_reschedule_queue("site-001")
        rebooked = service.rebook_queue_entry(request_id="req-rebook", actor_id="operator-001",
                                              queue_id=queue[0]["queue_id"],
                                              starts_at="2026-09-26T13:00Z")
        occupancy = service.resource_occupancy(
            "equipment", "rail-bench-001", "2026-09-26T00:00Z", "2026-09-27T00:00Z")
        valid, event_count = service.verify_audit()
        records = service.list_domain_data("site-001")
        result = {"status": "ok", "records": len(records), "audit_events": event_count,
                  "audit_valid": valid, "first_replayed": first.replayed,
                  "second_replayed": replay.replayed,
                  "candidate_count": len(inquiry["candidates"]),
                  "book_replayed": book_replay.replayed,
                  "book_id_matches": booked.resource_id == book_replay.resource_id,
                  "queue_size": len(queue), "queue_priority": queue[0]["priority_tier"],
                  "rebooked_confirmed": any(
                      item["reservation_id"] == rebooked.resource_id and item["status"] == "confirmed"
                      for item in service.list_reservations("site-001")),
                  "occupancy_kinds": sorted({item["kind"] for item in occupancy["busy"]})}
        database.close()
        return result


def main() -> int:
    """打印验收结果并设置退出码。"""

    result = run()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] == "ok" and result["audit_valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
