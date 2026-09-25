"""提供不依赖第三方框架的 HTTP/JSON 边界。"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from .equipment import EquipmentService
from .errors import DomainError, ValidationError
from .service import DomainService
from .storage import Database


def _receipt_reply(receipt) -> tuple[int, dict[str, Any]]:
    return (200 if receipt.replayed else 201), receipt.__dict__


def route(service: DomainService, method: str, path: str, body: dict[str, Any] | None,
          headers: dict[str, str] | None = None) -> tuple[int, dict[str, Any]]:
    """把一个 HTTP 语义请求分派到领域服务。"""

    headers = headers or {}
    body = body or {}
    parsed = urlparse(path)
    actor_id = headers.get("X-Actor-Id", "")
    try:
        if method == "GET" and parsed.path == "/health":
            valid, count = service.verify_audit()
            return 200, {"status": "ok", "audit_valid": valid, "audit_events": count}
        if method == "POST" and parsed.path == "/organizations":
            receipt = service.register_organization(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/actors":
            receipt = service.register_actor(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/sites":
            receipt = service.register_site(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/domain-records":
            receipt = service.record_domain_data(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "GET" and parsed.path == "/domain-records":
            query = parse_qs(parsed.query)
            site_id = query.get("site_id", [""])[0]
            if not site_id:
                raise ValidationError("site_id 不能为空")
            category = query.get("category", [None])[0]
            return 200, {"items": [item.__dict__ for item in service.list_domain_data(site_id, category)]}
        if method == "GET" and parsed.path == "/audit-events":
            query = parse_qs(parsed.query)
            after = int(query.get("after_sequence", ["0"])[0])
            return 200, {"items": service.audit_events(after)}
        if method == "POST" and parsed.path == "/equipment":
            return _receipt_reply(service.register_equipment(actor_id=actor_id, **body))
        if method == "POST" and parsed.path == "/equipment-capability-changes":
            return _receipt_reply(service.change_equipment_capability(actor_id=actor_id, **body))
        if method == "POST" and parsed.path == "/attachments":
            return _receipt_reply(service.register_attachment(actor_id=actor_id, **body))
        if method == "POST" and parsed.path == "/attachment-capability-changes":
            return _receipt_reply(service.change_attachment_capability(actor_id=actor_id, **body))
        if method == "POST" and parsed.path == "/calibration-certificates":
            return _receipt_reply(service.register_calibration(actor_id=actor_id, **body))
        if method == "POST" and parsed.path == "/open-windows":
            return _receipt_reply(service.register_open_window(actor_id=actor_id, **body))
        if method == "POST" and parsed.path == "/changeover-rules":
            return _receipt_reply(service.register_changeover_rule(actor_id=actor_id, **body))
        if method == "POST" and parsed.path == "/slot-searches":
            return 200, service.search_slots(**body)
        if method == "POST" and parsed.path == "/reservations":
            return _receipt_reply(service.confirm_reservation(actor_id=actor_id, **body))
        if method == "GET" and parsed.path.startswith("/reservations/"):
            reservation_id = parsed.path[len("/reservations/"):]
            if not reservation_id:
                raise ValidationError("reservation_id 不能为空")
            return 200, service.get_reservation(reservation_id)
        if method == "POST" and parsed.path == "/reservation-cancellations":
            return _receipt_reply(service.cancel_reservation(actor_id=actor_id, **body))
        if method == "POST" and parsed.path == "/maintenance-blocks":
            return _receipt_reply(service.publish_maintenance_block(actor_id=actor_id, **body))
        if method == "POST" and parsed.path == "/emergency-deactivations":
            return _receipt_reply(service.emergency_deactivate(actor_id=actor_id, **body))
        if method == "POST" and parsed.path == "/reactivations":
            return _receipt_reply(service.reactivate_resource(actor_id=actor_id, **body))
        if method == "POST" and parsed.path == "/usage-risk-decisions":
            return _receipt_reply(service.decide_usage_risk(actor_id=actor_id, **body))
        if method == "POST" and parsed.path == "/reschedule-entry-closures":
            return _receipt_reply(service.close_reschedule_entry(actor_id=actor_id, **body))
        if method == "GET" and parsed.path == "/equipment":
            query = parse_qs(parsed.query)
            site_id = query.get("site_id", [""])[0]
            if not site_id:
                raise ValidationError("site_id 不能为空")
            return 200, service.list_equipment(site_id)
        if method == "GET" and parsed.path == "/occupancy":
            query = parse_qs(parsed.query)
            site_id = query.get("site_id", [""])[0]
            if not site_id:
                raise ValidationError("site_id 不能为空")
            return 200, service.resource_occupancy(site_id, query.get("start_at", [""])[0],
                                                   query.get("end_at", [""])[0])
        if method == "GET" and parsed.path == "/reschedule-queue":
            query = parse_qs(parsed.query)
            site_id = query.get("site_id", [""])[0]
            if not site_id:
                raise ValidationError("site_id 不能为空")
            return 200, {"items": service.list_reschedule_queue(site_id)}
        if method == "GET" and parsed.path == "/usage-risks":
            query = parse_qs(parsed.query)
            site_id = query.get("site_id", [""])[0]
            if not site_id:
                raise ValidationError("site_id 不能为空")
            return 200, {"items": service.list_usage_risks(site_id, query.get("status", [None])[0])}
        if method == "GET" and parsed.path == "/manual-overrides":
            query = parse_qs(parsed.query)
            site_id = query.get("site_id", [""])[0]
            if not site_id:
                raise ValidationError("site_id 不能为空")
            return 200, {"items": service.list_manual_overrides(site_id)}
        return 404, {"error": "route_not_found", "message": "接口不存在"}
    except DomainError as exc:
        return exc.status, {"error": exc.code, "message": str(exc)}
    except (TypeError, ValueError) as exc:
        return 400, {"error": "invalid_request", "message": str(exc)}


class Handler(BaseHTTPRequestHandler):
    """把标准库 HTTP 请求转换为路由调用。"""

    service: DomainService

    def _handle(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._write(400, {"error": "invalid_json", "message": "请求体必须是 UTF-8 JSON"})
            return
        status, payload = route(self.service, self.command, self.path, body,
                                {"X-Actor-Id": self.headers.get("X-Actor-Id", "")})
        self._write(status, payload)

    def _write(self, status: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        self._handle()

    def do_POST(self) -> None:
        self._handle()

    def log_message(self, format: str, *args: object) -> None:
        return


def main() -> int:
    """启动本地 HTTP 服务。"""

    parser = argparse.ArgumentParser(description="启动技能赛训协作基础服务")
    parser.add_argument("--database", default="service.sqlite3")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    database = Database(args.database)
    Handler.service = EquipmentService(database)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        database.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
