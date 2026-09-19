"""HTTP 只读 API 契约测试：健康检查、结论、指标、已发布快照复现、授权头脱敏。"""

import importlib.util
import json
import threading
import unittest
import urllib.request
from pathlib import Path

import service
from evidence_db.timeline import annual_period


def _load_scenario_module():
    path = Path(__file__).resolve().parent.parent / "fixtures" / "scenario.py"
    spec = importlib.util.spec_from_file_location("scenario_fixture_http", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _ServerThread:
    def __init__(self, engine):
        self.httpd = service.build_server(0, engine)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)

    def get(self, path, headers=None):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}",
                                     headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))


class HttpApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sc = _load_scenario_module()
        reg, engine, handles = sc.build_registry()
        snap = engine.freeze_period(annual_period(2025), "2026-03-15")
        cls.freeze_id = snap["freeze_id"]
        cls.cid = handles["candidate"]
        cls.grant = handles["grants"]["contract"]
        cls.server = _ServerThread(engine)
        cls.server.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.server.__exit__(None, None, None)

    def test_health(self):
        status, body = self.server.get("/health")
        self.assertEqual(status, 200)
        self.assertEqual(body["service"], service.SERVICE_ID)

    def test_indicators_and_candidate(self):
        status, ind = self.server.get("/periods/2025/indicators")
        self.assertEqual(status, 200)
        self.assertEqual(ind["blockbuster-varieties"]["value"], 1)

        status, concl = self.server.get(f"/periods/2025/candidates/{self.cid}")
        self.assertEqual(status, 200)
        self.assertTrue(concl["included"])
        self.assertTrue(concl["global_sales"]["over_blockbuster"])

    def test_published_snapshot_and_verify(self):
        status, snap = self.server.get("/periods/2025/published/latest")
        self.assertEqual(status, 200)
        self.assertEqual(snap["freeze_id"], self.freeze_id)

        status, verify = self.server.get(
            f"/periods/2025/published/{self.freeze_id}/verify")
        self.assertEqual(status, 200)
        self.assertTrue(verify["reproduces_published"])

    def test_acl_header_redaction(self):
        # 未授权：合同出处被脱敏
        _, public = self.server.get(f"/periods/2025/candidates/{self.cid}")
        self.assertIn("LicenseRelation:LL-1", public["access"]["redacted"])

        # 授权 + 保密级 clearance：可见合同编号
        _, granted = self.server.get(
            f"/periods/2025/candidates/{self.cid}",
            headers={"X-Viewer": "zhao", "X-Clearance": "confidential",
                     "X-Grants": self.grant},
        )
        self.assertNotIn("access", granted)
        ll = [l for l in granted["licensing"] if l["relation_id"] == "LL-1"][0]
        self.assertEqual(ll["source"]["doc_id"], "contract-AB-2021")

    def test_unknown_route_is_json_404(self):
        status, body = self.server.get("/nope")
        self.assertEqual(status, 404)
        self.assertIn("error", body)


if __name__ == "__main__":
    unittest.main()
