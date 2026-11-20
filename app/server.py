"""HTTP server: JSON API plus static files.  Standard library only."""

from __future__ import annotations

import json
import mimetypes
import os
import re
import sys
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import db, service, strategies, timeutil
from .demo import scenarios_catalog, seed_demo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC = os.path.join(ROOT, "static")


class NotFound(KeyError):
    pass


class HttpError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def _int(body: dict, key: str, required: bool = True, default=None) -> int | None:
    if key not in body or body[key] is None or body[key] == "":
        if required:
            raise HttpError(400, f"缺少字段 {key}")
        return default
    try:
        return int(body[key])
    except (TypeError, ValueError):
        raise HttpError(400, f"字段 {key} 必须是整数")


def _float(body: dict, key: str, default=None):
    if key not in body or body[key] is None or body[key] == "":
        return default
    try:
        value = float(body[key])
    except (TypeError, ValueError):
        raise HttpError(400, f"字段 {key} 必须是数字")
    if value != value or value in (float("inf"), float("-inf")):
        raise HttpError(400, f"字段 {key} 必须是有限数字")
    return value


def _query_float(query: dict, key: str, default: float) -> float:
    raw = query.get(key)
    if raw is None or raw == "":
        return default
    try:
        value = float(raw)
    except ValueError:
        raise HttpError(400, f"查询参数 {key} 必须是数字")
    if value != value or value in (float("inf"), float("-inf")):
        raise HttpError(400, f"查询参数 {key} 必须是有限数字")
    return value


def _query_int(query: dict, key: str, default: int) -> int:
    raw = query.get(key)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        raise HttpError(400, f"查询参数 {key} 必须是整数")


class Handler(BaseHTTPRequestHandler):
    server_version = "SparseTS/1.0"

    def log_message(self, fmt, *args):  # quieter logs
        if os.environ.get("SPARSE_VERBOSE"):
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    # ---------------------------------------------------------------- helpers
    def _json(self, payload, status=200):
        data = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _read(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length == 0:
            return {}
        if length > 20_000_000:
            raise HttpError(413, "请求体过大")
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw)
        except ValueError:
            raise HttpError(400, "请求体不是合法 JSON")
        if not isinstance(body, dict):
            raise HttpError(400, "请求体必须是 JSON 对象")
        return body

    # ------------------------------------------------------------------ verbs
    def _safe(self, method: str):
        try:
            self._dispatch(method)
        except HttpError as exc:
            self._json({"error": exc.message}, exc.status)
        except KeyError as exc:
            self._json({"error": str(exc).strip("'\"")}, 404)
        except Exception:
            traceback.print_exc()
            self._json({"error": "服务器内部错误"}, 500)

    def do_GET(self):
        self._safe("GET")

    def do_POST(self):
        self._safe("POST")

    def do_DELETE(self):
        self._safe("DELETE")

    # -------------------------------------------------------------- routing
    def _dispatch(self, method: str):
        try:
            self._dispatch_locked(method)
        except Exception:
            # The SQLite connection is shared by every request.  If a route
            # raises part-way through a write, the open transaction must be
            # rolled back; otherwise the next request inherits its write locks
            # and fails with an unrelated error.
            if getattr(self, "_request_used_conn", False):
                conn = db._conn
                if conn is not None:
                    try:
                        conn.rollback()
                    except Exception:
                        pass
            raise

    def _dispatch_locked(self, method: str):
        self._request_used_conn = False
        parsed = urlparse(self.path)
        path = parsed.path
        query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        if not path.startswith("/api/"):
            return self._static(path)

        self._request_used_conn = True
        conn = db.connect()
        with db._lock:
            if path == "/api/health" and method == "GET":
                return self._json({"ok": True, "version": "1.0.0"})

            if path == "/api/strategies" and method == "GET":
                return self._json({"strategies": service.list_strategies(conn)})

            if path == "/api/series" and method == "GET":
                return self._json({"series": service.list_series(conn)})

            if path == "/api/scenarios" and method == "GET":
                return self._json({"scenarios": scenarios_catalog()})

            if path == "/api/series" and method == "POST":
                body = self._read()
                for key in ("name", "timezone", "window_start", "window_end"):
                    if key not in body:
                        raise HttpError(400, f"缺少字段 {key}")
                kind = body.get("interval_kind", "fixed")
                if kind not in ("fixed", "daily"):
                    raise HttpError(400, "interval_kind 只能是 fixed 或 daily")
                if kind == "fixed":
                    step = int(body.get("interval_seconds") or 3600)
                    if step <= 0:
                        raise HttpError(400, "interval_seconds 必须为正")
                if int(body["window_start"]) >= int(body["window_end"]):
                    raise HttpError(400, "window_start 必须早于 window_end")
                try:
                    timeutil.ZoneInfo(body["timezone"])
                except Exception:
                    raise HttpError(400, f"未知时区 {body.get('timezone')}")
                try:
                    created = service.create_series(conn, body)
                except (ValueError, KeyError) as exc:
                    raise HttpError(400, str(exc))
                return self._json(created, 201)

            if path == "/api/demo" and method == "POST":
                body = self._read()
                scenario = body.get("scenario", "dst_spring")
                result = seed_demo(conn, scenario, reset=bool(body.get("reset", True)))
                self._json(result)
                return

            m = re.fullmatch(r"/api/series/([\w.-]+)", path)
            if m and method == "GET":
                series = service.get_series(conn, m.group(1))
                return self._json(service.series_payload(conn, series))

            m = re.fullmatch(r"/api/series/([\w.-]+)/gaps", path)
            if m and method == "GET":
                series = service.get_series(conn, m.group(1))
                payload = service.series_payload(conn, series)
                return self._json({"gaps": payload["gaps"], "stats": payload["stats"]})

            m = re.fullmatch(r"/api/series/([\w.-]+)/timezone-audit", path)
            if m and method == "GET":
                series = service.get_series(conn, m.group(1))
                payload = service.series_payload(conn, series)
                return self._json(payload["timezone_audit"])

            m = re.fullmatch(r"/api/series/([\w.-]+)/ingest", path)
            if m and method == "POST":
                series = service.get_series(conn, m.group(1))
                body = self._read()
                points = body.get("points")
                if not isinstance(points, list) or not points:
                    raise HttpError(400, "points 必须是非空数组")
                cleaned = []
                for p in points:
                    if not isinstance(p, dict) or "ts" not in p or "value" not in p:
                        raise HttpError(400, "每个点需要 ts 和 value")
                    try:
                        cleaned.append({"ts": int(p["ts"]), "value": float(p["value"])})
                    except (TypeError, ValueError):
                        raise HttpError(400, "ts 必须是毫秒整数，value 必须是数字")
                received = _int(body, "received_ts", required=False)
                try:
                    return self._json(
                        service.ingest(
                            conn, series, cleaned,
                            kind=body.get("kind", "stream"),
                            note=body.get("note", ""),
                            source=body.get("source", "api"),
                            received_ts=received,
                        )
                    )
                except ValueError as exc:
                    raise HttpError(400, str(exc))

            m = re.fullmatch(r"/api/series/([\w.-]+)/ingest-local", path)
            if m and method == "POST":
                series = service.get_series(conn, m.group(1))
                body = self._read()
                points = body.get("points")
                if not isinstance(points, list) or not points:
                    raise HttpError(400, "points 必须是非空数组")
                cleaned = []
                for p in points:
                    if not isinstance(p, dict) or "local" not in p or "value" not in p:
                        raise HttpError(400, "每个点需要 local 与 value")
                    try:
                        from datetime import datetime
                        naive = datetime.fromisoformat(str(p["local"]))
                    except ValueError:
                        raise HttpError(400, f"本地时间格式非法：{p.get('local')}")
                    naive = naive.replace(tzinfo=None)
                    ts, status = timeutil.resolve_local(naive, series["timezone"])
                    if status == "nonexistent":
                        raise HttpError(
                            400,
                            f"本地时间 {p['local']} 在 {series['timezone']} 中不存在（DST 前跳），"
                            "请改用存在的本地时刻或直接提交 UTC 毫秒 ts",
                        )
                    cleaned.append(
                        {"ts": ts, "value": float(p["value"]), "_status": status, "_local": str(p["local"])}
                    )
                try:
                    result = service.ingest(
                        conn, series,
                        [{"ts": c["ts"], "value": c["value"]} for c in cleaned],
                        kind=body.get("kind", "stream"),
                        note=body.get("note", ""),
                        source="api-local",
                        received_ts=_int(body, "received_ts", required=False),
                    )
                except ValueError as exc:
                    raise HttpError(400, str(exc))
                result["resolved"] = [
                    {"local": c["_local"], "ts": c["ts"], "status": c["_status"]} for c in cleaned
                ]
                return self._json(result)

            m = re.fullmatch(r"/api/series/([\w.-]+)/runs", path)
            if m and method == "GET":
                service.get_series(conn, m.group(1))
                return self._json({"runs": service.series_runs(conn, m.group(1))})

            if path == "/api/runs" and method == "POST":
                body = self._read()
                sid = body.get("series_id")
                if not sid:
                    raise HttpError(400, "缺少 series_id")
                series = service.get_series(conn, sid)
                strategy_id = body.get("strategy_id")
                if not strategy_id:
                    raise HttpError(400, "缺少 strategy_id")
                version = _int(body, "version", required=False)
                if version is None:
                    version = max(
                        v for (s, v) in strategies.VERSION_SPECS.items() if s == strategy_id
                    ) if any(s == strategy_id for (s, _) in strategies.VERSION_SPECS) else None
                if version is None:
                    raise HttpError(400, f"未知策略 {strategy_id}")
                params = body.get("params") or {}
                if not isinstance(params, dict):
                    raise HttpError(400, "params 必须是对象")
                try:
                    run = service.get_or_create_run(conn, series, strategy_id, version, params)
                except ValueError as exc:
                    raise HttpError(400, str(exc))
                return self._json({"run": run, "runs": service.series_runs(conn, sid)}, 201)

            m = re.fullmatch(r"/api/runs/(\d+)", path)
            if m and method == "DELETE":
                service.delete_run(conn, int(m.group(1)))
                return self._json({"deleted": int(m.group(1))})

            m = re.fullmatch(r"/api/runs/(\d+)/evaluate", path)
            if m and method == "GET":
                run_id = int(m.group(1))
                run = service._run_row(conn, run_id)
                series = service.get_series(conn, run["series_id"])
                fraction = _query_float(query, "holdout", 0.1)
                seed = _query_int(query, "seed", 7)
                return self._json(service.evaluate(conn, series, run_id, fraction, seed))

            if path == "/api/compare" and method == "GET":
                sid = query.get("series_id")
                if not sid:
                    raise HttpError(400, "缺少 series_id")
                series = service.get_series(conn, sid)
                try:
                    ids = [int(x) for x in query.get("runs", "").split(",") if x.strip()]
                except ValueError:
                    raise HttpError(400, "runs 必须是逗号分隔的整数")
                if len(ids) < 2:
                    raise HttpError(400, "至少需要两个 run_id")
                fraction = _query_float(query, "holdout", 0.1)
                seed = _query_int(query, "seed", 7)
                return self._json(service.compare(conn, series, ids, fraction, seed))

            m = re.fullmatch(r"/api/runs/(\d+)/replay", path)
            if m and method == "POST":
                body = self._read()
                as_of = _int(body, "as_of")
                return self._json(service.replay(conn, int(m.group(1)), as_of))

            if path == "/api/version-diff" and method == "GET":
                a = query.get("a")
                b = query.get("b")
                if not a or not b:
                    raise HttpError(400, "需要 a 与 b 两个 run_id")
                try:
                    return self._json(service.version_diff(conn, int(a), int(b)))
                except ValueError as exc:
                    raise HttpError(400, str(exc))

            m = re.fullmatch(r"/api/series/([\w.-]+)/batches", path)
            if m and method == "GET":
                service.get_series(conn, m.group(1))
                return self._json({"batches": service.batches(conn, m.group(1))})

            raise HttpError(404, "接口不存在")

    # --------------------------------------------------------------- static
    def _static(self, path: str):
        if path in ("", "/"):
            path = "/index.html"
        rel = path.lstrip("/")
        full = os.path.normpath(os.path.join(STATIC, rel))
        if not full.startswith(STATIC + os.sep) and full != STATIC:
            raise HttpError(403, "禁止访问")
        if not os.path.isfile(full):
            raise HttpError(404, "文件不存在")
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
            ctype += "; charset=utf-8"
        with open(full, "rb") as fh:
            data = fh.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)


def build_server(host: str = "127.0.0.1", port: int = 8765) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), Handler)


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="稀疏时间序列分析")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--db", default=os.environ.get("SPARSE_DB", os.path.join(ROOT, "sparse.db")))
    parser.add_argument("--seed-demo", action="store_true", help="启动时重建演示数据")
    args = parser.parse_args(argv)

    conn = db.connect(args.db)
    service.seed_registry(conn)
    if args.seed_demo:
        seed_demo(conn, "dst_spring", reset=True)

    srv = build_server(args.host, args.port)
    print(f"稀疏时间序列分析已启动: http://{args.host}:{args.port}  (db: {args.db})", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
