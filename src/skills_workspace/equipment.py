"""实训设备预约与维护封锁的领域服务。

在基础服务之上登记设备能力版本、可组合附件、校准证书、开放窗口和转换规则，
支持按训练目标搜索候选时段、原子确认整组资源、发布计划封锁与紧急停用，
并对受影响预约生成改期队列、对已开始的使用生成待人工决定的风险记录。
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from .audit import append_event, canonical_json
from .errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from .models import Actor, WriteReceipt
from .service import DomainService


MAX_CANDIDATES = 50
RISK_DECISIONS = frozenset({"continue", "terminate"})


def _merge(intervals: list[tuple[datetime, datetime]]) -> list[tuple[datetime, datetime]]:
    """合并重叠或相接的区间。"""

    merged: list[list[datetime]] = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1]:
            if end > merged[-1][1]:
                merged[-1][1] = end
        else:
            merged.append([start, end])
    return [(item[0], item[1]) for item in merged]


def _intersect(first: list[tuple[datetime, datetime]],
               second: list[tuple[datetime, datetime]]) -> list[tuple[datetime, datetime]]:
    """计算两组区间的交集。"""

    result = []
    for left_start, left_end in first:
        for right_start, right_end in second:
            low = max(left_start, right_start)
            high = min(left_end, right_end)
            if low < high:
                result.append((low, high))
    return _merge(result)


def _subtract(base: list[tuple[datetime, datetime]],
              blocks: list[tuple[datetime, datetime]]) -> list[tuple[datetime, datetime]]:
    """从基础区间中扣除占用区间。"""

    remaining = list(base)
    for block_start, block_end in blocks:
        following = []
        for start, end in remaining:
            if block_end <= start or block_start >= end:
                following.append((start, end))
                continue
            if start < block_start:
                following.append((start, block_start))
            if block_end < end:
                following.append((block_end, end))
        remaining = following
    return remaining


class EquipmentService(DomainService):
    """在基础服务之上提供设备预约、校准核对与维护封锁能力。"""

    # ---------- 时间与校验工具 ----------

    def _now_moment(self) -> datetime:
        return self.clock.now().astimezone(timezone.utc).replace(microsecond=0)

    def _parse_time(self, value: Any, field: str) -> datetime:
        text = str(value).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            moment = datetime.fromisoformat(text)
        except ValueError as exc:
            raise ValidationError(f"{field} 必须是 ISO-8601 时间") from exc
        if moment.tzinfo is None:
            raise ValidationError(f"{field} 必须包含时区")
        return moment.astimezone(timezone.utc).replace(microsecond=0)

    def _iso(self, moment: datetime) -> str:
        return moment.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")

    def _minutes(self, value: Any, field: str) -> int:
        try:
            minutes = int(value)
        except (TypeError, ValueError) as exc:
            raise ValidationError(f"{field} 必须是整数") from exc
        if minutes < 0:
            raise ValidationError(f"{field} 不能为负数")
        return minutes

    def _normalize_capabilities(self, value: Any) -> list[str]:
        if not isinstance(value, list) or not value:
            raise ValidationError("capabilities 必须是非空字符串数组")
        normalized = sorted({str(item).strip() for item in value})
        if not all(normalized) or len(normalized) != len(value):
            raise ValidationError("capabilities 必须是非空字符串数组")
        return normalized

    def _normalize_compatible(self, value: Any) -> list[str]:
        if not isinstance(value, list) or not value:
            raise ValidationError("compatible_equipment 必须是非空数组")
        normalized = sorted({str(item).strip() for item in value})
        if not all(normalized) or len(normalized) != len(value):
            raise ValidationError("compatible_equipment 必须是非空数组")
        for item in normalized:
            if item != "*":
                self._identifier(item, "compatible_equipment")
        return normalized

    def _normalize_attachment_requests(self, value: Any) -> list[dict[str, Any]]:
        if value is None:
            return []
        if not isinstance(value, list):
            raise ValidationError("attachments 必须是数组")
        normalized = []
        seen = set()
        for item in value:
            if not isinstance(item, dict):
                raise ValidationError("attachments 元素必须是对象")
            attachment_id = self._identifier(str(item.get("attachment_id", "")), "attachment_id")
            if attachment_id in seen:
                raise ValidationError("attachments 中存在重复附件")
            seen.add(attachment_id)
            normalized.append({"attachment_id": attachment_id,
                               "expected_version": self._minutes(item.get("expected_version"), "expected_version")})
        normalized.sort(key=lambda item: item["attachment_id"])
        return normalized

    # ---------- 数据访问工具 ----------

    def _site_row(self, connection, site_id: str):
        row = connection.execute("SELECT * FROM sites WHERE site_id=?", (site_id,)).fetchone()
        if row is None:
            raise NotFoundError("场所不存在")
        return row

    def _check_site_scope(self, actor: Actor, site) -> None:
        if actor.organization_id != site["organization_id"] and actor.role != "admin":
            raise PermissionDenied("不能操作其他组织的场所")

    def _equipment_row(self, connection, equipment_id: str):
        row = connection.execute("SELECT * FROM equipment WHERE equipment_id=?", (equipment_id,)).fetchone()
        if row is None:
            raise NotFoundError("设备不存在")
        return row

    def _attachment_row(self, connection, attachment_id: str):
        row = connection.execute("SELECT * FROM attachments WHERE attachment_id=?", (attachment_id,)).fetchone()
        if row is None:
            raise NotFoundError("附件不存在")
        return row

    def _resource_row(self, connection, resource_type: str, resource_id: str):
        if resource_type == "equipment":
            return self._equipment_row(connection, resource_id)
        if resource_type == "attachment":
            return self._attachment_row(connection, resource_id)
        raise ValidationError("resource_type 必须是 equipment 或 attachment")

    def _compatible(self, attachment_row, equipment_id: str) -> bool:
        targets = json.loads(attachment_row["compatible_equipment_json"])
        return "*" in targets or equipment_id in targets

    def _window_intervals(self, connection, resource_type: str,
                          resource_id: str) -> list[tuple[datetime, datetime]]:
        rows = connection.execute(
            "SELECT start_at, end_at FROM open_windows WHERE resource_type=? AND resource_id=?",
            (resource_type, resource_id),
        ).fetchall()
        return _merge([(self._parse_time(row["start_at"], "start_at"),
                        self._parse_time(row["end_at"], "end_at")) for row in rows])

    def _windows_cover(self, connection, resource_type: str, resource_id: str,
                       start: datetime, end: datetime) -> bool:
        return not _subtract([(start, end)], self._window_intervals(connection, resource_type, resource_id))

    def _calibration_intervals(self, connection, resource_type: str,
                               resource_id: str) -> list[tuple[datetime, datetime]]:
        rows = connection.execute(
            "SELECT issued_at, expires_at FROM calibration_certificates WHERE resource_type=? AND resource_id=?",
            (resource_type, resource_id),
        ).fetchall()
        return _merge([(self._parse_time(row["issued_at"], "issued_at"),
                        self._parse_time(row["expires_at"], "expires_at")) for row in rows])

    def _calibration_covers(self, connection, resource_type: str, resource_id: str,
                            start: datetime, end: datetime) -> bool:
        return not _subtract([(start, end)], self._calibration_intervals(connection, resource_type, resource_id))

    def _calibration_basis(self, connection, resource_type: str, resource_id: str,
                           start: datetime, end: datetime) -> list[str]:
        rows = connection.execute(
            "SELECT certificate_id, issued_at, expires_at FROM calibration_certificates "
            "WHERE resource_type=? AND resource_id=? ORDER BY issued_at, certificate_id",
            (resource_type, resource_id),
        ).fetchall()
        return [row["certificate_id"] for row in rows
                if self._parse_time(row["issued_at"], "issued_at") < end
                and start < self._parse_time(row["expires_at"], "expires_at")]

    def _basis_or_fail(self, connection, resource_type: str, resource_id: str,
                       start: datetime, end: datetime) -> list[str]:
        if not self._calibration_covers(connection, resource_type, resource_id, start, end):
            raise ConflictError(f"{resource_id} 的校准证书无法覆盖预约时段")
        return self._calibration_basis(connection, resource_type, resource_id, start, end)

    def _reservation_rows(self, connection, resource_type: str, resource_id: str):
        if resource_type == "equipment":
            return connection.execute(
                "SELECT * FROM reservations WHERE equipment_id=? AND status='confirmed'", (resource_id,)
            ).fetchall()
        return connection.execute(
            "SELECT r.* FROM reservations r JOIN reservation_attachments ra "
            "ON r.reservation_id=ra.reservation_id WHERE ra.attachment_id=? AND r.status='confirmed'",
            (resource_id,),
        ).fetchall()

    def _overlapping_reservations(self, connection, resource_type: str, resource_id: str,
                                  start: datetime, end: datetime):
        result = []
        for row in self._reservation_rows(connection, resource_type, resource_id):
            row_start = self._parse_time(row["start_at"], "start_at")
            row_end = self._parse_time(row["end_at"], "end_at")
            if row_start < end and start < row_end:
                result.append(row)
        return sorted(result, key=lambda row: (row["start_at"], row["reservation_id"]))

    def _block_overlap(self, connection, resource_type: str, resource_id: str,
                       start: datetime, end: datetime) -> bool:
        rows = connection.execute(
            "SELECT start_at, end_at FROM maintenance_blocks WHERE resource_type=? AND resource_id=?",
            (resource_type, resource_id),
        ).fetchall()
        for row in rows:
            if self._parse_time(row["start_at"], "start_at") < end and start < self._parse_time(row["end_at"], "end_at"):
                return True
        return False

    def _busy_intervals(self, connection, equipment_id: str,
                        attachment_ids: list[str]) -> list[tuple[datetime, datetime]]:
        intervals: list[tuple[datetime, datetime]] = []
        resources = [("equipment", equipment_id)] + [("attachment", item) for item in attachment_ids]
        for resource_type, resource_id in resources:
            rows = connection.execute(
                "SELECT start_at, end_at FROM maintenance_blocks WHERE resource_type=? AND resource_id=?",
                (resource_type, resource_id),
            ).fetchall()
            intervals.extend((self._parse_time(row["start_at"], "start_at"),
                              self._parse_time(row["end_at"], "end_at")) for row in rows)
            intervals.extend((self._parse_time(row["start_at"], "start_at"),
                              self._parse_time(row["end_at"], "end_at"))
                             for row in self._reservation_rows(connection, resource_type, resource_id))
        return _merge(intervals)

    def _confirmed_reservations(self, connection, equipment_id: str) -> list[tuple[datetime, datetime, str]]:
        rows = connection.execute(
            "SELECT start_at, end_at, goal FROM reservations WHERE equipment_id=? AND status='confirmed'",
            (equipment_id,),
        ).fetchall()
        return sorted((self._parse_time(row["start_at"], "start_at"),
                       self._parse_time(row["end_at"], "end_at"), row["goal"]) for row in rows)

    def _changeover_rules(self, connection, equipment_id: str) -> dict[tuple[str, str], int]:
        rows = connection.execute(
            "SELECT from_goal, to_goal, minutes FROM changeover_rules WHERE equipment_id=?", (equipment_id,)
        ).fetchall()
        return {(row["from_goal"], row["to_goal"]): row["minutes"] for row in rows}

    def _changeover_minutes(self, rules: dict[tuple[str, str], int], from_goal: str, to_goal: str) -> int:
        for key in ((from_goal, to_goal), (from_goal, "*"), ("*", to_goal), ("*", "*")):
            if key in rules:
                return rules[key]
        return 0

    def _check_changeover(self, connection, equipment_id: str, goal: str,
                          start: datetime, end: datetime) -> None:
        rules = self._changeover_rules(connection, equipment_id)
        reservations = self._confirmed_reservations(connection, equipment_id)
        previous = None
        for item in reservations:
            if item[1] <= start and (previous is None or item[1] > previous[1]):
                previous = item
        if previous is not None:
            needed = self._changeover_minutes(rules, previous[2], goal)
            if start - previous[1] < timedelta(minutes=needed):
                raise ConflictError(f"与前一次训练之间需要 {needed} 分钟转换准备")
        following = None
        for item in reservations:
            if item[0] >= end and (following is None or item[0] < following[0]):
                following = item
        if following is not None:
            needed = self._changeover_minutes(rules, goal, following[2])
            if following[0] - end < timedelta(minutes=needed):
                raise ConflictError(f"与后一次训练之间需要 {needed} 分钟转换准备")

    # ---------- 登记能力 ----------

    def register_equipment(self, *, request_id: str, actor_id: str, equipment_id: str, site_id: str,
                           name: str, capability_version: str, capabilities: Any) -> WriteReceipt:
        capabilities = self._normalize_capabilities(capabilities)
        capability_version = self._text(capability_version, "capability_version", 80)
        payload = {"actor_id": actor_id, "equipment_id": equipment_id, "site_id": site_id, "name": name,
                   "capability_version": capability_version, "capabilities": capabilities}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            site = self._site_row(connection, site_id)
            self._check_site_scope(actor, site)
            equipment_id = self._identifier(equipment_id, "equipment_id")
            name = self._text(name, "name")

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO equipment(equipment_id,site_id,name,capability_version,capabilities_json,"
                        "status,version,created_at) VALUES(?,?,?,?,?,'active',1,?)",
                        (equipment_id, site_id, name, capability_version, canonical_json(capabilities), self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("设备编号已经存在") from exc
                append_event(connection, actor_id=actor_id, action="equipment.registered",
                             resource_type="equipment", resource_id=equipment_id,
                             detail={"site_id": site_id, "name": name, "capability_version": capability_version,
                                     "capabilities": capabilities}, occurred_at=self._now())
                return "equipment", equipment_id, {"equipment_id": equipment_id, "version": 1}

            return self._idempotent(connection, request_id=request_id,
                                    action="register_equipment", payload=payload, create=create)

    def change_equipment_capability(self, *, request_id: str, actor_id: str, equipment_id: str,
                                    capability_version: str, capabilities: Any) -> WriteReceipt:
        capabilities = self._normalize_capabilities(capabilities)
        capability_version = self._text(capability_version, "capability_version", 80)
        payload = {"actor_id": actor_id, "equipment_id": equipment_id,
                   "capability_version": capability_version, "capabilities": capabilities}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            equipment = self._equipment_row(connection, equipment_id)
            self._check_site_scope(actor, self._site_row(connection, equipment["site_id"]))

            def create() -> tuple[str, str, dict[str, Any]]:
                current = json.loads(equipment["capabilities_json"])
                if current == capabilities and equipment["capability_version"] == capability_version:
                    return "equipment", equipment_id, {"equipment_id": equipment_id, "version": equipment["version"]}
                new_version = equipment["version"] + 1
                connection.execute(
                    "UPDATE equipment SET capability_version=?, capabilities_json=?, version=? WHERE equipment_id=?",
                    (capability_version, canonical_json(capabilities), new_version, equipment_id),
                )
                append_event(connection, actor_id=actor_id, action="equipment.capability_changed",
                             resource_type="equipment", resource_id=equipment_id,
                             detail={"old_capability_version": equipment["capability_version"],
                                     "capability_version": capability_version, "capabilities": capabilities,
                                     "version": new_version}, occurred_at=self._now())
                return "equipment", equipment_id, {"equipment_id": equipment_id, "version": new_version}

            return self._idempotent(connection, request_id=request_id,
                                    action="change_equipment_capability", payload=payload, create=create)

    def register_attachment(self, *, request_id: str, actor_id: str, attachment_id: str, site_id: str,
                            name: str, capability_version: str, compatible_equipment: Any) -> WriteReceipt:
        compatible_equipment = self._normalize_compatible(compatible_equipment)
        capability_version = self._text(capability_version, "capability_version", 80)
        payload = {"actor_id": actor_id, "attachment_id": attachment_id, "site_id": site_id, "name": name,
                   "capability_version": capability_version, "compatible_equipment": compatible_equipment}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            site = self._site_row(connection, site_id)
            self._check_site_scope(actor, site)
            attachment_id = self._identifier(attachment_id, "attachment_id")
            name = self._text(name, "name")

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO attachments(attachment_id,site_id,name,capability_version,"
                        "compatible_equipment_json,status,version,created_at) VALUES(?,?,?,?,?,'active',1,?)",
                        (attachment_id, site_id, name, capability_version,
                         canonical_json(compatible_equipment), self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("附件编号已经存在") from exc
                append_event(connection, actor_id=actor_id, action="attachment.registered",
                             resource_type="attachment", resource_id=attachment_id,
                             detail={"site_id": site_id, "name": name, "capability_version": capability_version,
                                     "compatible_equipment": compatible_equipment}, occurred_at=self._now())
                return "attachment", attachment_id, {"attachment_id": attachment_id, "version": 1}

            return self._idempotent(connection, request_id=request_id,
                                    action="register_attachment", payload=payload, create=create)

    def change_attachment_capability(self, *, request_id: str, actor_id: str, attachment_id: str,
                                     capability_version: str) -> WriteReceipt:
        capability_version = self._text(capability_version, "capability_version", 80)
        payload = {"actor_id": actor_id, "attachment_id": attachment_id, "capability_version": capability_version}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            attachment = self._attachment_row(connection, attachment_id)
            self._check_site_scope(actor, self._site_row(connection, attachment["site_id"]))

            def create() -> tuple[str, str, dict[str, Any]]:
                if attachment["capability_version"] == capability_version:
                    return "attachment", attachment_id, {"attachment_id": attachment_id, "version": attachment["version"]}
                new_version = attachment["version"] + 1
                connection.execute(
                    "UPDATE attachments SET capability_version=?, version=? WHERE attachment_id=?",
                    (capability_version, new_version, attachment_id),
                )
                append_event(connection, actor_id=actor_id, action="attachment.capability_changed",
                             resource_type="attachment", resource_id=attachment_id,
                             detail={"old_capability_version": attachment["capability_version"],
                                     "capability_version": capability_version, "version": new_version},
                             occurred_at=self._now())
                return "attachment", attachment_id, {"attachment_id": attachment_id, "version": new_version}

            return self._idempotent(connection, request_id=request_id,
                                    action="change_attachment_capability", payload=payload, create=create)

    def register_calibration(self, *, request_id: str, actor_id: str, certificate_id: str,
                             resource_type: str, resource_id: str, issuer: str,
                             issued_at: Any, expires_at: Any) -> WriteReceipt:
        issued = self._parse_time(issued_at, "issued_at")
        expires = self._parse_time(expires_at, "expires_at")
        if expires <= issued:
            raise ValidationError("expires_at 必须晚于 issued_at")
        issuer = self._text(issuer, "issuer", 120)
        payload = {"actor_id": actor_id, "certificate_id": certificate_id, "resource_type": resource_type,
                   "resource_id": resource_id, "issuer": issuer,
                   "issued_at": self._iso(issued), "expires_at": self._iso(expires)}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            resource = self._resource_row(connection, resource_type, resource_id)
            self._check_site_scope(actor, self._site_row(connection, resource["site_id"]))
            certificate_id = self._identifier(certificate_id, "certificate_id")

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO calibration_certificates(certificate_id,resource_type,resource_id,issuer,"
                        "issued_at,expires_at,created_at) VALUES(?,?,?,?,?,?,?)",
                        (certificate_id, resource_type, resource_id, issuer,
                         self._iso(issued), self._iso(expires), self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("校准证书编号已经存在") from exc
                append_event(connection, actor_id=actor_id, action="calibration.registered",
                             resource_type="calibration_certificate", resource_id=certificate_id,
                             detail={"resource_type": resource_type, "resource_id": resource_id, "issuer": issuer,
                                     "issued_at": self._iso(issued), "expires_at": self._iso(expires)},
                             occurred_at=self._now())
                return "calibration_certificate", certificate_id, {"certificate_id": certificate_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="register_calibration", payload=payload, create=create)

    def register_open_window(self, *, request_id: str, actor_id: str, window_id: str,
                             resource_type: str, resource_id: str,
                             start_at: Any, end_at: Any) -> WriteReceipt:
        start = self._parse_time(start_at, "start_at")
        end = self._parse_time(end_at, "end_at")
        if end <= start:
            raise ValidationError("end_at 必须晚于 start_at")
        payload = {"actor_id": actor_id, "window_id": window_id, "resource_type": resource_type,
                   "resource_id": resource_id, "start_at": self._iso(start), "end_at": self._iso(end)}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            resource = self._resource_row(connection, resource_type, resource_id)
            self._check_site_scope(actor, self._site_row(connection, resource["site_id"]))
            window_id = self._identifier(window_id, "window_id")

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO open_windows(window_id,resource_type,resource_id,start_at,end_at,created_at) "
                        "VALUES(?,?,?,?,?,?)",
                        (window_id, resource_type, resource_id, self._iso(start), self._iso(end), self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("开放窗口编号已经存在") from exc
                append_event(connection, actor_id=actor_id, action="open_window.registered",
                             resource_type="open_window", resource_id=window_id,
                             detail={"resource_type": resource_type, "resource_id": resource_id,
                                     "start_at": self._iso(start), "end_at": self._iso(end)},
                             occurred_at=self._now())
                return "open_window", window_id, {"window_id": window_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="register_open_window", payload=payload, create=create)

    def register_changeover_rule(self, *, request_id: str, actor_id: str, rule_id: str, equipment_id: str,
                                 from_goal: str, to_goal: str, minutes: Any) -> WriteReceipt:
        minutes = self._minutes(minutes, "minutes")
        from_goal = self._text(from_goal, "from_goal", 120)
        to_goal = self._text(to_goal, "to_goal", 120)
        payload = {"actor_id": actor_id, "rule_id": rule_id, "equipment_id": equipment_id,
                   "from_goal": from_goal, "to_goal": to_goal, "minutes": minutes}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            equipment = self._equipment_row(connection, equipment_id)
            self._check_site_scope(actor, self._site_row(connection, equipment["site_id"]))
            rule_id = self._identifier(rule_id, "rule_id")

            def create() -> tuple[str, str, dict[str, Any]]:
                existing = connection.execute(
                    "SELECT * FROM changeover_rules WHERE equipment_id=? AND from_goal=? AND to_goal=?",
                    (equipment_id, from_goal, to_goal),
                ).fetchone()
                if existing:
                    connection.execute("UPDATE changeover_rules SET minutes=? WHERE rule_id=?",
                                       (minutes, existing["rule_id"]))
                    append_event(connection, actor_id=actor_id, action="changeover_rule.updated",
                                 resource_type="changeover_rule", resource_id=existing["rule_id"],
                                 detail={"equipment_id": equipment_id, "from_goal": from_goal, "to_goal": to_goal,
                                         "minutes": minutes}, occurred_at=self._now())
                    return "changeover_rule", existing["rule_id"], {"rule_id": existing["rule_id"]}
                try:
                    connection.execute(
                        "INSERT INTO changeover_rules(rule_id,equipment_id,from_goal,to_goal,minutes,created_at) "
                        "VALUES(?,?,?,?,?,?)",
                        (rule_id, equipment_id, from_goal, to_goal, minutes, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("转换规则编号已经存在") from exc
                append_event(connection, actor_id=actor_id, action="changeover_rule.registered",
                             resource_type="changeover_rule", resource_id=rule_id,
                             detail={"equipment_id": equipment_id, "from_goal": from_goal, "to_goal": to_goal,
                                     "minutes": minutes}, occurred_at=self._now())
                return "changeover_rule", rule_id, {"rule_id": rule_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="register_changeover_rule", payload=payload, create=create)

    # ---------- 候选时段搜索 ----------

    def search_slots(self, *, site_id: str, goal: str, duration_minutes: Any,
                     search_start: Any, search_end: Any, required_capabilities: Any = None,
                     attachment_ids: Any = None, max_candidates: Any = 5) -> dict[str, Any]:
        goal = self._text(goal, "goal", 120)
        capabilities = self._normalize_capabilities(required_capabilities) if required_capabilities else []
        duration = self._minutes(duration_minutes, "duration_minutes")
        if duration < 1 or duration > 24 * 60:
            raise ValidationError("duration_minutes 必须在 1 到 1440 之间")
        start = self._parse_time(search_start, "search_start")
        end = self._parse_time(search_end, "search_end")
        if end <= start:
            raise ValidationError("search_end 必须晚于 search_start")
        limit = self._minutes(max_candidates, "max_candidates")
        if limit < 1 or limit > MAX_CANDIDATES:
            raise ValidationError(f"max_candidates 必须在 1 到 {MAX_CANDIDATES} 之间")
        if attachment_ids is None:
            attachment_ids = []
        if not isinstance(attachment_ids, list):
            raise ValidationError("attachment_ids 必须是数组")
        attachment_ids = [self._identifier(str(item), "attachment_id") for item in attachment_ids]
        if len(set(attachment_ids)) != len(attachment_ids):
            raise ValidationError("attachment_ids 中存在重复附件")

        connection = self.database.connection
        self._site_row(connection, site_id)
        attachments = [self._attachment_row(connection, item) for item in attachment_ids]
        for attachment in attachments:
            if attachment["site_id"] != site_id:
                raise ValidationError("附件不属于该场所")

        equipment_rows = connection.execute(
            "SELECT * FROM equipment WHERE site_id=? ORDER BY equipment_id", (site_id,)
        ).fetchall()
        candidates: list[dict[str, Any]] = []
        unavailable: list[dict[str, Any]] = []
        for equipment in equipment_rows:
            equipment_id = equipment["equipment_id"]
            reason = ""
            detail = ""
            intervals: list[tuple[datetime, datetime]] = []
            if equipment["status"] != "active":
                reason, detail = "equipment_deactivated", "设备已停用"
            else:
                owned = set(json.loads(equipment["capabilities_json"]))
                missing = [item for item in capabilities if item not in owned]
                if missing:
                    reason, detail = "capability_mismatch", "缺少能力: " + ",".join(missing)
            if not reason:
                incompatible = [item["attachment_id"] for item in attachments
                                if not self._compatible(item, equipment_id)]
                if incompatible:
                    reason = "attachment_incompatible"
                    detail = "附件与设备不兼容: " + ",".join(incompatible)
                else:
                    stopped = [item["attachment_id"] for item in attachments if item["status"] != "active"]
                    if stopped:
                        reason = "attachment_deactivated"
                        detail = "附件已停用: " + ",".join(stopped)
            if not reason:
                intervals = _intersect([(start, end)],
                                       self._window_intervals(connection, "equipment", equipment_id))
                if not intervals:
                    reason = "no_open_window"
                    detail = f"设备 {equipment_id} 在搜索范围内没有开放窗口"
                else:
                    for attachment in attachments:
                        intervals = _intersect(
                            intervals,
                            self._window_intervals(connection, "attachment", attachment["attachment_id"]))
                        if not intervals:
                            reason = "no_open_window"
                            detail = f"附件 {attachment['attachment_id']} 在搜索范围内没有开放窗口"
                            break
            if not reason:
                intervals = _subtract(
                    intervals,
                    self._busy_intervals(connection, equipment_id,
                                         [item["attachment_id"] for item in attachments]))
                if not intervals:
                    reason, detail = "fully_booked_or_blocked", "时段已被预约或维护封锁占满"
            if not reason:
                rules = self._changeover_rules(connection, equipment_id)
                reservations = self._confirmed_reservations(connection, equipment_id)
                shrunk = []
                for slot_start, slot_end in intervals:
                    previous = None
                    for item in reservations:
                        if item[1] <= slot_start and (previous is None or item[1] > previous[1]):
                            previous = item
                    following = None
                    for item in reservations:
                        if item[0] >= slot_end and (following is None or item[0] < following[0]):
                            following = item
                    effective_start = slot_start
                    effective_end = slot_end
                    if previous is not None:
                        needed = self._changeover_minutes(rules, previous[2], goal)
                        effective_start = max(effective_start, previous[1] + timedelta(minutes=needed))
                    if following is not None:
                        needed = self._changeover_minutes(rules, goal, following[2])
                        effective_end = min(effective_end, following[0] - timedelta(minutes=needed))
                    if effective_start < effective_end:
                        shrunk.append((effective_start, effective_end))
                intervals = shrunk
                if not intervals:
                    reason, detail = "changeover_conflict", "相邻训练之间的转换准备时间不足"
            if not reason:
                resources = [("equipment", equipment_id)] + [
                    ("attachment", item["attachment_id"]) for item in attachments]
                for resource_type, resource_id in resources:
                    intervals = _intersect(intervals,
                                           self._calibration_intervals(connection, resource_type, resource_id))
                    if not intervals:
                        reason = "calibration_not_covering"
                        detail = f"{resource_id} 的校准证书无法覆盖训练时段"
                        break
            if reason:
                unavailable.append({"equipment_id": equipment_id, "reason": reason, "detail": detail})
                continue
            produced = 0
            for slot_start, slot_end in intervals:
                if slot_end - slot_start >= timedelta(minutes=duration):
                    candidates.append(self._candidate(connection, equipment, attachments,
                                                      slot_start, slot_start + timedelta(minutes=duration)))
                    produced += 1
            if not produced:
                unavailable.append({"equipment_id": equipment_id, "reason": "no_interval_long_enough",
                                    "detail": "可用连续时段短于训练时长"})
        candidates.sort(key=lambda item: (item["start_at"], item["equipment_id"]))
        return {"site_id": site_id, "goal": goal, "duration_minutes": duration,
                "candidates": candidates[:limit], "unavailable": unavailable}

    def _candidate(self, connection, equipment, attachments,
                   start: datetime, end: datetime) -> dict[str, Any]:
        return {
            "equipment_id": equipment["equipment_id"],
            "equipment_version": equipment["version"],
            "capability_version": equipment["capability_version"],
            "attachments": [{"attachment_id": item["attachment_id"],
                             "attachment_version": item["version"],
                             "capability_version": item["capability_version"]} for item in attachments],
            "start_at": self._iso(start),
            "end_at": self._iso(end),
            "calibration": {
                "equipment": {"resource_id": equipment["equipment_id"],
                              "certificate_ids": self._calibration_basis(
                                  connection, "equipment", equipment["equipment_id"], start, end)},
                "attachments": [{"resource_id": item["attachment_id"],
                                 "certificate_ids": self._calibration_basis(
                                     connection, "attachment", item["attachment_id"], start, end)}
                                for item in attachments],
            },
        }

    # ---------- 预约确认与取消 ----------

    def confirm_reservation(self, *, request_id: str, actor_id: str, site_id: str, goal: str,
                            equipment_id: str, expected_equipment_version: Any, start_at: Any, end_at: Any,
                            attachments: Any = None, reschedule_entry_id: str | None = None) -> WriteReceipt:
        attachment_requests = self._normalize_attachment_requests(attachments)
        goal = self._text(goal, "goal", 120)
        start = self._parse_time(start_at, "start_at")
        end = self._parse_time(end_at, "end_at")
        if end <= start:
            raise ValidationError("end_at 必须晚于 start_at")
        expected_equipment_version = self._minutes(expected_equipment_version, "expected_equipment_version")
        payload = {"actor_id": actor_id, "site_id": site_id, "goal": goal, "equipment_id": equipment_id,
                   "expected_equipment_version": expected_equipment_version,
                   "attachments": attachment_requests, "start_at": self._iso(start), "end_at": self._iso(end),
                   "reschedule_entry_id": reschedule_entry_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            site = self._site_row(connection, site_id)
            self._check_site_scope(actor, site)
            if start < self._now_moment():
                raise ValidationError("不能预约过去的时间段")

            def create() -> tuple[str, str, dict[str, Any]]:
                equipment = self._equipment_row(connection, equipment_id)
                if equipment["site_id"] != site_id:
                    raise ValidationError("设备不属于该场所")
                if equipment["status"] != "active":
                    raise ConflictError("设备已停用，不能确认预约")
                if equipment["version"] != expected_equipment_version:
                    raise ConflictError("设备版本已变化，请重新获取候选时段")
                attachment_rows = []
                for item in attachment_requests:
                    attachment = self._attachment_row(connection, item["attachment_id"])
                    if attachment["site_id"] != site_id:
                        raise ValidationError("附件不属于该场所")
                    if attachment["status"] != "active":
                        raise ConflictError(f"附件 {item['attachment_id']} 已停用")
                    if attachment["version"] != item["expected_version"]:
                        raise ConflictError(f"附件 {item['attachment_id']} 版本已变化，请重新获取候选时段")
                    if not self._compatible(attachment, equipment_id):
                        raise ConflictError(f"附件 {item['attachment_id']} 与设备 {equipment_id} 不兼容")
                    attachment_rows.append(attachment)
                if not self._windows_cover(connection, "equipment", equipment_id, start, end):
                    raise ConflictError("预约时段超出设备开放窗口")
                for attachment in attachment_rows:
                    if not self._windows_cover(connection, "attachment", attachment["attachment_id"], start, end):
                        raise ConflictError(f"预约时段超出附件 {attachment['attachment_id']} 的开放窗口")
                calibration = {
                    "equipment": {"resource_id": equipment_id,
                                  "certificate_ids": self._basis_or_fail(
                                      connection, "equipment", equipment_id, start, end)},
                    "attachments": [{"resource_id": item["attachment_id"],
                                     "certificate_ids": self._basis_or_fail(
                                         connection, "attachment", item["attachment_id"], start, end)}
                                    for item in attachment_rows],
                }
                if self._overlapping_reservations(connection, "equipment", equipment_id, start, end):
                    raise ConflictError("设备在该时段已被占用")
                for attachment in attachment_rows:
                    if self._overlapping_reservations(connection, "attachment",
                                                      attachment["attachment_id"], start, end):
                        raise ConflictError(f"附件 {attachment['attachment_id']} 在该时段已被占用")
                if self._block_overlap(connection, "equipment", equipment_id, start, end):
                    raise ConflictError("设备在该时段处于维护封锁")
                for attachment in attachment_rows:
                    if self._block_overlap(connection, "attachment", attachment["attachment_id"], start, end):
                        raise ConflictError(f"附件 {attachment['attachment_id']} 在该时段处于维护封锁")
                self._check_changeover(connection, equipment_id, goal, start, end)
                entry = None
                if reschedule_entry_id is not None:
                    entry = connection.execute(
                        "SELECT * FROM reschedule_queue WHERE entry_id=?", (reschedule_entry_id,)
                    ).fetchone()
                    if entry is None:
                        raise NotFoundError("改期条目不存在")
                    if entry["status"] != "pending":
                        raise ConflictError("改期条目已处理")
                    old = connection.execute(
                        "SELECT * FROM reservations WHERE reservation_id=?", (entry["reservation_id"],)
                    ).fetchone()
                    if old is None or old["site_id"] != site_id:
                        raise ValidationError("改期条目不属于该场所")
                reservation_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO reservations(reservation_id,site_id,goal,applicant_id,equipment_id,"
                    "equipment_version,start_at,end_at,status,calibration_json,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (reservation_id, site_id, goal, actor_id, equipment_id, equipment["version"],
                     self._iso(start), self._iso(end), "confirmed", canonical_json(calibration), self._now()),
                )
                for attachment in attachment_rows:
                    connection.execute(
                        "INSERT INTO reservation_attachments(reservation_id,attachment_id,attachment_version) "
                        "VALUES(?,?,?)",
                        (reservation_id, attachment["attachment_id"], attachment["version"]),
                    )
                if entry is not None:
                    connection.execute("UPDATE reschedule_queue SET status='resolved' WHERE entry_id=?",
                                       (entry["entry_id"],))
                    connection.execute("UPDATE reservations SET status='rescheduled' WHERE reservation_id=?",
                                       (entry["reservation_id"],))
                append_event(connection, actor_id=actor_id, action="reservation.confirmed",
                             resource_type="reservation", resource_id=reservation_id,
                             detail={"site_id": site_id, "goal": goal, "equipment_id": equipment_id,
                                     "equipment_version": equipment["version"],
                                     "attachments": [{"attachment_id": item["attachment_id"],
                                                      "attachment_version": item["version"]}
                                                     for item in attachment_rows],
                                     "start_at": self._iso(start), "end_at": self._iso(end),
                                     "calibration": calibration,
                                     "reschedule_entry_id": reschedule_entry_id},
                             occurred_at=self._now())
                return "reservation", reservation_id, {"reservation_id": reservation_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="confirm_reservation", payload=payload, create=create)

    def cancel_reservation(self, *, request_id: str, actor_id: str,
                           reservation_id: str, reason: str) -> WriteReceipt:
        reason = self._text(reason, "reason", 200)
        payload = {"actor_id": actor_id, "reservation_id": reservation_id, "reason": reason}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")

            def create() -> tuple[str, str, dict[str, Any]]:
                row = connection.execute(
                    "SELECT * FROM reservations WHERE reservation_id=?", (reservation_id,)
                ).fetchone()
                if row is None:
                    raise NotFoundError("预约不存在")
                self._check_site_scope(actor, self._site_row(connection, row["site_id"]))
                if row["status"] not in ("confirmed", "reschedule_pending"):
                    raise ConflictError("当前状态不能取消")
                connection.execute("UPDATE reservations SET status='cancelled' WHERE reservation_id=?",
                                   (reservation_id,))
                dropped = 0
                if row["status"] == "reschedule_pending":
                    cursor = connection.execute(
                        "UPDATE reschedule_queue SET status='dropped' WHERE reservation_id=? AND status='pending'",
                        (reservation_id,),
                    )
                    dropped = cursor.rowcount
                append_event(connection, actor_id=actor_id, action="reservation.cancelled",
                             resource_type="reservation", resource_id=reservation_id,
                             detail={"reason": reason, "dropped_entries": dropped}, occurred_at=self._now())
                return "reservation", reservation_id, {"reservation_id": reservation_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="cancel_reservation", payload=payload, create=create)

    # ---------- 维护封锁与紧急停用 ----------

    def publish_maintenance_block(self, *, request_id: str, actor_id: str, resource_type: str,
                                  resource_id: str, start_at: Any, end_at: Any, reason: str) -> WriteReceipt:
        start = self._parse_time(start_at, "start_at")
        end = self._parse_time(end_at, "end_at")
        if end <= start:
            raise ValidationError("end_at 必须晚于 start_at")
        reason = self._text(reason, "reason", 200)
        payload = {"actor_id": actor_id, "resource_type": resource_type, "resource_id": resource_id,
                   "start_at": self._iso(start), "end_at": self._iso(end), "reason": reason}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "maintainer")
            resource = self._resource_row(connection, resource_type, resource_id)
            self._check_site_scope(actor, self._site_row(connection, resource["site_id"]))

            def create() -> tuple[str, str, dict[str, Any]]:
                conflicts = [row["reservation_id"] for row in
                             self._overlapping_reservations(connection, resource_type, resource_id, start, end)]
                if conflicts:
                    raise ConflictError("封锁时段与已确认预约冲突: " + ",".join(conflicts))
                block_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO maintenance_blocks(block_id,site_id,resource_type,resource_id,kind,start_at,"
                    "end_at,reason,created_by,created_at) VALUES(?,?,?,?,'planned',?,?,?,?,?)",
                    (block_id, resource["site_id"], resource_type, resource_id,
                     self._iso(start), self._iso(end), reason, actor_id, self._now()),
                )
                append_event(connection, actor_id=actor_id, action="maintenance.block_published",
                             resource_type="maintenance_block", resource_id=block_id,
                             detail={"resource_type": resource_type, "resource_id": resource_id, "kind": "planned",
                                     "start_at": self._iso(start), "end_at": self._iso(end), "reason": reason},
                             occurred_at=self._now())
                return "maintenance_block", block_id, {"block_id": block_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="publish_maintenance_block", payload=payload, create=create)

    def emergency_deactivate(self, *, request_id: str, actor_id: str, resource_type: str, resource_id: str,
                             end_at: Any, reason: str, start_at: Any = None) -> WriteReceipt:
        parsed_start = self._parse_time(start_at, "start_at") if start_at is not None else None
        end = self._parse_time(end_at, "end_at")
        reason = self._text(reason, "reason", 200)
        payload = {"actor_id": actor_id, "resource_type": resource_type, "resource_id": resource_id,
                   "start_at": self._iso(parsed_start) if parsed_start else None,
                   "end_at": self._iso(end), "reason": reason}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "maintainer")
            resource = self._resource_row(connection, resource_type, resource_id)
            self._check_site_scope(actor, self._site_row(connection, resource["site_id"]))
            if resource["status"] == "deactivated":
                raise ConflictError("资源已处于停用状态")

            def create() -> tuple[str, str, dict[str, Any]]:
                start = parsed_start if parsed_start is not None else self._now_moment()
                if end <= start:
                    raise ValidationError("end_at 必须晚于 start_at")
                table = "equipment" if resource_type == "equipment" else "attachments"
                key = "equipment_id" if resource_type == "equipment" else "attachment_id"
                connection.execute(
                    f"UPDATE {table} SET status='deactivated', version=version+1 WHERE {key}=?", (resource_id,)
                )
                block_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO maintenance_blocks(block_id,site_id,resource_type,resource_id,kind,start_at,"
                    "end_at,reason,created_by,created_at) VALUES(?,?,?,?,'emergency',?,?,?,?,?)",
                    (block_id, resource["site_id"], resource_type, resource_id,
                     self._iso(start), self._iso(end), reason, actor_id, self._now()),
                )
                now = self._now_moment()
                displaced = []
                risks = []
                for row in self._overlapping_reservations(connection, resource_type, resource_id, start, end):
                    row_start = self._parse_time(row["start_at"], "start_at")
                    if row_start >= now:
                        connection.execute(
                            "UPDATE reservations SET status='reschedule_pending' WHERE reservation_id=?",
                            (row["reservation_id"],),
                        )
                        entry_id = uuid.uuid4().hex
                        connection.execute(
                            "INSERT INTO reschedule_queue(entry_id,reservation_id,site_id,block_id,priority_at,"
                            "status,created_at) VALUES(?,?,?,?,?,'pending',?)",
                            (entry_id, row["reservation_id"], row["site_id"], block_id,
                             row["start_at"], self._now()),
                        )
                        displaced.append({"reservation_id": row["reservation_id"], "entry_id": entry_id})
                        append_event(connection, actor_id=actor_id, action="reservation.displaced",
                                     resource_type="reservation", resource_id=row["reservation_id"],
                                     detail={"block_id": block_id, "entry_id": entry_id},
                                     occurred_at=self._now())
                    else:
                        risk_id = uuid.uuid4().hex
                        connection.execute(
                            "INSERT INTO usage_risks(risk_id,reservation_id,site_id,block_id,resource_type,"
                            "resource_id,status,created_at) VALUES(?,?,?,?,?,?,'awaiting_decision',?)",
                            (risk_id, row["reservation_id"], row["site_id"], block_id,
                             resource_type, resource_id, self._now()),
                        )
                        risks.append({"reservation_id": row["reservation_id"], "risk_id": risk_id})
                        append_event(connection, actor_id=actor_id, action="usage.risk_flagged",
                                     resource_type="reservation", resource_id=row["reservation_id"],
                                     detail={"block_id": block_id, "risk_id": risk_id},
                                     occurred_at=self._now())
                append_event(connection, actor_id=actor_id, action="resource.emergency_deactivated",
                             resource_type=resource_type, resource_id=resource_id,
                             detail={"block_id": block_id, "start_at": self._iso(start),
                                     "end_at": self._iso(end), "reason": reason,
                                     "displaced": displaced, "risks": risks},
                             occurred_at=self._now())
                return resource_type, resource_id, {"block_id": block_id,
                                                    "displaced": displaced, "risks": risks}

            return self._idempotent(connection, request_id=request_id,
                                    action="emergency_deactivate", payload=payload, create=create)

    def reactivate_resource(self, *, request_id: str, actor_id: str,
                            resource_type: str, resource_id: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "resource_type": resource_type, "resource_id": resource_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "maintainer")
            resource = self._resource_row(connection, resource_type, resource_id)
            self._check_site_scope(actor, self._site_row(connection, resource["site_id"]))

            def create() -> tuple[str, str, dict[str, Any]]:
                if resource["status"] != "deactivated":
                    raise ConflictError("资源未处于停用状态")
                table = "equipment" if resource_type == "equipment" else "attachments"
                key = "equipment_id" if resource_type == "equipment" else "attachment_id"
                new_version = resource["version"] + 1
                connection.execute(
                    f"UPDATE {table} SET status='active', version=? WHERE {key}=?", (new_version, resource_id)
                )
                append_event(connection, actor_id=actor_id, action="resource.reactivated",
                             resource_type=resource_type, resource_id=resource_id,
                             detail={"version": new_version}, occurred_at=self._now())
                return resource_type, resource_id, {"resource_id": resource_id, "version": new_version}

            return self._idempotent(connection, request_id=request_id,
                                    action="reactivate_resource", payload=payload, create=create)

    # ---------- 人工决定 ----------

    def decide_usage_risk(self, *, request_id: str, actor_id: str, risk_id: str,
                          decision: str, note: str) -> WriteReceipt:
        if decision not in RISK_DECISIONS:
            raise ValidationError("decision 必须是 continue 或 terminate")
        note = self._text(note, "note", 200)
        payload = {"actor_id": actor_id, "risk_id": risk_id, "decision": decision, "note": note}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "maintainer")

            def create() -> tuple[str, str, dict[str, Any]]:
                risk = connection.execute("SELECT * FROM usage_risks WHERE risk_id=?", (risk_id,)).fetchone()
                if risk is None:
                    raise NotFoundError("风险记录不存在")
                self._check_site_scope(actor, self._site_row(connection, risk["site_id"]))
                if risk["status"] != "awaiting_decision":
                    raise ConflictError("该风险已有人工决定")
                new_status = "resolved_continue" if decision == "continue" else "resolved_terminated"
                connection.execute("UPDATE usage_risks SET status=? WHERE risk_id=?", (new_status, risk_id))
                if decision == "terminate":
                    connection.execute("UPDATE reservations SET status='terminated' WHERE reservation_id=?",
                                       (risk["reservation_id"],))
                override_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO manual_overrides(override_id,site_id,actor_id,subject_type,subject_id,decision,"
                    "note,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (override_id, risk["site_id"], actor_id, "usage_risk", risk_id, decision, note, self._now()),
                )
                append_event(connection, actor_id=actor_id, action="usage_risk.decided",
                             resource_type="usage_risk", resource_id=risk_id,
                             detail={"reservation_id": risk["reservation_id"], "decision": decision,
                                     "note": note, "override_id": override_id}, occurred_at=self._now())
                return "usage_risk", risk_id, {"risk_id": risk_id, "decision": decision}

            return self._idempotent(connection, request_id=request_id,
                                    action="decide_usage_risk", payload=payload, create=create)

    def close_reschedule_entry(self, *, request_id: str, actor_id: str,
                               entry_id: str, note: str) -> WriteReceipt:
        note = self._text(note, "note", 200)
        payload = {"actor_id": actor_id, "entry_id": entry_id, "note": note}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "maintainer")

            def create() -> tuple[str, str, dict[str, Any]]:
                entry = connection.execute(
                    "SELECT * FROM reschedule_queue WHERE entry_id=?", (entry_id,)
                ).fetchone()
                if entry is None:
                    raise NotFoundError("改期条目不存在")
                self._check_site_scope(actor, self._site_row(connection, entry["site_id"]))
                if entry["status"] != "pending":
                    raise ConflictError("改期条目已处理")
                connection.execute("UPDATE reschedule_queue SET status='dropped' WHERE entry_id=?", (entry_id,))
                connection.execute(
                    "UPDATE reservations SET status='cancelled' WHERE reservation_id=? AND status='reschedule_pending'",
                    (entry["reservation_id"],),
                )
                override_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO manual_overrides(override_id,site_id,actor_id,subject_type,subject_id,decision,"
                    "note,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (override_id, entry["site_id"], actor_id, "reschedule_entry", entry_id,
                     "drop", note, self._now()),
                )
                append_event(connection, actor_id=actor_id, action="reschedule_entry.closed",
                             resource_type="reschedule_entry", resource_id=entry_id,
                             detail={"reservation_id": entry["reservation_id"], "note": note,
                                     "override_id": override_id}, occurred_at=self._now())
                return "reschedule_entry", entry_id, {"entry_id": entry_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="close_reschedule_entry", payload=payload, create=create)

    # ---------- 查询视图 ----------

    def get_reservation(self, reservation_id: str) -> dict[str, Any]:
        row = self.database.connection.execute(
            "SELECT * FROM reservations WHERE reservation_id=?", (reservation_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError("预约不存在")
        attachments = self.database.connection.execute(
            "SELECT attachment_id, attachment_version FROM reservation_attachments WHERE reservation_id=? "
            "ORDER BY attachment_id", (reservation_id,)
        ).fetchall()
        return {"reservation_id": row["reservation_id"], "site_id": row["site_id"], "goal": row["goal"],
                "applicant_id": row["applicant_id"], "status": row["status"],
                "equipment_id": row["equipment_id"], "equipment_version": row["equipment_version"],
                "attachments": [{"attachment_id": item["attachment_id"],
                                 "attachment_version": item["attachment_version"]} for item in attachments],
                "start_at": row["start_at"], "end_at": row["end_at"],
                "calibration": json.loads(row["calibration_json"]), "created_at": row["created_at"]}

    def list_equipment(self, site_id: str) -> dict[str, Any]:
        self._site_row(self.database.connection, site_id)
        equipment = self.database.connection.execute(
            "SELECT * FROM equipment WHERE site_id=? ORDER BY equipment_id", (site_id,)
        ).fetchall()
        attachments = self.database.connection.execute(
            "SELECT * FROM attachments WHERE site_id=? ORDER BY attachment_id", (site_id,)
        ).fetchall()
        return {
            "site_id": site_id,
            "equipment": [{"equipment_id": row["equipment_id"], "name": row["name"],
                           "status": row["status"], "capability_version": row["capability_version"],
                           "capabilities": json.loads(row["capabilities_json"]),
                           "version": row["version"]} for row in equipment],
            "attachments": [{"attachment_id": row["attachment_id"], "name": row["name"],
                             "status": row["status"], "capability_version": row["capability_version"],
                             "compatible_equipment": json.loads(row["compatible_equipment_json"]),
                             "version": row["version"]} for row in attachments],
        }

    def resource_occupancy(self, site_id: str, start_at: Any, end_at: Any) -> dict[str, Any]:
        start = self._parse_time(start_at, "start_at")
        end = self._parse_time(end_at, "end_at")
        if end <= start:
            raise ValidationError("end_at 必须晚于 start_at")
        connection = self.database.connection
        self._site_row(connection, site_id)

        def overlaps(row) -> bool:
            return (self._parse_time(row["start_at"], "start_at") < end
                    and start < self._parse_time(row["end_at"], "end_at"))

        def blocks_of(resource_type: str, resource_id: str) -> list[dict[str, Any]]:
            rows = connection.execute(
                "SELECT * FROM maintenance_blocks WHERE resource_type=? AND resource_id=? "
                "ORDER BY start_at, block_id", (resource_type, resource_id),
            ).fetchall()
            return [{"block_id": row["block_id"], "kind": row["kind"], "start_at": row["start_at"],
                     "end_at": row["end_at"], "reason": row["reason"]} for row in rows if overlaps(row)]

        def reservation_view(row) -> dict[str, Any]:
            attachment_ids = [item["attachment_id"] for item in connection.execute(
                "SELECT attachment_id FROM reservation_attachments WHERE reservation_id=? ORDER BY attachment_id",
                (row["reservation_id"],),
            ).fetchall()]
            return {"reservation_id": row["reservation_id"], "goal": row["goal"], "status": row["status"],
                    "start_at": row["start_at"], "end_at": row["end_at"], "attachments": attachment_ids}

        equipment_view = []
        equipment_rows = connection.execute(
            "SELECT * FROM equipment WHERE site_id=? ORDER BY equipment_id", (site_id,)
        ).fetchall()
        for equipment in equipment_rows:
            rows = [row for row in connection.execute(
                "SELECT * FROM reservations WHERE equipment_id=? AND status IN ('confirmed','reschedule_pending') "
                "ORDER BY start_at, reservation_id", (equipment["equipment_id"],)
            ).fetchall() if overlaps(row)]
            equipment_view.append({
                "equipment_id": equipment["equipment_id"], "name": equipment["name"],
                "status": equipment["status"], "capability_version": equipment["capability_version"],
                "version": equipment["version"],
                "reservations": [reservation_view(row) for row in rows],
                "blocks": blocks_of("equipment", equipment["equipment_id"]),
            })
        attachment_view = []
        attachment_rows = connection.execute(
            "SELECT * FROM attachments WHERE site_id=? ORDER BY attachment_id", (site_id,)
        ).fetchall()
        for attachment in attachment_rows:
            rows = [row for row in connection.execute(
                "SELECT r.* FROM reservations r JOIN reservation_attachments ra "
                "ON r.reservation_id=ra.reservation_id WHERE ra.attachment_id=? "
                "AND r.status IN ('confirmed','reschedule_pending') ORDER BY r.start_at, r.reservation_id",
                (attachment["attachment_id"],),
            ).fetchall() if overlaps(row)]
            attachment_view.append({
                "attachment_id": attachment["attachment_id"], "name": attachment["name"],
                "status": attachment["status"], "capability_version": attachment["capability_version"],
                "version": attachment["version"],
                "reservations": [reservation_view(row) for row in rows],
                "blocks": blocks_of("attachment", attachment["attachment_id"]),
            })
        return {"site_id": site_id, "start_at": self._iso(start), "end_at": self._iso(end),
                "equipment": equipment_view, "attachments": attachment_view}

    def list_reschedule_queue(self, site_id: str) -> list[dict[str, Any]]:
        self._site_row(self.database.connection, site_id)
        rows = self.database.connection.execute(
            "SELECT q.*, r.goal, r.applicant_id, r.start_at, r.end_at, b.reason AS block_reason "
            "FROM reschedule_queue q JOIN reservations r ON q.reservation_id=r.reservation_id "
            "JOIN maintenance_blocks b ON q.block_id=b.block_id "
            "WHERE q.site_id=? AND q.status='pending' ORDER BY q.priority_at, q.created_at, q.entry_id",
            (site_id,),
        ).fetchall()
        return [{"rank": index + 1, "entry_id": row["entry_id"], "reservation_id": row["reservation_id"],
                 "goal": row["goal"], "applicant_id": row["applicant_id"],
                 "original_start_at": row["start_at"], "original_end_at": row["end_at"],
                 "block_id": row["block_id"], "block_reason": row["block_reason"],
                 "priority_at": row["priority_at"], "created_at": row["created_at"]}
                for index, row in enumerate(rows)]

    def list_usage_risks(self, site_id: str, status: str | None = None) -> list[dict[str, Any]]:
        self._site_row(self.database.connection, site_id)
        query = ("SELECT u.*, r.goal, r.applicant_id, r.start_at, r.end_at, b.reason AS block_reason "
                 "FROM usage_risks u JOIN reservations r ON u.reservation_id=r.reservation_id "
                 "JOIN maintenance_blocks b ON u.block_id=b.block_id WHERE u.site_id=?")
        parameters: list[Any] = [site_id]
        if status:
            query += " AND u.status=?"
            parameters.append(status)
        query += " ORDER BY u.created_at, u.risk_id"
        rows = self.database.connection.execute(query, parameters).fetchall()
        return [{"risk_id": row["risk_id"], "reservation_id": row["reservation_id"], "goal": row["goal"],
                 "applicant_id": row["applicant_id"], "start_at": row["start_at"], "end_at": row["end_at"],
                 "resource_type": row["resource_type"], "resource_id": row["resource_id"],
                 "block_id": row["block_id"], "block_reason": row["block_reason"],
                 "status": row["status"], "created_at": row["created_at"]} for row in rows]

    def list_manual_overrides(self, site_id: str) -> list[dict[str, Any]]:
        self._site_row(self.database.connection, site_id)
        rows = self.database.connection.execute(
            "SELECT * FROM manual_overrides WHERE site_id=? ORDER BY created_at, override_id", (site_id,)
        ).fetchall()
        return [{"override_id": row["override_id"], "actor_id": row["actor_id"],
                 "subject_type": row["subject_type"], "subject_id": row["subject_id"],
                 "decision": row["decision"], "note": row["note"], "created_at": row["created_at"]}
                for row in rows]
