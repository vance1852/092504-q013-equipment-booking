import unittest
from datetime import datetime, timezone

from skills_workspace.booking import BookingService
from skills_workspace.clock import FixedClock
from skills_workspace.errors import (
    ConflictError,
    NotFoundError,
    PermissionDenied,
    ValidationError,
)
from skills_workspace.storage import Database


class BookingTest(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 26, 1, 30, tzinfo=timezone.utc)
        self.database = Database()
        self.service = BookingService(self.database, FixedClock(self.now))
        self.service.register_organization(
            request_id="org", actor_id="bootstrap", organization_id="o1", name="训练机构")
        self.service.register_actor(
            request_id="admin", actor_id="bootstrap", new_actor_id="a1",
            display_name="管理员", role="admin", organization_id="o1")
        self.service.register_actor(
            request_id="auditor", actor_id="a1", new_actor_id="au1",
            display_name="审计员", role="auditor", organization_id="o1")
        self.service.register_site(
            request_id="site", actor_id="a1", site_id="s1", organization_id="o1",
            name="实训场地", timezone_name="Asia/Shanghai")

    def tearDown(self):
        self.database.close()

    def _register_bench(self, equipment_id="bench1", accessory_ids=("sensor1",),
                        equipment_caps=("bogie_run",), accessory_caps=("vibration",)):
        self.service.register_equipment(
            request_id="eq-" + equipment_id, actor_id="a1", site_id="s1",
            equipment_id=equipment_id, name="轨道车辆台架", kind="rail_bench",
            capabilities=list(equipment_caps), capability_version=1)
        for accessory_id in accessory_ids:
            self.service.register_accessory(
                request_id="acc-" + accessory_id, actor_id="a1", site_id="s1",
                accessory_id=accessory_id, name="装调套件",
                capabilities=list(accessory_caps), capability_version=1,
                compatible_equipment=[equipment_id])
            self.service.register_certificate(
                request_id="cert-a-" + accessory_id, actor_id="a1",
                target_type="accessory", target_id=accessory_id,
                certificate_no="CA-" + accessory_id, basis="附件校准规范",
                calibrated_at="2026-09-01T00:00Z", expires_at="2026-12-01T00:00Z")
        self.service.register_certificate(
            request_id="cert-e-" + equipment_id, actor_id="a1", target_type="equipment",
            target_id=equipment_id, certificate_no="CE-" + equipment_id, basis="台架校准规范",
            calibrated_at="2026-09-01T00:00Z", expires_at="2026-12-01T00:00Z")
        self.service.register_open_window(
            request_id="win-" + equipment_id, actor_id="a1", equipment_id=equipment_id,
            starts_at="2026-09-25T00:00Z", ends_at="2026-10-02T00:00Z")

    def _inquiry(self, **overrides):
        arguments = dict(
            actor_id="a1", site_id="s1", objective="转向架调试", setup="bogie",
            duration_minutes=60, earliest_start="2026-09-26T01:00Z",
            latest_end="2026-09-26T06:00Z", capabilities=["bogie_run"],
            accessory_requirements=[{"capability": "vibration"}])
        arguments.update(overrides)
        return self.service.submit_inquiry(**arguments)

    # ------------------------------------------------------------ 候选与原因

    def test_inquiry_returns_candidates_with_versions_and_certificates(self):
        self._register_bench()
        inquiry = self._inquiry()
        self.assertTrue(inquiry["candidates"])
        first = inquiry["candidates"][0]
        self.assertEqual("bench1", first["equipment_id"])
        self.assertEqual(["sensor1"], first["accessory_ids"])
        self.assertEqual(1, first["equipment_version"])
        self.assertEqual(1, first["accessory_versions"]["sensor1"])
        self.assertIn("equipment", first["certificates"])
        self.assertEqual([], first["violations"])

    def test_inquiry_explains_missing_capability(self):
        self._register_bench(equipment_caps=("milling",))
        inquiry = self._inquiry()
        self.assertEqual([], inquiry["candidates"])
        self.assertEqual(["missing_capability"], inquiry["unavailable"][0]["reasons"])
        self.assertEqual(["bogie_run"],
                         inquiry["unavailable"][0]["details"]["missing_capabilities"])

    def test_calibration_expiry_is_flagged_but_waivable(self):
        # 只登记在查询窗口内到期的设备证书。
        self.service.register_equipment(
            request_id="eq-short", actor_id="a1", site_id="s1", equipment_id="bench1",
            name="轨道车辆台架", kind="rail_bench", capabilities=["bogie_run"],
            capability_version=1)
        self.service.register_accessory(
            request_id="acc-short", actor_id="a1", site_id="s1", accessory_id="sensor1",
            name="装调套件", capabilities=["vibration"], capability_version=1,
            compatible_equipment=["bench1"])
        self.service.register_certificate(
            request_id="cert-a-short", actor_id="a1", target_type="accessory",
            target_id="sensor1", certificate_no="CA1", basis="附件校准规范",
            calibrated_at="2026-09-01T00:00Z", expires_at="2026-12-01T00:00Z")
        self.service.register_certificate(
            request_id="cert-short", actor_id="a1", target_type="equipment",
            target_id="bench1", certificate_no="CE-SHORT", basis="台架校准规范",
            calibrated_at="2026-08-01T00:00Z", expires_at="2026-09-26T02:00Z")
        self.service.register_open_window(
            request_id="win-short", actor_id="a1", equipment_id="bench1",
            starts_at="2026-09-25T00:00Z", ends_at="2026-10-02T00:00Z")
        inquiry = self._inquiry(earliest_start="2026-09-26T01:00Z",
                                latest_end="2026-09-26T04:00Z")
        # 结束时刻恰好等于到期时刻，半开区间内仍然有效。
        clean = {c["starts_at"] for c in inquiry["candidates"] if not c["violations"]}
        self.assertIn("2026-09-26T01:00Z", clean)
        expired = next(c for c in inquiry["candidates"]
                       if c["starts_at"] == "2026-09-26T01:01Z")
        self.assertIn("calibration_expired", expired["waivers_required"])

    def test_transition_preparation_blocks_adjacent_slots(self):
        self._register_bench()
        self.service.register_transition_rule(
            request_id="rule", actor_id="a1", site_id="s1", equipment_id="bench1",
            from_setup="bogie", to_setup="drone", minutes=30)
        first_inquiry = self._inquiry(objective="第一项")
        first = next(c for c in first_inquiry["candidates"]
                     if c["starts_at"] == "2026-09-26T01:00Z")
        self.service.confirm_reservation(
            request_id="book-a", actor_id="a1", inquiry_id=first_inquiry["inquiry_id"],
            equipment_id="bench1", accessory_ids=first["accessory_ids"],
            starts_at=first["starts_at"], ends_at=first["ends_at"])
        second = self._inquiry(objective="第二项", setup="drone",
                               earliest_start="2026-09-26T02:00Z",
                               latest_end="2026-09-26T03:30Z")
        clean = {c["starts_at"] for c in second["candidates"] if not c["violations"]}
        flagged = {c["starts_at"] for c in second["candidates"]
                   if "transition_preparation" in c["waivers_required"]}
        self.assertNotIn("2026-09-26T02:00Z", clean)
        self.assertIn("2026-09-26T02:00Z", flagged)
        self.assertIn("2026-09-26T02:30Z", clean)

    # ------------------------------------------------------------ 确认与占用

    def test_confirm_checks_versions_and_records_calibration_basis(self):
        self._register_bench()
        inquiry = self._inquiry()
        first = inquiry["candidates"][0]
        receipt = self.service.confirm_reservation(
            request_id="book-1", actor_id="a1", inquiry_id=inquiry["inquiry_id"],
            equipment_id="bench1", accessory_ids=first["accessory_ids"],
            starts_at=first["starts_at"], ends_at=first["ends_at"],
            expected_equipment_version=1,
            expected_accessory_versions=first["accessory_versions"])
        self.assertFalse(receipt.replayed)
        reservations = self.service.list_reservations("s1")
        self.assertEqual("confirmed", reservations[0]["status"])
        self.assertEqual("CE-bench1",
                         reservations[0]["calibration_basis"]["equipment"]["certificate_no"])
        self.assertEqual("CA-sensor1",
                         reservations[0]["calibration_basis"]["accessories"]["sensor1"]["certificate_no"])

    def test_confirm_rejects_stale_equipment_version(self):
        self._register_bench()
        inquiry = self._inquiry()
        first = inquiry["candidates"][0]
        with self.assertRaises(ConflictError):
            self.service.confirm_reservation(
                request_id="book-stale", actor_id="a1", inquiry_id=inquiry["inquiry_id"],
                equipment_id="bench1", accessory_ids=first["accessory_ids"],
                starts_at=first["starts_at"], ends_at=first["ends_at"],
                expected_equipment_version=99)

    def test_group_occupancy_is_atomic(self):
        self._register_bench(accessory_ids=("sensor1", "sensor2"))
        # 第二台设备只与 sensor2 组合，先用它在 03:00-04:00 占用 sensor2。
        self.service.register_equipment(
            request_id="eq-bench2", actor_id="a1", site_id="s1", equipment_id="bench2",
            name="备用台架", kind="rail_bench", capabilities=["bogie_run"],
            capability_version=1)
        self.service.register_compatibility(
            request_id="link2", actor_id="a1", equipment_id="bench2",
            accessory_id="sensor2")
        self.service.register_certificate(
            request_id="cert-e-bench2", actor_id="a1", target_type="equipment",
            target_id="bench2", certificate_no="CE2", basis="台架校准规范",
            calibrated_at="2026-09-01T00:00Z", expires_at="2026-12-01T00:00Z")
        self.service.register_open_window(
            request_id="win-bench2", actor_id="a1", equipment_id="bench2",
            starts_at="2026-09-25T00:00Z", ends_at="2026-10-02T00:00Z")
        holder_inquiry = self._inquiry(
            equipment_ids=["bench2"],
            earliest_start="2026-09-26T03:00Z", latest_end="2026-09-26T04:00Z")
        holder = holder_inquiry["candidates"][0]
        self.assertEqual(["sensor2"], holder["accessory_ids"])
        self.service.confirm_reservation(
            request_id="holder", actor_id="a1", inquiry_id=holder_inquiry["inquiry_id"],
            equipment_id="bench2", accessory_ids=["sensor2"],
            starts_at=holder["starts_at"], ends_at=holder["ends_at"])
        # 整组槽位插入时附件命中唯一约束，同事务里的设备槽位必须一起回滚。
        connection = self.database.connection
        connection.execute("BEGIN IMMEDIATE")
        with self.assertRaises(ConflictError):
            self.service._insert_slots(
                connection, "ghost",
                datetime(2026, 9, 26, 3, tzinfo=timezone.utc),
                datetime(2026, 9, 26, 4, tzinfo=timezone.utc),
                "bench-free", ["sensor2"])
        connection.rollback()
        leftovers = connection.execute(
            "SELECT COUNT(*) AS c FROM booking_reservation_slots WHERE reservation_id='ghost'"
        ).fetchone()["c"]
        self.assertEqual(0, leftovers)
        # 未参与冲突的 sensor1 仍可与 bench1 组成候选。
        fresh = self._inquiry(
            equipment_ids=["bench1"],
            earliest_start="2026-09-26T03:00Z", latest_end="2026-09-26T04:00Z")
        self.assertEqual(["sensor1"], fresh["candidates"][0]["accessory_ids"])

    def test_confirm_fails_whole_group_when_accessory_taken_between_inquiry_and_confirm(self):
        self._register_bench()
        # 第二台设备共用 sensor1。
        self.service.register_equipment(
            request_id="eq-bench2", actor_id="a1", site_id="s1", equipment_id="bench2",
            name="备用台架", kind="rail_bench", capabilities=["bogie_run"],
            capability_version=1)
        self.service.register_compatibility(
            request_id="link2", actor_id="a1", equipment_id="bench2",
            accessory_id="sensor1")
        self.service.register_certificate(
            request_id="cert-e-bench2", actor_id="a1", target_type="equipment",
            target_id="bench2", certificate_no="CE2", basis="台架校准规范",
            calibrated_at="2026-09-01T00:00Z", expires_at="2026-12-01T00:00Z")
        self.service.register_open_window(
            request_id="win-bench2", actor_id="a1", equipment_id="bench2",
            starts_at="2026-09-25T00:00Z", ends_at="2026-10-02T00:00Z")
        first = self._inquiry(
            equipment_ids=["bench1"],
            earliest_start="2026-09-26T03:00Z", latest_end="2026-09-26T04:00Z")
        racer = self._inquiry(
            objective="抢占", equipment_ids=["bench2"],
            earliest_start="2026-09-26T03:00Z", latest_end="2026-09-26T04:00Z")
        racer_candidate = racer["candidates"][0]
        self.service.confirm_reservation(
            request_id="racer", actor_id="a1", inquiry_id=racer["inquiry_id"],
            equipment_id="bench2", accessory_ids=racer_candidate["accessory_ids"],
            starts_at=racer_candidate["starts_at"], ends_at=racer_candidate["ends_at"])
        stale = first["candidates"][0]
        with self.assertRaises(ConflictError):
            self.service.confirm_reservation(
                request_id="late", actor_id="a1", inquiry_id=first["inquiry_id"],
                equipment_id="bench1", accessory_ids=stale["accessory_ids"],
                starts_at=stale["starts_at"], ends_at=stale["ends_at"])
        # bench1 在该时段仍未被占用，可以重新预约别的时间。
        later = self._inquiry(
            equipment_ids=["bench1"],
            earliest_start="2026-09-26T04:00Z", latest_end="2026-09-26T05:00Z")
        self.assertTrue(later["candidates"])

    def test_same_request_replays_safe_and_changed_payload_conflicts(self):
        self._register_bench()
        inquiry = self._inquiry()
        first = inquiry["candidates"][0]
        second = inquiry["candidates"][1]
        arguments = dict(actor_id="a1", inquiry_id=inquiry["inquiry_id"],
                         equipment_id="bench1", accessory_ids=first["accessory_ids"],
                         starts_at=first["starts_at"], ends_at=first["ends_at"])
        one = self.service.confirm_reservation(request_id="idem", **arguments)
        replay = self.service.confirm_reservation(request_id="idem", **arguments)
        self.assertTrue(replay.replayed)
        self.assertEqual(one.resource_id, replay.resource_id)
        with self.assertRaises(ConflictError):
            self.service.confirm_reservation(
                request_id="idem", actor_id="a1", inquiry_id=inquiry["inquiry_id"],
                equipment_id="bench1", accessory_ids=second["accessory_ids"],
                starts_at=second["starts_at"], ends_at=second["ends_at"])

    # ------------------------------------------------------------ 封锁与队列

    def test_emergency_blockade_displaces_future_and_queues_with_priority(self):
        self._register_bench()
        early = self._inquiry(objective="普通", priority_tier=2,
                              earliest_start="2026-09-26T02:00Z",
                              latest_end="2026-09-26T03:00Z")
        early_c = early["candidates"][0]
        self.service.confirm_reservation(
            request_id="ordinary", actor_id="a1", inquiry_id=early["inquiry_id"],
            equipment_id="bench1", accessory_ids=early_c["accessory_ids"],
            starts_at=early_c["starts_at"], ends_at=early_c["ends_at"])
        urgent_inquiry = self._inquiry(objective="赛前", priority_tier=1,
                                       earliest_start="2026-09-26T04:00Z",
                                       latest_end="2026-09-26T05:00Z")
        urgent_c = urgent_inquiry["candidates"][0]
        self.service.confirm_reservation(
            request_id="urgent", actor_id="a1", inquiry_id=urgent_inquiry["inquiry_id"],
            equipment_id="bench1", accessory_ids=urgent_c["accessory_ids"],
            starts_at=urgent_c["starts_at"], ends_at=urgent_c["ends_at"])
        self.service.publish_blockade(
            request_id="emergency", actor_id="a1", equipment_id="bench1",
            kind="emergency", reason="台架异响",
            starts_at="2026-09-26T01:45Z", ends_at="2026-09-26T23:00Z")
        queue = self.service.list_reschedule_queue("s1")
        self.assertEqual(2, len(queue))
        self.assertEqual(1, queue[0]["priority_tier"])
        self.assertEqual(1, queue[0]["position"])
        self.assertEqual("emergency", queue[0]["cause_kind"])
        displaced = self.service.list_reservations("s1", status="displaced")
        self.assertEqual({q["reservation_id"] for q in queue},
                         {r["reservation_id"] for r in displaced})

    def test_emergency_blockade_records_risk_only_for_started_usage(self):
        self._register_bench()
        inquiry = self._inquiry(earliest_start="2026-09-26T01:00Z",
                                latest_end="2026-09-26T03:00Z",
                                duration_minutes=120)
        candidate = inquiry["candidates"][0]
        self.service.confirm_reservation(
            request_id="running", actor_id="a1", inquiry_id=inquiry["inquiry_id"],
            equipment_id="bench1", accessory_ids=candidate["accessory_ids"],
            starts_at="2026-09-26T01:00Z", ends_at="2026-09-26T03:00Z")
        self.service.publish_blockade(
            request_id="emergency-now", actor_id="a1", equipment_id="bench1",
            kind="emergency", reason="突发故障")
        risks = self.service.list_usage_risks("s1")
        self.assertEqual(1, len(risks))
        self.assertEqual("pending", risks[0]["status"])
        self.assertEqual([], self.service.list_reschedule_queue("s1"))
        self.assertEqual(1, len(self.service.list_reservations("s1", status="confirmed")))
        self.service.resolve_usage_risk(
            request_id="decide", actor_id="a1", risk_id=risks[0]["risk_id"],
            decision="terminated", reason="立即停机")
        risk_replay = self.service.resolve_usage_risk(
            request_id="decide", actor_id="a1", risk_id=risks[0]["risk_id"],
            decision="terminated", reason="立即停机")
        self.assertTrue(risk_replay.replayed)
        self.assertEqual(1, len(self.service.list_reservations("s1", status="terminated")))
        overrides = self.service.list_manual_overrides("s1")
        self.assertTrue(any(o["decision"] == "terminated" for o in overrides))

    def test_rebook_moves_queue_entry_atomically(self):
        self._register_bench()
        inquiry = self._inquiry(earliest_start="2026-09-26T02:00Z",
                                latest_end="2026-09-26T03:00Z")
        candidate = inquiry["candidates"][0]
        self.service.confirm_reservation(
            request_id="doomed", actor_id="a1", inquiry_id=inquiry["inquiry_id"],
            equipment_id="bench1", accessory_ids=candidate["accessory_ids"],
            starts_at=candidate["starts_at"], ends_at=candidate["ends_at"])
        self.service.publish_blockade(
            request_id="planned", actor_id="a1", equipment_id="bench1",
            kind="emergency", reason="故障",
            starts_at="2026-09-26T01:45Z", ends_at="2026-09-26T02:30Z")
        queue = self.service.list_reschedule_queue("s1")
        self.assertEqual(1, len(queue))
        receipt = self.service.rebook_queue_entry(
            request_id="rebook", actor_id="a1", queue_id=queue[0]["queue_id"],
            starts_at="2026-09-26T03:00Z")
        self.assertFalse(receipt.replayed)
        replay = self.service.rebook_queue_entry(
            request_id="rebook", actor_id="a1", queue_id=queue[0]["queue_id"],
            starts_at="2026-09-26T03:00Z")
        self.assertTrue(replay.replayed)
        self.assertEqual(receipt.resource_id, replay.resource_id)
        new_reservations = self.service.list_reservations("s1", status="confirmed")
        self.assertEqual(1, len(new_reservations))
        self.assertEqual("2026-09-26T03:00Z", new_reservations[0]["starts_at"])
        self.assertEqual("rebooked",
                         self.service.list_reschedule_queue("s1", status="rebooked")[0]["status"])

    def test_planned_blockade_is_waivable_and_emergency_is_not(self):
        self._register_bench()
        self.service.publish_blockade(
            request_id="planned", actor_id="a1", equipment_id="bench1", kind="planned",
            reason="例行保养", starts_at="2026-09-27T01:00Z",
            ends_at="2026-09-27T03:00Z")
        inquiry = self._inquiry(earliest_start="2026-09-27T01:00Z",
                                latest_end="2026-09-27T02:30Z")
        flagged = next(c for c in inquiry["candidates"]
                       if c["starts_at"] == "2026-09-27T01:00Z")
        with self.assertRaises(ConflictError):
            self.service.confirm_reservation(
                request_id="no-waiver", actor_id="a1", inquiry_id=inquiry["inquiry_id"],
                equipment_id="bench1", accessory_ids=flagged["accessory_ids"],
                starts_at=flagged["starts_at"], ends_at=flagged["ends_at"])
        self.service.confirm_reservation(
            request_id="with-waiver", actor_id="a1", inquiry_id=inquiry["inquiry_id"],
            equipment_id="bench1", accessory_ids=flagged["accessory_ids"],
            starts_at=flagged["starts_at"], ends_at=flagged["ends_at"],
            waivers=[{"violation": "maintenance_blockade", "reason": "教学特批"}])
        overrides = self.service.list_manual_overrides("s1")
        self.assertTrue(any("maintenance_blockade" in o["decision"] for o in overrides))

    def test_lift_blockade_replays_safely(self):
        self._register_bench()
        published = self.service.publish_blockade(
            request_id="planned", actor_id="a1", equipment_id="bench1", kind="planned",
            reason="例行保养", starts_at="2026-09-27T01:00Z",
            ends_at="2026-09-27T03:00Z")
        first = self.service.lift_blockade(
            request_id="lift", actor_id="a1",
            blockade_id=published.resource_id, reason="保养提前完成")
        replay = self.service.lift_blockade(
            request_id="lift", actor_id="a1",
            blockade_id=published.resource_id, reason="保养提前完成")
        self.assertTrue(replay.replayed)
        self.assertEqual(first.resource_id, replay.resource_id)

    # ------------------------------------------------------------ 权限与校验

    def test_auditor_cannot_book(self):
        self._register_bench()
        with self.assertRaises(PermissionDenied):
            self._inquiry(actor_id="au1")

    def test_unknown_equipment_raises_not_found(self):
        with self.assertRaises(NotFoundError):
            self.service.register_open_window(
                request_id="win-x", actor_id="a1", equipment_id="missing",
                starts_at="2026-09-26T01:00Z", ends_at="2026-09-26T02:00Z")

    def test_naive_datetime_is_rejected(self):
        self._register_bench()
        with self.assertRaises(ValidationError):
            self._inquiry(earliest_start="2026-09-26 01:00")

    def test_resource_occupancy_shows_reservations_and_blockades(self):
        self._register_bench()
        inquiry = self._inquiry()
        first = inquiry["candidates"][0]
        self.service.confirm_reservation(
            request_id="occupy", actor_id="a1", inquiry_id=inquiry["inquiry_id"],
            equipment_id="bench1", accessory_ids=first["accessory_ids"],
            starts_at=first["starts_at"], ends_at=first["ends_at"])
        occupancy = self.service.resource_occupancy(
            "equipment", "bench1", "2026-09-26T00:00Z", "2026-09-27T00:00Z")
        kinds = {item["kind"] for item in occupancy["busy"]}
        self.assertIn("reservation", kinds)


if __name__ == "__main__":
    unittest.main()
