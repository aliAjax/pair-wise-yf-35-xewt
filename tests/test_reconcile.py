import tempfile
import unittest
from pathlib import Path

from src.domain import Actor, ValidationError
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class ReconcileTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "test.db"
        self.repo = SQLiteRepository(self.db)
        self.service = DomainService(self.repo, RuleEngine())
        self.admin = Actor("admin", "admin")
        self.lab = Actor("lab", "lab")

    def tearDown(self):
        self.tmp.cleanup()

    def _athlete(self, name="A. Rider"):
        return self.service.create(
            self.admin, "athlete", {"name": name, "discipline": "cycling"}
        )

    def _sample(self, athlete_id, code, seal_id=None, collected_at="2026-01-01T08:00:00Z"):
        sample = self.service.create(
            self.admin, "sample",
            {"athlete_id": athlete_id, "sample_code": code, "event": "national"},
        )
        self.service.transition(self.admin, sample["id"], "collect", {"collected_at": collected_at})
        if seal_id:
            self.service.transition(self.admin, sample["id"], "seal", {"seal_id": seal_id})
            self.service.transition(self.admin, sample["id"], "ship", {"carrier": "Courier"})
            self.service.transition(self.admin, sample["id"], "receive", {"lab_id": "LAB-1"})
        return self.service.get(sample["id"])

    def _legacy_sample(self, athlete_id, code, collected_at):
        # 旧记录：已到实验室但没有封条号。
        entity_id = "legacy-" + code
        self.repo.create_entity(
            entity_id, "sample", "received",
            {"athlete_id": athlete_id, "sample_code": code, "event": "national",
             "collected_at": collected_at},
            "admin",
        )
        return self.service.get(entity_id)

    def _result(self, seal_id=None, result="adverse", b_sample=False,
                corrected_from=None, athlete_id=None, collected_at=None,
                reported_at="2026-01-05T10:00:00Z"):
        data = {"lab_id": "LAB-1", "result": result, "reported_at": reported_at}
        if seal_id:
            data["seal_id"] = seal_id
        if athlete_id:
            data["athlete_id"] = athlete_id
        if collected_at:
            data["collected_at"] = collected_at
        if b_sample:
            data["b_sample"] = True
        if corrected_from:
            data["corrected_from"] = corrected_from
        return self.service.create(self.lab, "lab_result", data)

    def _case_for(self, sample_id):
        cases = self.service.list("case")
        return next((c for c in cases if c["data"].get("sample_id") == sample_id), None)

    def _notifies(self, entity_id):
        return [a for a in self.service.audit_log(entity_id) if a["action"] == "notify"]

    def test_reconcile_matches_by_seal_and_files_case(self):
        athlete = self._athlete()
        sample = self._sample(athlete["id"], "S-001", seal_id="SEAL-1")
        result = self._result(seal_id="SEAL-1")

        outcome = self.service.reconcile(self.lab)
        self.assertEqual(outcome["processed"], 1)
        matched = self.service.get(result["id"])
        self.assertEqual(matched["status"], "matched")
        self.assertEqual(matched["data"]["sample_id"], sample["id"])

        sample_after = self.service.get(sample["id"])
        self.assertEqual(sample_after["status"], "adverse")
        self.assertEqual(sample_after["data"]["result"], "adverse")

        case = self._case_for(sample["id"])
        self.assertIsNotNone(case)
        self.assertEqual(case["status"], "suspended")
        self.assertEqual(len(self._notifies(case["id"])), 1)

    def test_reconcile_negative_clears_without_case(self):
        athlete = self._athlete()
        sample = self._sample(athlete["id"], "S-002", seal_id="SEAL-2")
        self._result(seal_id="SEAL-2", result="negative")

        self.service.reconcile(self.lab)
        sample_after = self.service.get(sample["id"])
        self.assertEqual(sample_after["status"], "cleared")
        self.assertIsNone(self._case_for(sample["id"]))

    def test_correction_to_negative_lifts_suspension(self):
        athlete = self._athlete()
        sample = self._sample(athlete["id"], "S-003", seal_id="SEAL-3")
        first = self._result(seal_id="SEAL-3", result="adverse")
        self.service.reconcile(self.lab)
        case = self._case_for(sample["id"])
        self.assertEqual(case["status"], "suspended")

        self._result(seal_id="SEAL-3", result="negative", corrected_from=first["id"],
                     reported_at="2026-01-08T10:00:00Z")
        self.service.reconcile(self.lab)

        case_after = self._case_for(sample["id"])
        self.assertEqual(case_after["status"], "closed")
        self.assertEqual(case_after["data"]["decision"], "no_sanction")
        self.assertEqual(self.service.get(sample["id"])["status"], "cleared")

    def test_correction_to_adverse_reconfirms_and_b_confirms(self):
        athlete = self._athlete()
        sample = self._sample(athlete["id"], "S-004", seal_id="SEAL-4")
        first = self._result(seal_id="SEAL-4", result="adverse")
        self.service.reconcile(self.lab)
        case = self._case_for(sample["id"])

        second = self._result(seal_id="SEAL-4", result="adverse", b_sample=True,
                              corrected_from=first["id"], reported_at="2026-01-08T10:00:00Z")
        self.service.reconcile(self.lab)

        case_after = self._case_for(sample["id"])
        self.assertEqual(case_after["status"], "suspended")
        self.assertTrue(self.service.get(sample["id"])["data"]["b_confirmed"])
        reconfirm = [a for a in self.service.audit_log(case["id"])
                     if a["action"] == "reconfirm_suspension"]
        self.assertEqual(len(reconfirm), 1)
        self.assertEqual(len(self._notifies(case["id"])), 2)

        self.service.transition(self.admin, case["id"], "schedule_hearing",
                                {"hearing_at": "2026-02-01"})
        self.service.transition(self.admin, case["id"], "decide", {"decision": "sanction"})
        self.assertEqual(self.service.get(case["id"])["status"], "closed")

    def test_cannot_close_before_b_confirmation(self):
        athlete = self._athlete()
        sample = self._sample(athlete["id"], "S-005", seal_id="SEAL-5")
        self._result(seal_id="SEAL-5", result="adverse")
        self.service.reconcile(self.lab)
        case = self._case_for(sample["id"])
        self.service.transition(self.admin, case["id"], "schedule_hearing",
                                {"hearing_at": "2026-02-01"})
        with self.assertRaises(ValidationError):
            self.service.transition(self.admin, case["id"], "decide", {"decision": "sanction"})

    def test_reconcile_is_resumable_and_idempotent(self):
        athlete = self._athlete()
        sample = self._sample(athlete["id"], "S-006", seal_id="SEAL-6")
        self._result(seal_id="SEAL-6", result="adverse")

        first = self.service.reconcile(self.lab)
        second = self.service.reconcile(self.lab)
        self.assertEqual(first["processed"], 1)
        self.assertEqual(second["processed"], 0)

        case = self._case_for(sample["id"])
        self.assertEqual(len(self.service.list("case")), 1)
        self.assertEqual(len(self._notifies(case["id"])), 1)

    def test_restart_does_not_duplicate_case_or_notify(self):
        athlete = self._athlete()
        sample = self._sample(athlete["id"], "S-007", seal_id="SEAL-7")
        self._result(seal_id="SEAL-7", result="adverse")
        self.service.reconcile(self.lab)

        # 模拟服务重启：新的 service 实例，同一个 SQLite 文件。
        restarted = DomainService(SQLiteRepository(self.db), RuleEngine())
        outcome = restarted.reconcile(self.lab)
        self.assertEqual(outcome["processed"], 0)
        self.assertEqual(len(restarted.list("case")), 1)
        case = self._case_for(sample["id"])
        self.assertEqual(len(self._notifies(case["id"])), 1)

    def test_unmatched_result_goes_manual_then_resolves(self):
        athlete = self._athlete()
        sample = self._sample(athlete["id"], "S-008", seal_id="SEAL-8")
        orphan = self._result(seal_id="UNKNOWN-SEAL", result="adverse")

        outcome = self.service.reconcile(self.lab)
        self.assertEqual(outcome["processed"], 1)
        self.assertEqual(self.service.get(orphan["id"])["status"], "manual")
        manual = self.service.list("lab_result", status="manual")
        self.assertEqual(len(manual), 1)

        self.service.transition(self.admin, orphan["id"], "resolve", {"sample_id": sample["id"]})
        self.assertEqual(self.service.get(orphan["id"])["status"], "matched")
        self.assertIsNotNone(self._case_for(sample["id"]))

    def test_backfill_seal_by_sampling_time_and_athlete(self):
        athlete = self._athlete()
        sample = self._legacy_sample(athlete["id"], "S-009", "2026-03-02T08:00:00Z")
        self._result(seal_id="SEAL-9", result="adverse", athlete_id=athlete["id"],
                     collected_at="2026-03-02T08:00:00Z")

        outcome = self.service.backfill_seals(self.admin)
        self.assertEqual(outcome["backfilled"], 1)
        self.assertEqual(self.service.get(sample["id"])["data"]["seal_id"], "SEAL-9")

    def test_reconcile_falls_back_to_sampling_time(self):
        athlete = self._athlete()
        sample = self._legacy_sample(athlete["id"], "S-010", "2026-04-01T08:00:00Z")
        result = self._result(result="adverse", athlete_id=athlete["id"],
                              collected_at="2026-04-01T08:00:00Z")

        outcome = self.service.reconcile(self.lab)
        self.assertEqual(outcome["processed"], 1)
        matched = self.service.get(result["id"])
        self.assertEqual(matched["status"], "matched")
        self.assertEqual(matched["data"]["matched_by"], "time")
        self.assertEqual(self.service.get(sample["id"])["status"], "adverse")
        # 回填：结果没有封条号，样本也没有，保持无封条号但已关联。
        self.assertEqual(self.service.get(sample["id"])["data"].get("seal_id"), None)

    def test_handover_records_custody_chain(self):
        athlete = self._athlete()
        sample = self._sample(athlete["id"], "S-011", seal_id="SEAL-11")
        handover = self.service.create(
            self.admin, "handover",
            {"sample_id": sample["id"], "seal_id": "SEAL-11", "from_party": "inspector",
             "to_party": "courier", "handover_at": "2026-01-02T09:00:00Z"},
        )
        self.assertEqual(handover["status"], "recorded")
        self.service.transition(self.admin, handover["id"], "confirm", {})
        self.assertEqual(self.service.get(handover["id"])["status"], "confirmed")


if __name__ == "__main__":
    unittest.main()
