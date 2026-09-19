"""医药规划指标证据库的运行入口（健康检查 + 只读指标 API）。

* ``python3 service.py --check`` 检查基础入口。
* ``python3 service.py --data fixtures/dataset --bootstrap`` 装载证据库并冻结一期。
* HTTP：``/health`` 健康检查；``/api/...`` 提供只读发布查询（支持使用人密级头）。

发布数字只读、不可手填；冻结与局部重算走库 API（见 evidence_base.app / .engine）。
"""

import argparse
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, unquote

from evidence_base.app import NotFound, Service
from evidence_base.loader import load_dataset_dir

SERVICE_ID = "pharma-indicator-evidence"
SERVICE_NAME = "医药规划指标证据库"

# 初次冻结的默认时点（早于 events.json 中的重述/更正/后到证据）
DEFAULT_FREEZE_AS_OF = "2026-07-31T00:00:00Z"
DEFAULT_PERIOD_ID = "FY2025"
DEFAULT_SNAPSHOT_ID = "SNAP-FY2025-V1"


def health_payload():
    """返回稳定的服务身份信息。"""
    return {"status": "ok", "service": SERVICE_ID, "name": SERVICE_NAME}


def bootstrap(data_dir: str,
              period_id: str = DEFAULT_PERIOD_ID,
              as_of: str = DEFAULT_FREEZE_AS_OF,
              snapshot_id: str = DEFAULT_SNAPSHOT_ID) -> Service:
    """装载证据库并冻结指定统计期，返回只读应用服务。"""
    dataset = load_dataset_dir(data_dir)
    svc = Service(dataset)
    svc.freeze(period_id, as_of, snapshot_id, frozen_at=as_of)
    return svc


class Handler(BaseHTTPRequestHandler):
    svc: Service = None  # 由 make_server 注入

    # ----------------------------------------------------------------- 工具
    def _send(self, code: int, payload) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _actor(self):
        actor_id = self.headers.get("X-Actor-Id")
        clearance = self.headers.get("X-Clearance")
        return self.svc.actor_for(
            actor_id, int(clearance) if clearance is not None else None
        )

    def log_message(self, *_args):
        return

    # ----------------------------------------------------------------- 路由
    def do_GET(self):
        parts = [unquote(p) for p in urlsplit(self.path).path.strip("/").split("/") if p]
        try:
            if parts == ["health"]:
                self._send(200, health_payload()); return
            if self.svc is None:
                self._send(503, {"error": "evidence-base-not-loaded"}); return

            if parts == ["api", "periods"]:
                self._send(200, {"periods": self.svc.periods()}); return
            if parts == ["api", "snapshots"]:
                self._send(200, {"snapshots": self.svc.list_snapshots()}); return

            if len(parts) >= 3 and parts[:2] == ["api", "snapshots"]:
                snap_id = parts[2]
                if len(parts) == 3:
                    self._send(200, self.svc.get_snapshot(snap_id, self._actor())); return
                if len(parts) == 4 and parts[3] == "metrics":
                    self._send(200, self.svc.get_metrics(snap_id)); return
                if len(parts) == 4 and parts[3] == "reproduce":
                    self._send(200, self.svc.reproduce(snap_id)); return
                if len(parts) == 6 and parts[3] == "candidates":
                    self._send(200, self.svc.get_determination(
                        snap_id, parts[4], self._actor())); return

            if len(parts) == 3 and parts[:2] == ["api", "evidence"]:
                self._send(200, self.svc.get_evidence(parts[2], self._actor())); return

            self._send(404, {"error": "not-found", "path": self.path})
        except NotFound as exc:
            self._send(404, {"error": "not-found", "detail": str(exc)})
        except PermissionError as exc:
            self._send(403, {"error": "forbidden", "detail": str(exc)})
        except Exception as exc:  # noqa: BLE001
            self._send(500, {"error": "internal", "detail": str(exc)})


def make_server(port: int, svc: Service = None) -> ThreadingHTTPServer:
    handler = Handler
    handler.svc = svc
    return ThreadingHTTPServer(("0.0.0.0", port), handler)


def main():
    parser = argparse.ArgumentParser(description=SERVICE_NAME)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--data", default=os.environ.get("EVIDENCE_DATA_DIR"),
                        help="证据数据集目录")
    parser.add_argument("--bootstrap", action="store_true",
                        help="装载数据集并冻结默认统计期")
    args = parser.parse_args()

    if args.check:
        assert health_payload()["service"] == SERVICE_ID
        print("基础检查通过")
        return

    svc = None
    if args.data and args.bootstrap:
        svc = bootstrap(args.data)
    elif args.data:
        svc = Service(load_dataset_dir(args.data))

    server = make_server(args.port, svc)
    print(f"{SERVICE_NAME} listening on :{args.port} (data={'loaded' if svc else 'none'})")
    server.serve_forever()


if __name__ == "__main__":
    main()
