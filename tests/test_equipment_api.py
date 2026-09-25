import unittest
from datetime import datetime, timezone

from skills_workspace.api import route
from skills_workspace.clock import FixedClock
from skills_workspace.equipment import EquipmentService
from skills_workspace.storage import Database


class EquipmentApiTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.service = EquipmentService(self.database, FixedClock(datetime(2026, 9, 25, 8, 0, tzinfo=timezone.utc)))
        self.service.register_organization(request_id="org", actor_id="bootstrap", organization_id="o1", name="院校")
        self.service.register_actor(request_id="admin", actor_id="bootstrap", new_actor_id="a1",
                                    display_name="管理员", role="admin", organization_id="o1")
        self.service.register_actor(request_id="operator", actor_id="a1", new_actor_id="op1",
                                    display_name="申请人", role="operator", organization_id="o1")
        self.service.register_site(request_id="site", actor_id="a1", site_id="s1",
                                   organization_id="o1", name="实训基地", timezone_name="Asia/Shanghai")

    def tearDown(self):
        self.database.close()

    def _register_equipment(self):
        body = {"request_id": "eq1", "equipment_id": "eq1", "site_id": "s1", "name": "轨道车辆台架",
                "capability_version": "CV-1", "capabilities": ["traction_debug"]}
        return route(self.service, "POST", "/equipment", body, {"X-Actor-Id": "a1"})

    def test_equipment_registration_route_replays(self):
        status, payload = self._register_equipment()
        self.assertEqual(201, status)
        self.assertFalse(payload["replayed"])
        status, payload = self._register_equipment()
        self.assertEqual(200, status)
        self.assertTrue(payload["replayed"])

    def test_slot_search_and_reservation_routes(self):
        self._register_equipment()
        route(self.service, "POST", "/calibration-certificates",
              {"request_id": "cert1", "certificate_id": "cert-eq1", "resource_type": "equipment",
               "resource_id": "eq1", "issuer": "计量院", "issued_at": "2026-08-01T00:00:00Z",
               "expires_at": "2026-10-01T00:00:00Z"}, {"X-Actor-Id": "a1"})
        route(self.service, "POST", "/open-windows",
              {"request_id": "win1", "window_id": "win-eq1", "resource_type": "equipment",
               "resource_id": "eq1", "start_at": "2026-09-26T08:00:00Z",
               "end_at": "2026-09-26T18:00:00Z"}, {"X-Actor-Id": "a1"})
        status, search = route(self.service, "POST", "/slot-searches",
                               {"site_id": "s1", "goal": "g1", "required_capabilities": ["traction_debug"],
                                "duration_minutes": 120, "search_start": "2026-09-26T08:00:00Z",
                                "search_end": "2026-09-26T18:00:00Z"})
        self.assertEqual(200, status)
        self.assertEqual(1, len(search["candidates"]))
        candidate = search["candidates"][0]
        status, receipt = route(self.service, "POST", "/reservations",
                                {"request_id": "r1", "site_id": "s1", "goal": "g1", "equipment_id": "eq1",
                                 "expected_equipment_version": candidate["equipment_version"],
                                 "attachments": [], "start_at": candidate["start_at"],
                                 "end_at": candidate["end_at"]}, {"X-Actor-Id": "op1"})
        self.assertEqual(201, status)
        status, detail = route(self.service, "GET", f"/reservations/{receipt['resource_id']}", None)
        self.assertEqual(200, status)
        self.assertEqual("confirmed", detail["status"])
        self.assertEqual(["cert-eq1"], detail["calibration"]["equipment"]["certificate_ids"])
        status, payload = route(self.service, "GET", "/reservations/missing", None)
        self.assertEqual(404, status)

    def test_occupancy_queue_and_overrides_routes(self):
        self._register_equipment()
        status, occupancy = route(self.service, "GET",
                                  "/occupancy?site_id=s1&start_at=2026-09-26T00:00:00Z"
                                  "&end_at=2026-09-27T00:00:00Z", None)
        self.assertEqual(200, status)
        self.assertEqual(1, len(occupancy["equipment"]))
        status, queue = route(self.service, "GET", "/reschedule-queue?site_id=s1", None)
        self.assertEqual(200, status)
        self.assertEqual([], queue["items"])
        status, overrides = route(self.service, "GET", "/manual-overrides?site_id=s1", None)
        self.assertEqual(200, status)
        self.assertEqual([], overrides["items"])
        status, payload = route(self.service, "GET", "/occupancy", None)
        self.assertEqual(400, status)


if __name__ == "__main__":
    unittest.main()
