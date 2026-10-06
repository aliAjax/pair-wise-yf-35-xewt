import json
import tempfile
import threading
import unittest
import urllib.request
import urllib.error
from pathlib import Path

from src.http_api import create_server
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


def _request(url, method="GET", body=None, headers=None, port=None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(
        "http://127.0.0.1:%s%s" % (port, url),
        data=data,
        method=method,
        headers={"Content-Type": "application/json", **(headers or {})},
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


class HttpApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        repo = SQLiteRepository(Path(cls.tmp.name) / "http.db")
        cls.service = DomainService(repo, RuleEngine())
        cls.server = create_server("127.0.0.1", 0, cls.service, RuleEngine(),
                                   str(Path("static").resolve()))
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.tmp.cleanup()

    def test_chain_over_http(self):
        port = self.port
        admin = {"X-User-Id": "admin", "X-Role": "admin"}
        dco = {"X-User-Id": "DCO-9", "X-Role": "inspector"}
        lab = {"X-User-Id": "LAB", "X-Role": "lab"}
        panel = {"X-User-Id": "panel", "X-Role": "panel"}

        status, athlete = _request("/api/athletes", "POST",
                                   {"name": "H. Sprinter", "discipline": "athletics"},
                                   admin, port)
        self.assertEqual(status, 201)

        # 无信号现场采集包（网络恢复后补传）
        packet = {
            "athlete_id": athlete["id"], "sample_code": "HTTP-1",
            "event": "OOC", "collected_at": "2026-06-01T08:00:00Z",
            "seal_id": "SEAL-HTTP-1",
        }
        status, first = _request("/api/collection-packets", "POST", packet, dco, port)
        self.assertEqual(status, 201)
        # 重放不重复
        status, second = _request("/api/collection-packets", "POST", packet, dco, port)
        self.assertEqual(second["sample"]["id"], first["sample"]["id"])
        self.assertTrue(second["replayed"])

        sample_id = first["sample"]["id"]
        handover_id = first["seal_handover"]["id"]
        status, _ = _request("/api/entities/%s/actions" % handover_id, "POST",
                             {"action": "hand_over", "data": {"carrier": "C"}}, dco, port)
        self.assertEqual(status, 200)

        # A 样阳性结果上报 + 对账
        result_a = {
            "report_no": "HTTP-R1", "revision": 1, "seal_id": "SEAL-HTTP-1",
            "collected_at": "2026-06-01T08:00:00Z",
            "result": "adverse", "aliquot": "A", "substance": "EPO",
        }
        status, created_a = _request("/api/lab-results/ingest", "POST", result_a,
                                     lab, port)
        self.assertEqual(status, 201)
        status, report = _request("/api/reconcile", "POST", {}, admin, port)
        self.assertEqual(status, 200)
        self.assertEqual(report["processed"], 1)

        status, cases = _request("/api/cases", "GET", port=port)
        case_id = cases["items"][0]["id"]
        self.assertEqual(cases["items"][0]["status"], "suspended")

        # B 样确认前结案被拒
        status, body = _request("/api/entities/%s/actions" % case_id, "POST",
                                {"action": "schedule_hearing",
                                 "data": {"hearing_at": "2026-07-01"}}, panel, port)
        self.assertEqual(status, 200)
        status, body = _request("/api/entities/%s/actions" % case_id, "POST",
                                {"action": "decide",
                                 "data": {"decision": "sanction"}}, panel, port)
        self.assertEqual(status, 409)

        # B 样阳性 -> 重新确认禁赛 -> 再排听证 -> 结案
        result_b = dict(result_a, report_no="HTTP-R1-B", aliquot="B")
        _request("/api/lab-results/ingest", "POST", result_b, lab, port)
        _request("/api/reconcile", "POST", {}, admin, port)
        _request("/api/entities/%s/actions" % case_id, "POST",
                 {"action": "schedule_hearing",
                  "data": {"hearing_at": "2026-08-01"}}, panel, port)
        status, closed = _request("/api/entities/%s/actions" % case_id, "POST",
                                  {"action": "decide",
                                   "data": {"decision": "sanction"}}, panel, port)
        self.assertEqual(status, 200)
        self.assertEqual(closed["status"], "closed")

        # 通知只有两条：A 样停赛 + B 样重新确认
        status, notes = _request("/api/notifications", "GET", port=port)
        self.assertEqual(status, 200)
        self.assertEqual(len(notes["items"]), 2)

        # 再对账不产生任何重复
        status, again = _request("/api/reconcile", "POST", {}, admin, port)
        self.assertEqual(again["processed"], 0)
        status, notes = _request("/api/notifications", "GET", port=port)
        self.assertEqual(len(notes["items"]), 2)


if __name__ == "__main__":
    unittest.main()
