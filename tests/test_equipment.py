import unittest
from datetime import datetime, timezone

from skills_workspace.clock import FixedClock
from skills_workspace.equipment import EquipmentService
from skills_workspace.errors import ConflictError, PermissionDenied
from skills_workspace.storage import Database


class EquipmentServiceTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.service = EquipmentService(self.database, FixedClock(datetime(2026, 9, 25, 8, 0, tzinfo=timezone.utc)))
        self.service.register_organization(request_id="org", actor_id="bootstrap", organization_id="o1", name="院校")
        self.service.register_actor(request_id="admin", actor_id="bootstrap", new_actor_id="a1",
                                    display_name="管理员", role="admin", organization_id="o1")
        self.service.register_actor(request_id="operator", actor_id="a1", new_actor_id="op1",
                                    display_name="申请人", role="operator", organization_id="o1")
        self.service.register_actor(request_id="maintainer", actor_id="a1", new_actor_id="m1",
                                    display_name="维护员", role="maintainer", organization_id="o1")
        self.service.register_actor(request_id="auditor", actor_id="a1", new_actor_id="au1",
                                    display_name="审计员", role="auditor", organization_id="o1")
        self.service.register_site(request_id="site", actor_id="a1", site_id="s1",
                                   organization_id="o1", name="实训基地", timezone_name="Asia/Shanghai")
        self._equipment("eq1", ["brake_test", "traction_debug"])
        self._equipment("eq2", ["traction_debug"], expires="2026-09-01T00:00:00Z")
        self._equipment("eq3", ["crown_milling"])
        self._attachment("at1", ["eq1"])
        self._attachment("at2", ["*"])
        self.service.register_changeover_rule(request_id="rule1", actor_id="a1", rule_id="rule-eq1",
                                              equipment_id="eq1", from_goal="*", to_goal="*", minutes=30)

    def tearDown(self):
        self.database.close()

    def _equipment(self, equipment_id, capabilities, expires="2026-10-01T00:00:00Z"):
        self.service.register_equipment(request_id=f"reg-{equipment_id}", actor_id="a1",
                                        equipment_id=equipment_id, site_id="s1", name=f"设备{equipment_id}",
                                        capability_version="CV-1", capabilities=capabilities)
        self.service.register_calibration(request_id=f"cal-{equipment_id}", actor_id="a1",
                                          certificate_id=f"cert-{equipment_id}", resource_type="equipment",
                                          resource_id=equipment_id, issuer="计量院",
                                          issued_at="2026-08-01T00:00:00Z", expires_at=expires)
        self.service.register_open_window(request_id=f"win-{equipment_id}", actor_id="a1",
                                          window_id=f"win-{equipment_id}", resource_type="equipment",
                                          resource_id=equipment_id, start_at="2026-09-26T08:00:00Z",
                                          end_at="2026-09-26T18:00:00Z")

    def _attachment(self, attachment_id, compatible):
        self.service.register_attachment(request_id=f"reg-{attachment_id}", actor_id="a1",
                                         attachment_id=attachment_id, site_id="s1", name=f"附件{attachment_id}",
                                         capability_version="AV-1", compatible_equipment=compatible)
        self.service.register_calibration(request_id=f"cal-{attachment_id}", actor_id="a1",
                                          certificate_id=f"cert-{attachment_id}", resource_type="attachment",
                                          resource_id=attachment_id, issuer="计量院",
                                          issued_at="2026-08-01T00:00:00Z", expires_at="2026-10-01T00:00:00Z")
        self.service.register_open_window(request_id=f"win-{attachment_id}", actor_id="a1",
                                          window_id=f"win-{attachment_id}", resource_type="attachment",
                                          resource_id=attachment_id, start_at="2026-09-26T08:00:00Z",
                                          end_at="2026-09-26T18:00:00Z")

    def _search(self, **overrides):
        params = dict(site_id="s1", goal="g1", required_capabilities=["traction_debug"],
                      attachment_ids=["at2"], duration_minutes=120,
                      search_start="2026-09-26T08:00:00Z", search_end="2026-09-26T18:00:00Z")
        params.update(overrides)
        return self.service.search_slots(**params)

    def _confirm(self, request_id, start, end, equipment_version=1, attachments=None, **extra):
        if attachments is None:
            attachments = [{"attachment_id": "at2", "expected_version": 1}]
        return self.service.confirm_reservation(
            request_id=request_id, actor_id="op1", site_id="s1", goal="g1",
            equipment_id="eq1", expected_equipment_version=equipment_version,
            attachments=attachments, start_at=start, end_at=end, **extra)

    def _count(self, table):
        return self.database.connection.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()["c"]

    def test_register_equipment_replays_and_conflicts(self):
        first = self.service.register_equipment(request_id="eq-new", actor_id="a1", equipment_id="eq9",
                                                site_id="s1", name="新设备", capability_version="CV-1",
                                                capabilities=["traction_debug"])
        replay = self.service.register_equipment(request_id="eq-new", actor_id="a1", equipment_id="eq9",
                                                 site_id="s1", name="新设备", capability_version="CV-1",
                                                 capabilities=["traction_debug"])
        self.assertFalse(first.replayed)
        self.assertTrue(replay.replayed)
        with self.assertRaises(ConflictError):
            self.service.register_equipment(request_id="eq-new", actor_id="a1", equipment_id="eq9",
                                            site_id="s1", name="改名设备", capability_version="CV-1",
                                            capabilities=["traction_debug"])

    def test_search_offers_candidate_with_calibration_basis_and_reasons(self):
        result = self._search()
        self.assertEqual(1, len(result["candidates"]))
        candidate = result["candidates"][0]
        self.assertEqual("eq1", candidate["equipment_id"])
        self.assertEqual(1, candidate["equipment_version"])
        self.assertEqual("2026-09-26T08:00:00Z", candidate["start_at"])
        self.assertEqual("2026-09-26T10:00:00Z", candidate["end_at"])
        self.assertEqual(["cert-eq1"], candidate["calibration"]["equipment"]["certificate_ids"])
        self.assertEqual(["cert-at2"], candidate["calibration"]["attachments"][0]["certificate_ids"])
        reasons = {item["equipment_id"]: item["reason"] for item in result["unavailable"]}
        self.assertEqual("calibration_not_covering", reasons["eq2"])
        self.assertEqual("capability_mismatch", reasons["eq3"])

    def test_confirm_is_atomic_when_attachment_version_stale(self):
        events_before = len(self.service.audit_events())
        with self.assertRaises(ConflictError):
            self._confirm("r-x", "2026-09-26T08:00:00Z", "2026-09-26T10:00:00Z",
                          attachments=[{"attachment_id": "at1", "expected_version": 1},
                                       {"attachment_id": "at2", "expected_version": 99}])
        self.assertEqual(0, self._count("reservations"))
        self.assertEqual(0, self._count("reservation_attachments"))
        self.assertEqual(events_before, len(self.service.audit_events()))

    def test_confirm_replays_and_rejects_changed_payload(self):
        first = self._confirm("r1", "2026-09-26T08:00:00Z", "2026-09-26T10:00:00Z")
        replay = self._confirm("r1", "2026-09-26T08:00:00Z", "2026-09-26T10:00:00Z")
        self.assertFalse(first.replayed)
        self.assertTrue(replay.replayed)
        self.assertEqual(first.resource_id, replay.resource_id)
        with self.assertRaises(ConflictError):
            self._confirm("r1", "2026-09-26T09:00:00Z", "2026-09-26T11:00:00Z")
        detail = self.service.get_reservation(first.resource_id)
        self.assertEqual("confirmed", detail["status"])
        self.assertEqual(1, detail["equipment_version"])
        self.assertEqual(["cert-eq1"], detail["calibration"]["equipment"]["certificate_ids"])
        self.assertEqual([{"attachment_id": "at2", "attachment_version": 1}], detail["attachments"])

    def test_changeover_gap_enforced_on_confirm_and_search(self):
        self._confirm("r1", "2026-09-26T08:00:00Z", "2026-09-26T10:00:00Z")
        with self.assertRaises(ConflictError):
            self._confirm("r2", "2026-09-26T10:00:00Z", "2026-09-26T12:00:00Z")
        self._confirm("r3", "2026-09-26T10:30:00Z", "2026-09-26T12:00:00Z")
        result = self._search(duration_minutes=60)
        starts = [item["start_at"] for item in result["candidates"]]
        self.assertEqual(["2026-09-26T12:30:00Z"], starts)

    def test_planned_block_rejects_overlap_and_shapes_search(self):
        first = self._confirm("r1", "2026-09-26T08:00:00Z", "2026-09-26T10:00:00Z")
        with self.assertRaises(ConflictError) as context:
            self.service.publish_maintenance_block(request_id="b-bad", actor_id="m1",
                                                   resource_type="equipment", resource_id="eq1",
                                                   start_at="2026-09-26T09:00:00Z",
                                                   end_at="2026-09-26T11:00:00Z", reason="保养")
        self.assertIn(first.resource_id, str(context.exception))
        self.service.publish_maintenance_block(request_id="b1", actor_id="m1",
                                               resource_type="equipment", resource_id="eq1",
                                               start_at="2026-09-26T12:00:00Z",
                                               end_at="2026-09-26T13:00:00Z", reason="保养")
        result = self._search(duration_minutes=60)
        starts = [item["start_at"] for item in result["candidates"]]
        self.assertEqual(["2026-09-26T10:30:00Z", "2026-09-26T13:00:00Z"], starts)

    def _deactivate_eq1_mid_session(self):
        first = self._confirm("r1", "2026-09-26T08:00:00Z", "2026-09-26T10:00:00Z")
        second = self._confirm("r2", "2026-09-26T11:30:00Z", "2026-09-26T13:00:00Z")
        self.service.clock = FixedClock(datetime(2026, 9, 26, 8, 30, tzinfo=timezone.utc))
        self.service.emergency_deactivate(request_id="stop1", actor_id="m1",
                                          resource_type="equipment", resource_id="eq1",
                                          end_at="2026-09-26T12:00:00Z", reason="台架异响")
        return first, second

    def test_emergency_deactivation_displaces_future_and_flags_started(self):
        first, second = self._deactivate_eq1_mid_session()
        equipment = {item["equipment_id"]: item for item in self.service.list_equipment("s1")["equipment"]}
        self.assertEqual("deactivated", equipment["eq1"]["status"])
        self.assertEqual(2, equipment["eq1"]["version"])
        queue = self.service.list_reschedule_queue("s1")
        self.assertEqual(1, len(queue))
        self.assertEqual(second.resource_id, queue[0]["reservation_id"])
        self.assertEqual(1, queue[0]["rank"])
        self.assertEqual("台架异响", queue[0]["block_reason"])
        risks = self.service.list_usage_risks("s1", status="awaiting_decision")
        self.assertEqual(1, len(risks))
        self.assertEqual(first.resource_id, risks[0]["reservation_id"])
        self.assertEqual("confirmed", self.service.get_reservation(first.resource_id)["status"])
        self.assertEqual("reschedule_pending", self.service.get_reservation(second.resource_id)["status"])
        with self.assertRaises(ConflictError):
            self._confirm("r3", "2026-09-26T14:00:00Z", "2026-09-26T16:00:00Z", equipment_version=2)
        with self.assertRaises(ConflictError):
            self.service.emergency_deactivate(request_id="stop2", actor_id="m1",
                                              resource_type="equipment", resource_id="eq1",
                                              end_at="2026-09-26T13:00:00Z", reason="重复停用")

    def test_risk_decision_continue_records_manual_override(self):
        first, _ = self._deactivate_eq1_mid_session()
        risk = self.service.list_usage_risks("s1", status="awaiting_decision")[0]
        self.service.decide_usage_risk(request_id="d1", actor_id="m1", risk_id=risk["risk_id"],
                                       decision="continue", note="现场检查无异常")
        self.assertEqual("confirmed", self.service.get_reservation(first.resource_id)["status"])
        risks = self.service.list_usage_risks("s1")
        self.assertEqual("resolved_continue", risks[0]["status"])
        overrides = self.service.list_manual_overrides("s1")
        self.assertEqual(1, len(overrides))
        self.assertEqual("usage_risk", overrides[0]["subject_type"])
        self.assertEqual("continue", overrides[0]["decision"])
        self.assertEqual("m1", overrides[0]["actor_id"])
        self.assertEqual("现场检查无异常", overrides[0]["note"])
        with self.assertRaises(ConflictError):
            self.service.decide_usage_risk(request_id="d2", actor_id="m1", risk_id=risk["risk_id"],
                                           decision="terminate", note="重复决定")

    def test_risk_decision_terminate_ends_started_reservation(self):
        first = self._confirm("r1", "2026-09-26T08:00:00Z", "2026-09-26T10:00:00Z")
        self.service.clock = FixedClock(datetime(2026, 9, 26, 8, 30, tzinfo=timezone.utc))
        self.service.emergency_deactivate(request_id="stop-a", actor_id="m1",
                                          resource_type="attachment", resource_id="at2",
                                          end_at="2026-09-26T09:30:00Z", reason="附件校验失败")
        risk = self.service.list_usage_risks("s1", status="awaiting_decision")[0]
        self.assertEqual(first.resource_id, risk["reservation_id"])
        self.service.decide_usage_risk(request_id="d1", actor_id="m1", risk_id=risk["risk_id"],
                                       decision="terminate", note="存在安全隐患，立即结束")
        self.assertEqual("terminated", self.service.get_reservation(first.resource_id)["status"])
        overrides = self.service.list_manual_overrides("s1")
        self.assertEqual("terminate", overrides[0]["decision"])

    def test_attachment_deactivation_displaces_future_reservation(self):
        first = self._confirm("r1", "2026-09-26T08:00:00Z", "2026-09-26T10:00:00Z")
        self.service.emergency_deactivate(request_id="stop-a", actor_id="m1",
                                          resource_type="attachment", resource_id="at2",
                                          end_at="2026-09-26T12:00:00Z", reason="附件召回")
        queue = self.service.list_reschedule_queue("s1")
        self.assertEqual(1, len(queue))
        self.assertEqual(first.resource_id, queue[0]["reservation_id"])
        self.assertEqual("reschedule_pending", self.service.get_reservation(first.resource_id)["status"])

    def test_rebook_through_queue_resolves_entry(self):
        _, second = self._deactivate_eq1_mid_session()
        entry = self.service.list_reschedule_queue("s1")[0]
        self.service.reactivate_resource(request_id="re1", actor_id="m1",
                                         resource_type="equipment", resource_id="eq1")
        result = self._search(search_start="2026-09-26T12:00:00Z", search_end="2026-09-26T18:00:00Z")
        candidate = result["candidates"][0]
        self.assertEqual("2026-09-26T12:00:00Z", candidate["start_at"])
        self.assertEqual(3, candidate["equipment_version"])
        self.service.confirm_reservation(
            request_id="r3", actor_id="op1", site_id="s1", goal="g1", equipment_id="eq1",
            expected_equipment_version=candidate["equipment_version"],
            attachments=[{"attachment_id": "at2", "expected_version": 1}],
            start_at=candidate["start_at"], end_at=candidate["end_at"],
            reschedule_entry_id=entry["entry_id"])
        self.assertEqual([], self.service.list_reschedule_queue("s1"))
        self.assertEqual("rescheduled", self.service.get_reservation(second.resource_id)["status"])

    def test_close_entry_cancels_reservation_and_records_override(self):
        second = self._confirm("r2", "2026-09-26T11:30:00Z", "2026-09-26T13:00:00Z")
        self.service.emergency_deactivate(request_id="stop1", actor_id="m1",
                                          resource_type="equipment", resource_id="eq1",
                                          end_at="2026-09-26T12:00:00Z", reason="计划外检修")
        entry = self.service.list_reschedule_queue("s1")[0]
        self.service.close_reschedule_entry(request_id="c1", actor_id="m1",
                                            entry_id=entry["entry_id"], note="训练取消，不再改期")
        self.assertEqual([], self.service.list_reschedule_queue("s1"))
        self.assertEqual("cancelled", self.service.get_reservation(second.resource_id)["status"])
        overrides = self.service.list_manual_overrides("s1")
        self.assertEqual(1, len(overrides))
        self.assertEqual("reschedule_entry", overrides[0]["subject_type"])
        self.assertEqual("drop", overrides[0]["decision"])
        with self.assertRaises(ConflictError):
            self.service.close_reschedule_entry(request_id="c2", actor_id="m1",
                                                entry_id=entry["entry_id"], note="重复处理")

    def test_permissions_for_maintenance_and_registry(self):
        with self.assertRaises(PermissionDenied):
            self.service.publish_maintenance_block(request_id="pb", actor_id="op1",
                                                   resource_type="equipment", resource_id="eq1",
                                                   start_at="2026-09-26T12:00:00Z",
                                                   end_at="2026-09-26T13:00:00Z", reason="保养")
        with self.assertRaises(PermissionDenied):
            self.service.register_equipment(request_id="eq-x", actor_id="m1", equipment_id="eq-x",
                                            site_id="s1", name="越权设备", capability_version="CV-1",
                                            capabilities=["traction_debug"])
        with self.assertRaises(PermissionDenied):
            self.service.confirm_reservation(request_id="rc", actor_id="au1", site_id="s1", goal="g1",
                                             equipment_id="eq1", expected_equipment_version=1,
                                             attachments=[], start_at="2026-09-26T08:00:00Z",
                                             end_at="2026-09-26T10:00:00Z")
        receipt = self.service.emergency_deactivate(request_id="ok", actor_id="m1",
                                                    resource_type="equipment", resource_id="eq1",
                                                    end_at="2026-09-26T09:00:00Z", reason="检查")
        self.assertFalse(receipt.replayed)

    def test_calibration_boundary_limits_candidates(self):
        self._equipment("eq4", ["traction_debug"], expires="2026-09-26T12:00:00Z")
        result = self._search()
        eq4_candidates = [item for item in result["candidates"] if item["equipment_id"] == "eq4"]
        self.assertEqual(1, len(eq4_candidates))
        self.assertEqual("2026-09-26T10:00:00Z", eq4_candidates[0]["end_at"])
        with self.assertRaises(ConflictError):
            self.service.confirm_reservation(
                request_id="r-late", actor_id="op1", site_id="s1", goal="g1", equipment_id="eq4",
                expected_equipment_version=1,
                attachments=[{"attachment_id": "at2", "expected_version": 1}],
                start_at="2026-09-26T10:30:00Z", end_at="2026-09-26T12:30:00Z")

    def test_capability_change_bumps_version_and_invalidates_confirm(self):
        self.service.change_equipment_capability(request_id="cc1", actor_id="a1", equipment_id="eq1",
                                                 capability_version="CV-2", capabilities=["traction_debug"])
        equipment = {item["equipment_id"]: item for item in self.service.list_equipment("s1")["equipment"]}
        self.assertEqual(2, equipment["eq1"]["version"])
        with self.assertRaises(ConflictError):
            self._confirm("r1", "2026-09-26T08:00:00Z", "2026-09-26T10:00:00Z", equipment_version=1)
        self._confirm("r2", "2026-09-26T08:00:00Z", "2026-09-26T10:00:00Z", equipment_version=2)
        self.service.change_equipment_capability(request_id="cc2", actor_id="a1", equipment_id="eq1",
                                                 capability_version="CV-2", capabilities=["traction_debug"])
        equipment = {item["equipment_id"]: item for item in self.service.list_equipment("s1")["equipment"]}
        self.assertEqual(2, equipment["eq1"]["version"])

    def test_occupancy_view_shows_reservations_and_blocks(self):
        first = self._confirm("r1", "2026-09-26T08:00:00Z", "2026-09-26T10:00:00Z")
        self.service.publish_maintenance_block(request_id="b1", actor_id="m1",
                                               resource_type="equipment", resource_id="eq1",
                                               start_at="2026-09-26T12:00:00Z",
                                               end_at="2026-09-26T13:00:00Z", reason="保养")
        view = self.service.resource_occupancy("s1", "2026-09-26T00:00:00Z", "2026-09-27T00:00:00Z")
        equipment = {item["equipment_id"]: item for item in view["equipment"]}
        self.assertEqual([first.resource_id],
                         [item["reservation_id"] for item in equipment["eq1"]["reservations"]])
        self.assertEqual(["at2"], equipment["eq1"]["reservations"][0]["attachments"])
        self.assertEqual("planned", equipment["eq1"]["blocks"][0]["kind"])
        attachments = {item["attachment_id"]: item for item in view["attachments"]}
        self.assertEqual([first.resource_id],
                         [item["reservation_id"] for item in attachments["at2"]["reservations"]])

    def test_audit_chain_remains_valid_after_mixed_operations(self):
        self._deactivate_eq1_mid_session()
        risk = self.service.list_usage_risks("s1", status="awaiting_decision")[0]
        self.service.decide_usage_risk(request_id="d1", actor_id="m1", risk_id=risk["risk_id"],
                                       decision="continue", note="检查无异常")
        valid, count = self.service.verify_audit()
        self.assertTrue(valid)
        self.assertGreater(count, 0)


if __name__ == "__main__":
    unittest.main()
