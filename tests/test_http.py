"""只读 HTTP API：健康检查、指标、品种结论、密级头、复现。"""

import json
import threading
import unittest
import urllib.request
import urllib.error

from service import make_server
from evidence_base.app import Service
from evidence_base.loader import load_dataset_dir

T1 = "2026-07-31T00:00:00Z"


class _Session:
    def __init__(self, port, svc):
        self.httpd = make_server(port, svc)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{port}"

    def stop(self):
        self.httpd.shutdown()

    def get(self, path, headers=None):
        req = urllib.request.Request(self.base + path, headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode("utf-8"))


class HttpApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        ds = load_dataset_dir("fixtures/dataset")
        svc = Service(ds)
        svc.freeze("FY2025", T1, "SNAP-V1", frozen_at=T1)
        cls.s = _Session(8123, svc)

    @classmethod
    def tearDownClass(cls):
        cls.s.stop()

    def test_health(self):
        code, body = self.s.get("/health")
        self.assertEqual(code, 200)
        self.assertEqual(body["service"], "pharma-indicator-evidence")

    def test_metrics_and_share(self):
        code, body = self.s.get("/api/snapshots/SNAP-V1/metrics")
        self.assertEqual(code, 200)
        self.assertEqual(body["cohort_size"], 4)
        self.assertAlmostEqual(body["fic_global_share"], 0.5)
        self.assertEqual(body["blockbuster_over_1b_usd_count"], 2)

    def test_determination_explains_inclusion_and_chain(self):
        code, d = self.s.get("/api/snapshots/SNAP-V1/candidates/CD-NG07/determination")
        self.assertEqual(code, 200)
        self.assertTrue(d["included"])
        self.assertEqual(d["totals_provenance"], "derived-from-source-chain")
        eliminated = [l for l in d["sales"]["lines"]
                      if "intra-group-eliminated" in l["reasons"]]
        self.assertEqual(len(eliminated), 2)

    def test_conflict_candidate_lists_pending_evidence(self):
        code, d = self.s.get("/api/snapshots/SNAP-V1/candidates/CD-X9/determination")
        self.assertFalse(d["included"])
        self.assertTrue(any(
            p.get("code") == "unresolved-competing-opinions"
            for p in d["pending_evidence"]))
        self.assertEqual(d["sales"]["global_annual_anchor"], 0)

    def test_reproduce_endpoint(self):
        code, body = self.s.get("/api/snapshots/SNAP-V1/reproduce")
        self.assertEqual(code, 200)
        self.assertTrue(body["reproduced"])

    def test_clearance_header_and_evidence_403(self):
        code, body = self.s.get("/api/evidence/R-LIC-HLX-BRX")
        self.assertEqual(code, 403)
        code, body = self.s.get(
            "/api/evidence/R-LIC-HLX-BRX",
            headers={"X-Actor-Id": "analyst.li", "X-Clearance": "2"})
        self.assertEqual(code, 200)
        self.assertEqual(body["id"], "R-LIC-HLX-BRX")

    def test_unknown_snapshot_404(self):
        code, _ = self.s.get("/api/snapshots/NOPE/metrics")
        self.assertEqual(code, 404)


if __name__ == "__main__":
    unittest.main()
