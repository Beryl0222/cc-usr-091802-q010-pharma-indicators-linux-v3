"""医药规划指标证据库的运行入口。

只读 HTTP 服务，所有数字都由来源链实时推导或取自已冻结快照，不提供任何
“手填汇总数”写入接口。访问未公开合同需要在请求头携带授权：

* ``X-Viewer``        监测人员标识
* ``X-Clearance``     public | restricted | confidential
* ``X-Grants``        逗号分隔的合同授权标识

路由
----
``GET /health``
    服务身份。
``GET /periods/{year}/candidates/{cid}``
    品种当前结论（是否计入、首创依据、去重后全球销售、换算规则、待确认证据）。
``GET /periods/{year}/indicators``
    指标当前值（FIC 全球占比、年销超十亿美元品种数）。
``GET /periods/{year}/published/latest``
    最近一次冻结发布快照。
``GET /periods/{year}/published/{freeze_id}``
    指定冻结快照（已发布年度数字）。
``GET /periods/{year}/published/{freeze_id}/verify``
    用快照事件序号重算并逐位校验。
"""

import argparse
import importlib.util
import json
from functools import lru_cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote

from evidence_db import evidence as ev
from evidence_db.engine import Engine, Viewer
from evidence_db.timeline import annual_period

SERVICE_ID = "pharma-indicator-evidence"
SERVICE_NAME = "医药规划指标证据库"


def health_payload():
    """返回稳定的服务身份信息。"""
    return {"status": "ok", "service": SERVICE_ID, "name": SERVICE_NAME}


@lru_cache(maxsize=1)
def demo_engine() -> Engine:
    """加载内置虚构场景用于演示与自检；生产环境由持久化登记册注入。"""
    path = Path(__file__).parent / "fixtures" / "scenario.py"
    spec = importlib.util.spec_from_file_location("scenario_fixture", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    _reg, engine, _handles = module.build_registry()
    # 演示库加载时冻结 2025 期，使已发布快照路由可用
    engine.freeze_period(annual_period(2025), "2026-03-15")
    return engine


def viewer_from_headers(headers) -> Viewer:
    clearance_map = {
        "public": ev.Confidentiality.PUBLIC,
        "restricted": ev.Confidentiality.RESTRICTED,
        "confidential": ev.Confidentiality.CONFIDENTIAL,
    }
    grants = frozenset(
        g.strip() for g in (headers.get("X-Grants") or "").split(",") if g.strip()
    )
    clearance = clearance_map.get(
        (headers.get("X-Clearance") or "public").lower(),
        ev.Confidentiality.PUBLIC,
    )
    return Viewer(user=headers.get("X-Viewer") or "anonymous",
                  grants=grants, clearance=clearance)


class Handler(BaseHTTPRequestHandler):
    engine: Engine = None  # 由 build_server 注入

    # ----- 基础工具 -------------------------------------------------------

    def _send_json(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_error_json(self, status, code, message):
        self._send_json({"error": code, "message": message}, status)

    def log_message(self, *_args):
        return

    # ----- 路由 -----------------------------------------------------------

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        try:
            if path == "/health":
                self._send_json(health_payload())
                return
            self._route(path)
        except KeyError as exc:
            self._send_error_json(404, "not_found", str(exc).strip("'"))
        except Exception as exc:  # noqa: BLE001 - 服务层统一返回错误信封
            self._send_error_json(400, "bad_request", f"{type(exc).__name__}: {exc}")

    def _route(self, path):
        parts = [unquote(p) for p in path.strip("/").split("/") if p]
        # /periods/{year}/...
        if len(parts) >= 3 and parts[0] == "periods":
            year = int(parts[1])
            period = annual_period(year)
            engine = self.engine or demo_engine()
            viewer = viewer_from_headers(self.headers)
            rest = parts[2:]

            if rest == ["indicators"]:
                self._send_json(engine.indicators(period))
                return
            if len(rest) == 2 and rest[0] == "candidates":
                self._send_json(
                    engine.visible_conclusion(period, rest[1], viewer)
                )
                return
            if rest[:1] == ["published"]:
                self._route_published(engine, period.code, rest[1:])
                return
        self._send_error_json(404, "not_found", f"无此路由: {path}")

    def _route_published(self, engine, period_code, rest):
        registry = engine.registry
        if rest == ["latest"]:
            snap = registry.latest_freeze(period_code)
            if snap is None:
                raise KeyError(f"{period_code} 期尚无冻结发布")
            self._send_json(snap.to_dict())
            return
        if len(rest) == 1:
            self._send_json(registry.get_freeze(period_code, rest[0]).to_dict())
            return
        if len(rest) == 2 and rest[1] == "verify":
            period = annual_period(int(period_code))
            ok = engine.verify_published(period, rest[0])
            self._send_json({"freeze_id": rest[0], "reproduces_published": ok})
            return
        raise KeyError("/published 子路径不存在")


def build_server(port: int, engine: Engine = None) -> ThreadingHTTPServer:
    """构造服务器，允许注入特定登记册派生的引擎。"""
    handler = Handler
    handler.engine = engine

    class _Server(ThreadingHTTPServer):
        daemon_threads = True

    return _Server(("0.0.0.0", port), handler)


def main():
    parser = argparse.ArgumentParser(description=SERVICE_NAME)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.check:
        assert health_payload()["service"] == SERVICE_ID
        # 自检：演示场景加载即冻结 2025 期，并逐位复现已发布数字
        engine = demo_engine()
        snap = engine.registry.latest_freeze("2025")
        assert snap is not None, "缺少 2025 期冻结"
        assert engine.verify_published(annual_period(2025), snap.freeze_id), "冻结复现失败"
        print("基础检查通过（含冻结复现自检）")
        return
    build_server(args.port).serve_forever()


if __name__ == "__main__":
    main()
