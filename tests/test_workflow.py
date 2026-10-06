import tempfile
import unittest
from pathlib import Path

from src.domain import Actor, InvalidTransition
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class ChainTest(unittest.TestCase):
    """样本 -> 封条交接 -> 实验室结果 -> 案件 的完整链路。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.admin = Actor("admin", "admin")
        self.inspector = Actor("DCO-7", "inspector")
        self.lab = Actor("LAB-1-ingest", "lab")
        self.panel = Actor("panel-1", "panel")

    def tearDown(self):
        self.tmp.cleanup()

    def _athlete(self, name="A. Rider", discipline="cycling"):
        return self.service.create(
            self.admin, "athlete", {"name": name, "discipline": discipline}
        )

    def _offline_sample(self, athlete_id, sample_code="S-001", seal_id="SEAL-1",
                        collected_at="2026-03-01T08:00:00+00:00", event="OOC-check"):
        return self.service.record_collection_packet(
            {
                "athlete_id": athlete_id,
                "sample_code": sample_code,
                "event": event,
                "collected_at": collected_at,
                "seal_id": seal_id,
            },
            self.inspector,
        )

    def test_full_chain_a_adverse_then_b_confirms_and_closes(self):
        athlete = self._athlete()
        packet = self._offline_sample(athlete["id"])
        sample = packet["sample"]
        self.assertEqual(sample["status"], "collected")
        handover = packet["seal_handover"]
        self.assertEqual(handover["status"], "recorded")

        # 现场 -> 承运 -> 实验室签收
        self.service.transition(self.inspector, handover["id"], "hand_over",
                                {"carrier": "Courier-A"})
        self.service.transition(self.admin, sample["id"], "ship",
                                {"carrier": "Courier-A"})
        self.service.transition(self.lab, handover["id"], "confirm_receipt",
                                {"lab_id": "LAB-1"})
        self.service.transition(self.admin, sample["id"], "receive",
                                {"lab_id": "LAB-1"})
        self.service.transition(self.admin, sample["id"], "analyze",
                                {"result": "pending"})

        # 实验室先传 A 样阳性结果，网络恢复后对账
        result_a, created = self.service.upload_lab_result(
            {
                "report_no": "R-100",
                "revision": 1,
                "seal_id": "SEAL-1",
                "collected_at": "2026-03-01T08:00:00Z",
                "result": "adverse",
                "aliquot": "A",
                "substance": "EPO",
            },
            self.lab,
        )
        self.assertTrue(created)
        report = self.service.reconcile()
        self.assertEqual(report["processed"], 1)

        sample = self.service.get(sample["id"])
        self.assertEqual(sample["status"], "adverse")
        cases = self.service.list("case")
        self.assertEqual(len(cases), 1)
        case = cases[0]
        self.assertEqual(case["status"], "suspended")

        # B 样确认阳性前不能结案
        self.service.transition(self.panel, case["id"], "schedule_hearing",
                                {"hearing_at": "2026-04-01"})
        with self.assertRaises(InvalidTransition):
            self.service.transition(self.panel, case["id"], "decide",
                                    {"decision": "sanction"})

        # B 样确认阳性后才允许结案（B 样更新会重新确认禁赛，需再排听证）
        self.service.upload_lab_result(
            {
                "report_no": "R-100-B",
                "revision": 1,
                "seal_id": "SEAL-1",
                "collected_at": "2026-03-01T08:00:00Z",
                "result": "adverse",
                "aliquot": "B",
                "substance": "EPO",
            },
            self.lab,
        )
        self.service.reconcile()
        case = self.service.get(case["id"])
        self.assertEqual(case["status"], "suspended")
        self.service.transition(self.panel, case["id"], "schedule_hearing",
                                {"hearing_at": "2026-05-01"})
        decided = self.service.transition(self.panel, case["id"], "decide",
                                          {"decision": "sanction"})
        self.assertEqual(decided["status"], "closed")

    def test_b_sample_cleared_reopens_case_and_lifts_suspension(self):
        athlete = self._athlete()
        packet = self._offline_sample(athlete["id"])
        sample_id = packet["sample"]["id"]

        self.service.upload_lab_result(
            {
                "report_no": "R-200", "revision": 1, "seal_id": "SEAL-1",
                "collected_at": "2026-03-01T08:00:00Z",
                "result": "adverse", "aliquot": "A", "substance": "EPO",
            },
            self.lab,
        )
        self.service.reconcile()
        case = self.service.list("case")[0]
        self.assertEqual(case["status"], "suspended")

        # B 样阴性：解除临时禁赛、案件退回重开，不能直接结案
        self.service.upload_lab_result(
            {
                "report_no": "R-200-B", "revision": 1, "seal_id": "SEAL-1",
                "collected_at": "2026-03-01T08:00:00Z",
                "result": "cleared", "aliquot": "B",
            },
            self.lab,
        )
        self.service.reconcile()
        case = self.service.get(case["id"])
        self.assertEqual(case["status"], "reopened")
        self.assertEqual(case["data"]["lift_result"], "cleared")
        with self.assertRaises(InvalidTransition):
            self.service.transition(self.panel, case["id"], "decide",
                                    {"decision": "sanction"})

        notifications = self.service.list("notification")
        channels = {n["data"]["channel"] for n in notifications}
        self.assertIn("provisional_suspension", channels)
        self.assertIn("suspension_lifted", channels)


if __name__ == "__main__":
    unittest.main()
