import unittest

from skills_workspace.equipment_acceptance import run


class EquipmentAcceptanceTest(unittest.TestCase):
    def test_offline_acceptance(self):
        result = run()
        self.assertEqual("ok", result["status"])
        self.assertTrue(result["audit_valid"])
        self.assertGreaterEqual(result["candidates"], 1)
        self.assertEqual(2, result["unavailable"])
        self.assertFalse(result["first_replayed"])
        self.assertTrue(result["second_replayed"])
        self.assertTrue(result["conflict_rejected"])
        self.assertEqual(1, result["displaced"])
        self.assertEqual(1, result["risks_flagged"])
        self.assertEqual(0, result["queue_pending_after_rebook"])
        self.assertEqual(1, result["manual_overrides"])


if __name__ == "__main__":
    unittest.main()
