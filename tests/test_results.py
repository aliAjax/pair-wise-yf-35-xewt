import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src.domain import Actor, ConflictError
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService, SYSTEM_ACTOR


class ResultManagementTest(unittest.TestCase):
    """结果更正、对账续办、回填与人工判读。"""

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

    def _athlete(self, name="A. Rider"):
        return self.service.create(
            self.admin, "athlete", {"name": name, "discipline": "cycling"}
        )

    def _offline_sample(self, athlete_id, sample_code="S-001", seal_id="SEAL-1",
                        collected_at="2026-03-01T08:00:00+00:00"):
        return self.service.record_collection_packet(
            {
                "athlete_id": athlete_id,
                "sample_code": sample_code,
                "event": "OOC-check",
                "collected_at": collected_at,
                "seal_id": seal_id,
            },
            self.inspector,
        )

    def test_result_correction_reconfirms_suspension_without_new_case(self):
        athlete = self._athlete()
        sample = self._offline_sample(athlete["id"])["sample"]

        # 首报：A 样阳性 -> 立案 + 临时禁赛
        self.service.upload_lab_result(
            {
                "report_no": "R-300", "revision": 1, "seal_id": "SEAL-1",
                "collected_at": "2026-03-01T08:00:00Z",
                "result": "adverse", "aliquot": "A", "substance": "EPO",
            },
            self.lab,
        )
        self.service.reconcile()
        case = self.service.list("case")[0]
        self.assertNotIn("reconfirm_result", case["data"])

        # 几天后实验室复检更正（仍阳性，换了物质认定）：不能重复立案
        self.service.upload_lab_result(
            {
                "report_no": "R-300", "revision": 2, "seal_id": "SEAL-1",
                "collected_at": "2026-03-01T08:00:00Z",
                "result": "adverse", "aliquot": "A", "substance": "CERA",
                "remark": "retest corrected finding",
            },
            self.lab,
        )
        self.service.reconcile()
        cases = self.service.list("case")
        self.assertEqual(len(cases), 1)
        case = self.service.get(case["id"])
        self.assertEqual(case["status"], "suspended")
        self.assertEqual(case["data"]["reconfirm_revision"], 2)
        self.assertEqual(case["data"]["reconfirm_result"], "adverse")

        # 旧修订被标记 superseded
        revisions = {
            item["data"]["revision"]: item["status"]
            for item in self.service.list("lab_result")
        }
        self.assertEqual(revisions, {1: "superseded", 2: "matched"})

    def test_reconcile_resumes_only_unfinished_results(self):
        athlete = self._athlete()
        self._offline_sample(athlete["id"], sample_code="S-001", seal_id="SEAL-1")
        self._offline_sample(
            athlete["id"], sample_code="S-002", seal_id="SEAL-2",
            collected_at="2026-03-02T08:00:00+00:00",
        )
        for sample_code, seal_id, report_no, collected_day in (
            ("S-001", "SEAL-1", "R-401", "1"),
            ("S-002", "SEAL-2", "R-402", "2"),
        ):
            self.service.upload_lab_result(
                {
                    "report_no": report_no, "revision": 1, "seal_id": seal_id,
                    "collected_at": "2026-03-0%sT08:00:00Z" % collected_day,
                    "result": "adverse", "aliquot": "A",
                },
                self.lab,
            )

        # 只对第一条结果对账（模拟中断在两条之间）
        first_result = [
            item["id"] for item in self.service.list("lab_result")
            if item["data"]["report_no"] == "R-401"
        ]
        first_run = self.service.reconcile(result_ids=first_result)
        self.assertEqual(first_run["processed"], 1)
        self.assertEqual(len(self.service.list("case")), 1)

        # 续办：只处理还没对完的第二条
        second_run = self.service.reconcile()
        self.assertEqual(second_run["processed"], 1)
        self.assertEqual(len(self.service.list("case")), 2)

        # 再跑一遍（模拟重启）：没有未处理结果，不重复立案
        third_run = self.service.reconcile()
        self.assertEqual(third_run["processed"], 0)
        self.assertEqual(len(self.service.list("case")), 2)

    def test_restart_does_not_duplicate_notifications(self):
        athlete = self._athlete()
        sample = self._offline_sample(athlete["id"])["sample"]
        self.service.upload_lab_result(
            {
                "report_no": "R-500", "revision": 1, "seal_id": "SEAL-1",
                "collected_at": "2026-03-01T08:00:00Z",
                "result": "adverse", "aliquot": "A",
            },
            self.lab,
        )

        # 模拟服务在对账传播中途崩溃：案件已自动立案，但结果仍停在 recorded。
        # 用确定性 ID 直接造出"半成品"现场，再重跑对账。
        self.repo.create_entity(
            "case-" + sample["id"], "case", "open",
            {
                "athlete_id": athlete["id"],
                "sample_id": sample["id"],
                "alleged_rule": "adverse-finding",
                "origin_result_id": "result-R-500|1",
            },
            "system",
        )
        self.service.reconcile()
        self.service.reconcile()  # 再来一次也一样

        cases = self.service.list("case")
        self.assertEqual(len(cases), 1)
        suspend_notes = [
            n for n in self.service.list("notification")
            if n["data"]["channel"] == "provisional_suspension"
        ]
        self.assertEqual(len(suspend_notes), 1)

    def test_offline_packet_replay_is_idempotent(self):
        athlete = self._athlete()
        first = self.service.record_collection_packet(
            {
                "athlete_id": athlete["id"], "sample_code": "S-600",
                "event": "OOC-check",
                "collected_at": "2026-03-01T08:00:00Z", "seal_id": "SEAL-600",
            },
            self.inspector,
        )
        second = self.service.record_collection_packet(
            {
                "athlete_id": athlete["id"], "sample_code": "S-600",
                "event": "OOC-check",
                "collected_at": "2026-03-01T08:00:00Z", "seal_id": "SEAL-600",
            },
            self.inspector,
        )
        self.assertEqual(first["sample"]["id"], second["sample"]["id"])
        self.assertTrue(second["replayed"])
        self.assertEqual(len(self.service.list("sample")), 1)
        self.assertEqual(len(self.service.list("seal_handover")), 1)

    def test_unmatched_result_goes_to_manual_review_then_resolves(self):
        athlete = self._athlete()
        sample = self._offline_sample(athlete["id"])["sample"]

        # 封条号拼错：按封条号+采样时刻判不出，进入人工队列
        self.service.upload_lab_result(
            {
                "report_no": "R-700", "revision": 1, "seal_id": "SEAL-TYPO",
                "collected_at": "2026-03-01T08:00:00Z",
                "result": "adverse", "aliquot": "A",
            },
            self.lab,
        )
        report = self.service.reconcile()
        self.assertEqual(report["items"][0]["status"], "manual_review")
        review = self.service.list("manual_review")[0]
        self.assertEqual(review["status"], "pending")
        self.assertEqual(len(self.service.list("case")), 0)

        # 人工判读确认归属，链路继续推进
        resolved = self.service.resolve_manual_review(
            review["id"], "matched", self.panel, sample_id=sample["id"]
        )
        self.assertEqual(resolved["outcome"]["case_action"], "suspended")
        self.assertEqual(len(self.service.list("case")), 1)

    def test_backfill_seal_by_time_and_athlete_ambiguous_goes_manual(self):
        athlete = self._athlete()
        collected_at = "2026-03-01T08:00:00+00:00"

        # 旧样本：没有封条号（绕过常规创建，模拟历史数据）
        self.repo.create_entity(
            "sample-OLD-1", "sample", "analyzed",
            {
                "athlete_id": athlete["id"], "sample_code": "OLD-1",
                "event": "OOC-2025", "collected_at": collected_at,
            },
            "legacy-import",
        )
        # 同一运动员、同一采样时刻的封条交接记录
        self.service.create(
            self.admin,
            "seal_handover",
            {
                "id": "seal-OLD-SEAL", "seal_id": "OLD-SEAL",
                "athlete_id": athlete["id"], "collected_at": collected_at,
            },
        )

        report = self.service.backfill_seals()
        self.assertEqual(report["items"][0]["status"], "backfilled")
        sample = self.service.get("sample-OLD-1")
        self.assertEqual(sample["data"]["seal_id"], "OLD-SEAL")
        self.assertTrue(sample["data"]["seal_backfilled"])
        self.assertEqual(sample["status"], "analyzed")  # 回填不改变状态

        # 之后实验室结果即使带了封条号也能对上
        self.service.upload_lab_result(
            {
                "report_no": "R-800", "revision": 1, "seal_id": "OLD-SEAL",
                "collected_at": "2026-03-01T08:00:00Z",
                "result": "cleared", "aliquot": "A",
            },
            self.lab,
        )
        self.assertEqual(self.service.reconcile()["items"][0]["status"], "matched")

    def test_backfill_without_candidate_goes_manual(self):
        athlete = self._athlete()
        self.repo.create_entity(
            "sample-OLD-2", "sample", "analyzed",
            {
                "athlete_id": athlete["id"], "sample_code": "OLD-2",
                "event": "OOC-2025",
                "collected_at": "2025-12-01T08:00:00+00:00",
            },
            "legacy-import",
        )
        report = self.service.backfill_seals()
        self.assertEqual(report["items"][0]["status"], "manual_review")
        reviews = self.service.list("manual_review")
        self.assertEqual(reviews[0]["data"]["topic"], "seal_backfill")

    def test_reconcile_matches_legacy_sample_by_time_and_athlete(self):
        """实验室结果带封条号但本地旧样本没有：对账按运动员+采样时刻回填命中。"""
        athlete = self._athlete()
        collected_at = "2025-12-01T08:00:00+00:00"
        # 旧样本：无封条号，但结果里带了运动员编号
        self.repo.create_entity(
            "sample-LEGACY", "sample", "analyzed",
            {
                "athlete_id": athlete["id"], "sample_code": "LEGACY",
                "event": "OOC-2025", "collected_at": collected_at,
            },
            "legacy-import",
        )
        self.service.upload_lab_result(
            {
                "report_no": "R-LEG", "revision": 1, "seal_id": "OLD-SEAL-9",
                "collected_at": "2025-12-01T08:00:00Z",
                "athlete_id": athlete["id"],
                "result": "adverse", "aliquot": "A",
            },
            self.lab,
        )
        report = self.service.reconcile()
        self.assertEqual(report["items"][0]["status"], "matched")
        sample = self.service.get("sample-LEGACY")
        self.assertEqual(sample["status"], "adverse")
        self.assertEqual(len(self.service.list("case")), 1)

    def test_reconcile_resumes_after_match_before_case_propagation(self):
        """崩溃点：结果已 matched、样本已写回，但案件还没立起来。"""
        athlete = self._athlete()
        sample = self._offline_sample(athlete["id"])["sample"]
        result, _ = self.service.upload_lab_result(
            {
                "report_no": "R-910", "revision": 1, "seal_id": "SEAL-1",
                "collected_at": "2026-03-01T08:00:00Z",
                "result": "adverse", "aliquot": "A",
            },
            self.lab,
        )
        # 手工造出"匹配完、立案前"的半成品现场
        self.service.transition(SYSTEM_ACTOR, result["id"], "match",
                                {"sample_id": sample["id"]})
        self.service.transition(
            SYSTEM_ACTOR, sample["id"], "reconcile_findings",
            {"result": "adverse", "result_id": result["id"], "revision": 1},
        )
        self.assertEqual(self.service.reconcile()["processed"], 1)
        self.assertEqual(len(self.service.list("case")), 1)
        self.assertEqual(self.service.list("case")[0]["status"], "suspended")
        # 再跑不再续办
        self.assertEqual(self.service.reconcile()["processed"], 0)

    def test_reconcile_interrupted_halfway_is_idempotent(self):
        """对账传播在通知之后崩溃，重跑同一结果不得重复通知/立案。"""
        athlete = self._athlete()
        sample = self._offline_sample(athlete["id"])["sample"]
        self.service.upload_lab_result(
            {
                "report_no": "R-900", "revision": 1, "seal_id": "SEAL-1",
                "collected_at": "2026-03-01T08:00:00Z",
                "result": "adverse", "aliquot": "A",
            },
            self.lab,
        )
        real_notify = self.service._notify
        calls = {"count": 0}

        def flaky_notify(*args, **kwargs):
            calls["count"] += 1
            created = real_notify(*args, **kwargs)
            if calls["count"] == 1:
                # 通知已落库、记账已完成，但进程在事务外崩溃
                raise RuntimeError("simulated crash after first notify")
            return created

        with mock.patch.object(self.service, "_notify", flaky_notify):
            with self.assertRaises(RuntimeError):
                self.service.reconcile()

        # 现场：notification 已写、case 已 last_result_id 记账，结果仍 recorded
        self.assertEqual(len(self.service.list("notification")), 1)
        # 重跑：通知不再重复，案件状态正常收敛
        self.service.reconcile()
        self.assertEqual(len(self.service.list("case")), 1)
        self.assertEqual(len(self.service.list("notification")), 1)
        case = self.service.list("case")[0]
        self.assertEqual(case["status"], "suspended")


if __name__ == "__main__":
    unittest.main()
