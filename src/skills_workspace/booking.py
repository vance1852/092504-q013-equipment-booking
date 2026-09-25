"""实现实训设备预约、能力核对、维护封锁与改期队列。"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from .audit import append_event, canonical_json, digest
from .errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from .models import WriteReceipt
from .service import DomainService


CANDIDATE_LIMIT = 50
WAIVABLE_VIOLATIONS = frozenset({"maintenance_blockade", "transition_preparation", "calibration_expired"})
WRITE_ROLES = ("admin", "operator")

def parse_dt(value: str, field: str) -> datetime:
    """解析必须带时区的 ISO 时间。"""

    if not isinstance(value, str):
        raise ValidationError(f"{field} 必须是 ISO 时间字符串")
    text = value.strip().replace("Z", "+00:00")
    try:
        result = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValidationError(f"{field} 不是有效的 ISO 时间") from exc
    if result.tzinfo is None:
        raise ValidationError(f"{field} 必须包含时区")
    return result.astimezone(timezone.utc)


def minute_dt(value: str, field: str) -> datetime:
    """解析按分钟对齐的时间。"""

    result = parse_dt(value, field)
    if result.second != 0 or result.microsecond != 0:
        raise ValidationError(f"{field} 必须按分钟对齐")
    return result


def fmt(dt: datetime) -> str:
    """格式化为 UTC 的紧凑 ISO 文本。"""

    return dt.astimezone(timezone.utc).isoformat(timespec="minutes").replace("+00:00", "Z")


def slot_key(dt: datetime) -> str:
    """生成分钟槽的稳定键。"""

    return fmt(dt)


def overlaps(start_a: datetime, end_a: datetime, start_b: datetime, end_b: datetime) -> bool:
    """两个半开区间是否重叠。"""

    return start_a < end_b and start_b < end_a


class BookingService(DomainService):
    """在基础服务之上协调设备预约与维护封锁。"""

    # ------------------------------------------------------------------ 工具

    def _site(self, connection, site_id: str) -> Any:
        row = connection.execute("SELECT * FROM sites WHERE site_id=?", (site_id,)).fetchone()
        if row is None:
            raise NotFoundError("场所不存在")
        return row

    def _equipment(self, connection, equipment_id: str) -> Any:
        row = connection.execute(
            "SELECT * FROM booking_equipment WHERE equipment_id=?", (equipment_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError("设备不存在")
        return row

    def _accessory(self, connection, accessory_id: str) -> Any:
        row = connection.execute(
            "SELECT * FROM booking_accessories WHERE accessory_id=?", (accessory_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError("附件不存在")
        return row

    def _capabilities(self, value: Any, field: str) -> list[str]:
        if not isinstance(value, list) or not value:
            raise ValidationError(f"{field} 必须是非空数组")
        result: list[str] = []
        for item in value:
            item = str(item).strip()
            if not item or len(item) > 120:
                raise ValidationError(f"{field} 中存在无效能力项")
            if item not in result:
                result.append(item)
        return result

    def _setup(self, value: Any) -> str:
        value = str(value or "").strip()
        if not value or len(value) > 80:
            raise ValidationError("setup 不能为空且不能超过 80 个字符")
        return value

    @staticmethod
    def _intervals_overlap(intervals: Iterable[tuple[datetime, datetime]],
                          start: datetime, end: datetime) -> bool:
        return any(overlaps(start, end, other_start, other_end)
                   for other_start, other_end in intervals)

    def _reservation_intervals(self, connection, *, equipment_id: str | None = None,
                               statuses: tuple[str, ...] = ("confirmed",)) -> list[dict[str, Any]]:
        rows = connection.execute(
            "SELECT * FROM booking_reservations WHERE status IN (%s)"
            % ",".join("?" for _ in statuses),
            statuses,
        ).fetchall()
        items = []
        for row in rows:
            if equipment_id is not None and row["equipment_id"] != equipment_id:
                continue
            items.append({
                "reservation_id": row["reservation_id"],
                "start": parse_dt(row["starts_at"], "starts_at"),
                "end": parse_dt(row["ends_at"], "ends_at"),
                "setup": row["setup"],
                "equipment_id": row["equipment_id"],
                "accessory_ids": self._loads(row["accessory_ids_json"]),
            })
        return items

    @staticmethod
    def _loads(value: str) -> list[str]:
        return json.loads(value)

    def _replayed_receipt(self, connection, request_id: str, action: str,
                          payload: dict[str, Any]) -> WriteReceipt | None:
        """状态迁移类接口在状态校验前先识别安全重放。"""

        request_id = self._identifier(request_id, "request_id")
        row = connection.execute(
            "SELECT * FROM request_receipts WHERE request_id=?", (request_id,),
        ).fetchone()
        if row is None:
            return None
        if row["action"] != action or row["payload_hash"] != digest(payload):
            raise ConflictError("request_id 已被不同内容使用")
        return WriteReceipt(request_id, row["resource_type"], row["resource_id"], True)

    def _blockade_intervals(self, connection, equipment_id: str,
                            horizon_end: datetime) -> list[dict[str, Any]]:
        rows = connection.execute(
            "SELECT * FROM booking_blockades WHERE equipment_id=? AND status='active'",
            (equipment_id,),
        ).fetchall()
        items = []
        for row in rows:
            end = parse_dt(row["ends_at"], "ends_at") if row["ends_at"] else horizon_end
            items.append({"blockade_id": row["blockade_id"], "kind": row["kind"],
                          "start": parse_dt(row["starts_at"], "starts_at"),
                          "end": max(end, parse_dt(row["starts_at"], "starts_at"))})
        return items

    def _certificate(self, connection, target_type: str, target_id: str,
                     as_of: datetime) -> Any | None:
        row = connection.execute(
            "SELECT * FROM booking_certificates WHERE target_type=? AND target_id=? "
            "AND calibrated_at<=? AND expires_at>=? ORDER BY expires_at DESC LIMIT 1",
            (target_type, target_id, fmt(as_of), fmt(as_of)),
        ).fetchone()
        return row

    def _windows(self, connection, equipment_id: str) -> list[tuple[datetime, datetime]]:
        rows = connection.execute(
            "SELECT * FROM booking_open_windows WHERE equipment_id=? ORDER BY starts_at",
            (equipment_id,),
        ).fetchall()
        return [(parse_dt(r["starts_at"], "starts_at"), parse_dt(r["ends_at"], "ends_at"))
                for r in rows]

    def _transition_minutes(self, connection, equipment_id: str,
                            from_setup: str, to_setup: str) -> int:
        row = connection.execute(
            "SELECT minutes FROM booking_transition_rules WHERE equipment_id=? "
            "AND from_setup=? AND to_setup=?", (equipment_id, from_setup, to_setup),
        ).fetchone()
        return row["minutes"] if row else 0

    def _violations(self, connection, equipment_id: str, accessory_ids: list[str],
                    start: datetime, end: datetime, setup: str) -> list[str]:
        """返回该时段仍然存在的违规代码（占用冲突除外）。"""

        found: list[str] = []
        blockades = self._blockade_intervals(connection, equipment_id, end)
        if self._intervals_overlap(((b["start"], b["end"]) for b in blockades), start, end):
            if any(b["kind"] == "emergency"
                   for b in blockades
                   if overlaps(start, end, b["start"], b["end"])):
                found.append("emergency_blockade")
            else:
                found.append("maintenance_blockade")
        if self._certificate(connection, "equipment", equipment_id, end) is None:
            found.append("calibration_expired")
        windows = self._windows(connection, equipment_id)
        if not any(start >= w_start and end <= w_end for w_start, w_end in windows):
            found.append("outside_open_window")
        for accessory_id in accessory_ids:
            if self._certificate(connection, "accessory", accessory_id, end) is None:
                found.append("calibration_expired")
                break
        reservations = self._reservation_intervals(connection, equipment_id=equipment_id)
        for item in reservations:
            gap_before = (start - item["end"]).total_seconds() / 60
            gap_after = (item["start"] - end).total_seconds() / 60
            if item["end"] <= start:
                needed = self._transition_minutes(connection, equipment_id, item["setup"], setup)
                if 0 <= gap_before < needed:
                    found.append("transition_preparation")
                    break
            elif item["start"] >= end:
                needed = self._transition_minutes(connection, equipment_id, setup, item["setup"])
                if 0 <= gap_after < needed:
                    found.append("transition_preparation")
                    break
        return found

    # ---------------------------------------------------------- 资源登记

    def register_equipment(self, *, request_id: str, actor_id: str, site_id: str,
                           equipment_id: str, name: str, kind: str,
                           capabilities: list[str], capability_version: int = 1) -> WriteReceipt:
        payload = {"actor_id": actor_id, "site_id": site_id, "equipment_id": equipment_id,
                   "name": name, "kind": kind, "capabilities": capabilities,
                   "capability_version": capability_version}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, *WRITE_ROLES)
            self._site(connection, site_id)
            equipment_id = self._identifier(equipment_id, "equipment_id")
            name = self._text(name, "name")
            kind = self._text(kind, "kind", 80)
            capabilities = self._capabilities(capabilities, "capabilities")
            if not isinstance(capability_version, int) or capability_version < 1:
                raise ValidationError("capability_version 必须是不小于 1 的整数")

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO booking_equipment(equipment_id,site_id,name,kind,capabilities_json,"
                        "capability_version,created_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
                        (equipment_id, site_id, name, kind, canonical_json(capabilities),
                         capability_version, actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("设备编号已经存在") from exc
                append_event(connection, actor_id=actor_id, action="booking_equipment.registered",
                             resource_type="booking_equipment", resource_id=equipment_id,
                             detail={"site_id": site_id, "name": name, "kind": kind,
                                     "capabilities": capabilities,
                                     "capability_version": capability_version},
                             occurred_at=self._now())
                return "booking_equipment", equipment_id, {"equipment_id": equipment_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="register_booking_equipment", payload=payload, create=create)

    def register_accessory(self, *, request_id: str, actor_id: str, site_id: str,
                           accessory_id: str, name: str, capabilities: list[str],
                           capability_version: int = 1,
                           compatible_equipment: list[str] | None = None) -> WriteReceipt:
        compatible_equipment = compatible_equipment or []
        payload = {"actor_id": actor_id, "site_id": site_id, "accessory_id": accessory_id,
                   "name": name, "capabilities": capabilities,
                   "capability_version": capability_version,
                   "compatible_equipment": compatible_equipment}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, *WRITE_ROLES)
            self._site(connection, site_id)
            accessory_id = self._identifier(accessory_id, "accessory_id")
            name = self._text(name, "name")
            capabilities = self._capabilities(capabilities, "capabilities")
            if not isinstance(capability_version, int) or capability_version < 1:
                raise ValidationError("capability_version 必须是不小于 1 的整数")
            if not isinstance(compatible_equipment, list):
                raise ValidationError("compatible_equipment 必须是数组")
            for equipment_id in compatible_equipment:
                self._equipment(connection, str(equipment_id))

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO booking_accessories(accessory_id,site_id,name,capabilities_json,"
                        "capability_version,created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                        (accessory_id, site_id, name, canonical_json(capabilities),
                         capability_version, actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("附件编号已经存在") from exc
                for equipment_id in compatible_equipment:
                    connection.execute(
                        "INSERT OR IGNORE INTO booking_equipment_accessories(equipment_id,accessory_id) "
                        "VALUES(?,?)", (equipment_id, accessory_id),
                    )
                append_event(connection, actor_id=actor_id, action="booking_accessory.registered",
                             resource_type="booking_accessory", resource_id=accessory_id,
                             detail={"site_id": site_id, "name": name, "capabilities": capabilities,
                                     "capability_version": capability_version,
                                     "compatible_equipment": compatible_equipment},
                             occurred_at=self._now())
                return "booking_accessory", accessory_id, {"accessory_id": accessory_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="register_booking_accessory", payload=payload, create=create)

    def register_compatibility(self, *, request_id: str, actor_id: str,
                               equipment_id: str, accessory_id: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "equipment_id": equipment_id, "accessory_id": accessory_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, *WRITE_ROLES)
            equipment = self._equipment(connection, equipment_id)
            accessory = self._accessory(connection, accessory_id)
            if equipment["site_id"] != accessory["site_id"]:
                raise ValidationError("设备与附件不属于同一场所")

            def create() -> tuple[str, str, dict[str, Any]]:
                connection.execute(
                    "INSERT OR IGNORE INTO booking_equipment_accessories(equipment_id,accessory_id) "
                    "VALUES(?,?)", (equipment_id, accessory_id),
                )
                append_event(connection, actor_id=actor_id,
                             action="booking_compatibility.registered",
                             resource_type="booking_equipment_accessory",
                             resource_id=f"{equipment_id}:{accessory_id}",
                             detail={"equipment_id": equipment_id, "accessory_id": accessory_id},
                             occurred_at=self._now())
                return "booking_equipment_accessory", f"{equipment_id}:{accessory_id}", \
                    {"equipment_id": equipment_id, "accessory_id": accessory_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="register_booking_compatibility",
                                    payload=payload, create=create)

    def register_certificate(self, *, request_id: str, actor_id: str, target_type: str,
                             target_id: str, certificate_no: str, basis: str,
                             calibrated_at: str, expires_at: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "target_type": target_type, "target_id": target_id,
                   "certificate_no": certificate_no, "basis": basis,
                   "calibrated_at": calibrated_at, "expires_at": expires_at}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, *WRITE_ROLES)
            if target_type not in ("equipment", "accessory"):
                raise ValidationError("target_type 只能是 equipment 或 accessory")
            if target_type == "equipment":
                self._equipment(connection, target_id)
            else:
                self._accessory(connection, target_id)
            certificate_no = self._text(certificate_no, "certificate_no")
            basis = self._text(basis, "basis", 500)
            start = minute_dt(calibrated_at, "calibrated_at")
            end = minute_dt(expires_at, "expires_at")
            if end <= start:
                raise ValidationError("expires_at 必须晚于 calibrated_at")
            certificate_id = uuid.uuid4().hex

            def create() -> tuple[str, str, dict[str, Any]]:
                connection.execute(
                    "INSERT INTO booking_certificates(certificate_id,target_type,target_id,"
                    "certificate_no,basis,calibrated_at,expires_at,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?)",
                    (certificate_id, target_type, target_id, certificate_no, basis,
                     fmt(start), fmt(end), actor_id, self._now()),
                )
                append_event(connection, actor_id=actor_id,
                             action="booking_certificate.registered",
                             resource_type="booking_certificate", resource_id=certificate_id,
                             detail={"target_type": target_type, "target_id": target_id,
                                     "certificate_no": certificate_no, "basis": basis,
                                     "expires_at": fmt(end)},
                             occurred_at=self._now())
                return "booking_certificate", certificate_id, {"certificate_id": certificate_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="register_booking_certificate",
                                    payload=payload, create=create)

    def register_open_window(self, *, request_id: str, actor_id: str, equipment_id: str,
                             starts_at: str, ends_at: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "equipment_id": equipment_id,
                   "starts_at": starts_at, "ends_at": ends_at}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, *WRITE_ROLES)
            self._equipment(connection, equipment_id)
            start = minute_dt(starts_at, "starts_at")
            end = minute_dt(ends_at, "ends_at")
            if end <= start:
                raise ValidationError("ends_at 必须晚于 starts_at")
            window_id = uuid.uuid4().hex

            def create() -> tuple[str, str, dict[str, Any]]:
                connection.execute(
                    "INSERT INTO booking_open_windows(window_id,equipment_id,starts_at,ends_at,"
                    "created_by,created_at) VALUES(?,?,?,?,?,?)",
                    (window_id, equipment_id, fmt(start), fmt(end), actor_id, self._now()),
                )
                append_event(connection, actor_id=actor_id,
                             action="booking_open_window.registered",
                             resource_type="booking_open_window", resource_id=window_id,
                             detail={"equipment_id": equipment_id, "starts_at": fmt(start),
                                     "ends_at": fmt(end)},
                             occurred_at=self._now())
                return "booking_open_window", window_id, {"window_id": window_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="register_booking_open_window",
                                    payload=payload, create=create)

    def register_transition_rule(self, *, request_id: str, actor_id: str, site_id: str,
                                 equipment_id: str, from_setup: str, to_setup: str,
                                 minutes: int) -> WriteReceipt:
        payload = {"actor_id": actor_id, "site_id": site_id, "equipment_id": equipment_id,
                   "from_setup": from_setup, "to_setup": to_setup, "minutes": minutes}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, *WRITE_ROLES)
            self._site(connection, site_id)
            equipment = self._equipment(connection, equipment_id)
            if equipment["site_id"] != site_id:
                raise ValidationError("设备不属于该场所")
            from_setup = self._setup(from_setup)
            to_setup = self._setup(to_setup)
            if not isinstance(minutes, int) or minutes < 0 or minutes > 24 * 60:
                raise ValidationError("minutes 必须是 0 到 1440 之间的整数")

            def create() -> tuple[str, str, dict[str, Any]]:
                connection.execute(
                    "INSERT INTO booking_transition_rules(rule_id,site_id,equipment_id,from_setup,"
                    "to_setup,minutes,created_by,created_at) VALUES(?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(equipment_id,from_setup,to_setup) DO UPDATE SET minutes=excluded.minutes",
                    (uuid.uuid4().hex, site_id, equipment_id, from_setup, to_setup, minutes,
                     actor_id, self._now()),
                )
                append_event(connection, actor_id=actor_id,
                             action="booking_transition_rule.registered",
                             resource_type="booking_transition_rule",
                             resource_id=f"{equipment_id}:{from_setup}:{to_setup}",
                             detail={"equipment_id": equipment_id, "from_setup": from_setup,
                                     "to_setup": to_setup, "minutes": minutes},
                             occurred_at=self._now())
                return "booking_transition_rule", f"{equipment_id}:{from_setup}:{to_setup}", \
                    {"equipment_id": equipment_id, "from_setup": from_setup,
                     "to_setup": to_setup, "minutes": minutes}

            return self._idempotent(connection, request_id=request_id,
                                    action="register_booking_transition_rule",
                                    payload=payload, create=create)

    # -------------------------------------------------------------- 候选查询

    def _compatible_accessories(self, connection, equipment_id: str) -> list[Any]:
        rows = connection.execute(
            "SELECT a.* FROM booking_accessories a JOIN booking_equipment_accessories l "
            "ON a.accessory_id=l.accessory_id WHERE l.equipment_id=? "
            "ORDER BY a.accessory_id", (equipment_id,),
        ).fetchall()
        return rows

    def _choose_accessories(self, connection, options: list[list[Any]], start: datetime,
                            end: datetime, busy: dict[str, list[dict[str, Any]]],
                            *, allow_expired: bool = False
                            ) -> tuple[list[str], dict[str, int], dict[str, str | None]] | None:
        chosen: list[str] = []
        versions: dict[str, int] = {}
        certs: dict[str, str | None] = {}

        def backtrack(index: int) -> bool:
            if index == len(options):
                return True
            for row in options[index]:
                accessory_id = row["accessory_id"]
                if accessory_id in chosen:
                    continue
                intervals = busy.get(accessory_id, [])
                if self._intervals_overlap(((i["start"], i["end"]) for i in intervals), start, end):
                    continue
                certificate = self._certificate(connection, "accessory", accessory_id, end)
                if certificate is None and not allow_expired:
                    continue
                chosen.append(accessory_id)
                versions[accessory_id] = row["capability_version"]
                certs[accessory_id] = certificate["certificate_id"] if certificate else None
                if backtrack(index + 1):
                    return True
                chosen.pop()
                versions.pop(accessory_id)
                certs.pop(accessory_id)
            return False

        if backtrack(0):
            return chosen, versions, certs
        return None

    def submit_inquiry(self, *, actor_id: str, site_id: str, objective: str, setup: str,
                       duration_minutes: int, earliest_start: str, latest_end: str,
                       capabilities: list[str],
                       accessory_requirements: list[dict[str, str]] | None = None,
                       equipment_ids: list[str] | None = None,
                       priority_tier: int = 2) -> dict[str, Any]:
        """根据训练目标生成候选时段和无法满足的原因。"""

        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, *WRITE_ROLES)
            site = self._site(connection, site_id)
            if actor.organization_id != site["organization_id"] and actor.role != "admin":
                raise PermissionDenied("不能查询其他组织场所的设备")
            objective = self._text(objective, "objective", 500)
            setup = self._setup(setup)
            if not isinstance(duration_minutes, int) or not 1 <= duration_minutes <= 24 * 60:
                raise ValidationError("duration_minutes 必须是 1 到 1440 之间的整数")
            horizon_start = minute_dt(earliest_start, "earliest_start")
            horizon_end = minute_dt(latest_end, "latest_end")
            if horizon_end <= horizon_start:
                raise ValidationError("latest_end 必须晚于 earliest_start")
            if (horizon_end - horizon_start) > timedelta(days=60):
                raise ValidationError("查询范围不能超过 60 天")
            capabilities = self._capabilities(capabilities, "capabilities")
            requirements: list[str] = []
            if accessory_requirements is None:
                accessory_requirements = []
            if not isinstance(accessory_requirements, list):
                raise ValidationError("accessory_requirements 必须是数组")
            for requirement in accessory_requirements:
                if not isinstance(requirement, dict) or "capability" not in requirement:
                    raise ValidationError("附件需求必须包含 capability")
                requirements.append(self._text(requirement["capability"], "accessory capability", 120))
            if not isinstance(priority_tier, int) or not 1 <= priority_tier <= 3:
                raise ValidationError("priority_tier 必须是 1 到 3 之间的整数")
            if equipment_ids is not None and not isinstance(equipment_ids, list):
                raise ValidationError("equipment_ids 必须是数组")

            duration = timedelta(minutes=duration_minutes)
            equipment_rows = connection.execute(
                "SELECT * FROM booking_equipment WHERE site_id=? ORDER BY equipment_id",
                (site_id,),
            ).fetchall()
            if equipment_ids is not None:
                wanted = {str(item) for item in equipment_ids}
                equipment_rows = [row for row in equipment_rows if row["equipment_id"] in wanted]
                if len(wanted) != len(equipment_rows):
                    raise NotFoundError("指定的设备不存在或不属于该场所")

            # 全场所的已确认占用，供附件冲突判断复用。
            all_reservations = self._reservation_intervals(connection)
            accessory_busy: dict[str, list[dict[str, Any]]] = {}
            for item in all_reservations:
                for accessory_id in item["accessory_ids"]:
                    accessory_busy.setdefault(accessory_id, []).append(item)

            candidates: list[dict[str, Any]] = []
            unavailable: list[dict[str, Any]] = []

            for equipment in equipment_rows:
                equipment_id = equipment["equipment_id"]
                equipment_caps = self._loads(equipment["capabilities_json"])
                missing = [cap for cap in capabilities if cap not in equipment_caps]
                if missing:
                    unavailable.append({"equipment_id": equipment_id,
                                        "reasons": ["missing_capability"],
                                        "details": {"missing_capabilities": missing}})
                    continue
                accessories = self._compatible_accessories(connection, equipment_id)
                options: list[list[Any]] = []
                accessory_missing: list[str] = []
                for capability in requirements:
                    matched = [row for row in accessories
                               if capability in self._loads(row["capabilities_json"])]
                    if not matched:
                        accessory_missing.append(capability)
                    options.append(matched)
                if accessory_missing:
                    unavailable.append({"equipment_id": equipment_id,
                                        "reasons": ["accessory_missing_capability"],
                                        "details": {"missing_capabilities": accessory_missing}})
                    continue
                if self._certificate(connection, "equipment", equipment_id, horizon_start) is None \
                        and self._certificate(connection, "equipment", equipment_id, horizon_end) is None \
                        and connection.execute(
                            "SELECT 1 FROM booking_certificates WHERE target_type='equipment' "
                            "AND target_id=? AND expires_at>? AND calibrated_at<? LIMIT 1",
                            (equipment_id, fmt(horizon_start), fmt(horizon_end))).fetchone() is None:
                    # 证书有效期与查询范围完全不相交，扫描不会产生候选。
                    unavailable.append({"equipment_id": equipment_id,
                                        "reasons": ["calibration_expired"],
                                        "details": {"resource": "equipment"}})
                    continue

                windows = [w for w in self._windows(connection, equipment_id)
                           if overlaps(horizon_start, horizon_end, w[0], w[1])]
                if not windows:
                    unavailable.append({"equipment_id": equipment_id,
                                        "reasons": ["no_open_window"], "details": {}})
                    continue

                blockades = self._blockade_intervals(connection, equipment_id, horizon_end)
                reservations = self._reservation_intervals(connection, equipment_id=equipment_id)
                reasons: set[str] = set()
                found_for_equipment = False
                for window_start, window_end in windows:
                    clip_start = max(window_start, horizon_start)
                    clip_end = min(window_end, horizon_end)
                    cursor = clip_start
                    while cursor + duration <= clip_end:
                        end = cursor + duration
                        violations: set[str] = set()
                        if self._certificate(connection, "equipment", equipment_id, end) is None:
                            violations.add("calibration_expired")
                        if self._intervals_overlap(
                                ((b["start"], b["end"]) for b in blockades), cursor, end):
                            if any(b["kind"] == "emergency"
                                   and overlaps(cursor, end, b["start"], b["end"])
                                   for b in blockades):
                                violations.add("emergency_blockade")
                            else:
                                violations.add("maintenance_blockade")
                        equipment_busy = any(
                            overlaps(cursor, end, item["start"], item["end"])
                            for item in reservations
                        )
                        if equipment_busy:
                            violations.add("busy_reservation")
                        for item in reservations:
                            if item["end"] <= cursor:
                                needed = self._transition_minutes(
                                    connection, equipment_id, item["setup"], setup)
                                if 0 <= (cursor - item["end"]).total_seconds() / 60 < needed:
                                    violations.add("transition_preparation")
                                    break
                            elif item["start"] >= end:
                                needed = self._transition_minutes(
                                    connection, equipment_id, setup, item["setup"])
                                if 0 <= (item["start"] - end).total_seconds() / 60 < needed:
                                    violations.add("transition_preparation")
                                    break
                        # 优先选出证书齐全的附件组合；不存在时再接受需豁免校准的组合。
                        chosen = self._choose_accessories(
                            connection, options, cursor, end, accessory_busy)
                        if chosen is None:
                            chosen = self._choose_accessories(
                                connection, options, cursor, end, accessory_busy,
                                allow_expired=True)
                            if chosen is not None:
                                violations.add("calibration_expired")
                        # 先排除任何附件被占用的组合（附件占用不可豁免）。
                        accessory_busy_hard = chosen is not None and any(
                            self._intervals_overlap(
                                ((i["start"], i["end"])
                                 for i in accessory_busy.get(accessory_id, [])),
                                cursor, end)
                            for accessory_id in chosen[0])
                        if accessory_busy_hard:
                            violations.add("busy_reservation")
                        hard_violations = violations - WAIVABLE_VIOLATIONS
                        if not hard_violations and chosen is not None:
                            chosen_ids, chosen_versions, chosen_certs = chosen
                            certificate = self._certificate(
                                connection, "equipment", equipment_id, end)
                            candidates.append({
                                "equipment_id": equipment_id,
                                "accessory_ids": chosen_ids,
                                "setup": setup,
                                "starts_at": fmt(cursor),
                                "ends_at": fmt(end),
                                "duration_minutes": duration_minutes,
                                "equipment_version": equipment["capability_version"],
                                "accessory_versions": chosen_versions,
                                "violations": sorted(violations),
                                "waivers_required": sorted(violations & WAIVABLE_VIOLATIONS),
                                "certificates": {
                                    "equipment": certificate["certificate_id"] if certificate else None,
                                    "accessories": chosen_certs,
                                },
                            })
                            found_for_equipment = True
                            if len(candidates) >= CANDIDATE_LIMIT:
                                break
                        reasons.update(violations)
                        cursor += timedelta(minutes=1)
                    if len(candidates) >= CANDIDATE_LIMIT:
                        break
                if not found_for_equipment:
                    unavailable.append({"equipment_id": equipment_id,
                                        "reasons": sorted(reasons or {"no_open_window"}),
                                        "details": {}})
                if len(candidates) >= CANDIDATE_LIMIT:
                    break

            candidates.sort(key=lambda item: (len(item["violations"]),
                                              item["starts_at"], item["equipment_id"]))
            result = {"candidates": candidates, "unavailable": unavailable,
                      "generated_at": self._now()}
            inquiry_id = uuid.uuid4().hex
            connection.execute(
                "INSERT INTO booking_inquiries(inquiry_id,site_id,applicant_actor_id,objective,setup,"
                "duration_minutes,earliest_start,latest_end,priority_tier,capabilities_json,"
                "accessory_requirements_json,result_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (inquiry_id, site_id, actor_id, objective, setup, duration_minutes,
                 fmt(horizon_start), fmt(horizon_end), priority_tier,
                 canonical_json(capabilities), canonical_json(requirements),
                 canonical_json(result), self._now()),
            )
            append_event(connection, actor_id=actor_id, action="booking_inquiry.submitted",
                         resource_type="booking_inquiry", resource_id=inquiry_id,
                         detail={"site_id": site_id, "setup": setup,
                                 "duration_minutes": duration_minutes,
                                 "candidate_count": len(candidates),
                                 "unavailable_count": len(unavailable)},
                         occurred_at=self._now())
            return {"inquiry_id": inquiry_id, **result}

    def get_inquiry(self, inquiry_id: str) -> dict[str, Any]:
        row = self.database.connection.execute(
            "SELECT * FROM booking_inquiries WHERE inquiry_id=?", (inquiry_id,),
        ).fetchone()
        if row is None:
            raise NotFoundError("预约咨询不存在")
        return {
            "inquiry_id": row["inquiry_id"], "site_id": row["site_id"],
            "applicant_actor_id": row["applicant_actor_id"], "objective": row["objective"],
            "setup": row["setup"], "duration_minutes": row["duration_minutes"],
            "earliest_start": row["earliest_start"], "latest_end": row["latest_end"],
            "priority_tier": row["priority_tier"],
            "capabilities": json.loads(row["capabilities_json"]),
            "accessory_requirements": json.loads(row["accessory_requirements_json"]),
            "result": json.loads(row["result_json"]), "created_at": row["created_at"],
        }

    # -------------------------------------------------------------- 确认预约

    def _insert_slots(self, connection, reservation_id: str, start: datetime, end: datetime,
                      equipment_id: str, accessory_ids: list[str]) -> None:
        cursor = start
        while cursor < end:
            key = slot_key(cursor)
            for resource_type, resource_id in (
                (("equipment", equipment_id),
                 *[("accessory", accessory_id) for accessory_id in accessory_ids])):
                try:
                    connection.execute(
                        "INSERT INTO booking_reservation_slots(resource_type,resource_id,slot,"
                        "reservation_id) VALUES(?,?,?,?)",
                        (resource_type, resource_id, key, reservation_id),
                    )
                except Exception as exc:
                    raise ConflictError(f"资源 {resource_type}:{resource_id} 在 {key} 已被占用") from exc
            cursor += timedelta(minutes=1)

    def confirm_reservation(self, *, request_id: str, actor_id: str, inquiry_id: str,
                            equipment_id: str, accessory_ids: list[str], starts_at: str,
                            ends_at: str, expected_equipment_version: int | None = None,
                            expected_accessory_versions: dict[str, int] | None = None,
                            waivers: list[dict[str, str]] | None = None) -> WriteReceipt:
        """原子占用整组资源；任一项冲突则全部失败。"""

        payload = {"actor_id": actor_id, "inquiry_id": inquiry_id,
                   "equipment_id": equipment_id, "accessory_ids": accessory_ids,
                   "starts_at": starts_at, "ends_at": ends_at,
                   "expected_equipment_version": expected_equipment_version,
                   "expected_accessory_versions": expected_accessory_versions,
                   "waivers": waivers or []}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, *WRITE_ROLES)
            inquiry = connection.execute(
                "SELECT * FROM booking_inquiries WHERE inquiry_id=?", (inquiry_id,),
            ).fetchone()
            if inquiry is None:
                raise NotFoundError("预约咨询不存在")
            if inquiry["applicant_actor_id"] != actor_id and actor.role != "admin":
                raise PermissionDenied("只能确认自己提交的预约咨询")
            equipment = self._equipment(connection, equipment_id)
            if equipment["site_id"] != inquiry["site_id"]:
                raise ValidationError("设备不属于咨询所在场所")
            if not isinstance(accessory_ids, list):
                raise ValidationError("accessory_ids 必须是数组")
            accessory_ids = sorted({str(item) for item in accessory_ids})
            start = minute_dt(starts_at, "starts_at")
            end = minute_dt(ends_at, "ends_at")
            duration_minutes = int((end - start).total_seconds() / 60)
            if duration_minutes != inquiry["duration_minutes"]:
                raise ValidationError("确认时长必须与咨询时长一致")
            setup = inquiry["setup"]

            # 候选必须来自咨询结果，防止确认未被评估的组合。
            candidates = json.loads(inquiry["result_json"])["candidates"]
            matched = next((candidate for candidate in candidates
                            if candidate["equipment_id"] == equipment_id
                            and sorted(candidate["accessory_ids"]) == accessory_ids
                            and candidate["starts_at"] == fmt(start)
                            and candidate["ends_at"] == fmt(end)), None)
            if matched is None:
                raise ConflictError("确认内容不在咨询给出的候选时段内")

            # 版本核对：候选快照与登记现状必须一致，也可与客户端期望值核对。
            if expected_equipment_version is not None \
                    and expected_equipment_version != equipment["capability_version"]:
                raise ConflictError("设备能力版本与预期不一致")
            if equipment["capability_version"] != matched["equipment_version"]:
                raise ConflictError("设备能力版本在咨询后发生变化，请重新获取候选")
            accessory_rows = {accessory_id: self._accessory(connection, accessory_id)
                              for accessory_id in accessory_ids}
            for accessory_id, row in accessory_rows.items():
                link = connection.execute(
                    "SELECT 1 FROM booking_equipment_accessories WHERE equipment_id=? AND accessory_id=?",
                    (equipment_id, accessory_id),
                ).fetchone()
                if link is None:
                    raise ValidationError(f"附件 {accessory_id} 不能与该设备组合")
                if row["capability_version"] != matched["accessory_versions"][accessory_id]:
                    raise ConflictError(f"附件 {accessory_id} 能力版本在咨询后发生变化")
                if expected_accessory_versions and \
                        expected_accessory_versions.get(accessory_id) != row["capability_version"]:
                    raise ConflictError(f"附件 {accessory_id} 能力版本与预期不一致")

            violations = self._violations(connection, equipment_id, accessory_ids, start, end, setup)
            waivers = waivers or []
            for item in waivers:
                if not isinstance(item, dict) or not str(item.get("violation", "")).strip():
                    raise ValidationError("waivers 每项必须包含 violation")
            waiver_codes = {str(item.get("violation", "")).strip() for item in waivers}
            unknown = waiver_codes - WAIVABLE_VIOLATIONS
            if unknown:
                raise ValidationError(f"不支持豁免的限制: {','.join(sorted(unknown))}")
            unwaivable = set(violations) - WAIVABLE_VIOLATIONS
            if unwaivable:
                raise ConflictError(f"时段存在不可豁免的限制: {','.join(sorted(unwaivable))}")
            missing = set(violations) - waiver_codes
            if missing:
                raise ConflictError(f"时段存在未豁免的限制: {','.join(sorted(missing))}")
            invalid_waivers = waiver_codes - set(violations)
            if invalid_waivers:
                raise ValidationError(f"豁免项与实际限制不符: {','.join(sorted(invalid_waivers))}")

            reservation_id = uuid.uuid4().hex
            certificate = self._certificate(connection, "equipment", equipment_id, end)
            accessory_certs: dict[str, str] = {}
            for accessory_id in accessory_ids:
                cert = self._certificate(connection, "accessory", accessory_id, end)
                if cert is not None:
                    accessory_certs[accessory_id] = cert["certificate_id"]

            def create() -> tuple[str, str, dict[str, Any]]:
                # 唯一约束保证整组资源原子占用：任一插入失败，事务回滚，不会部分成功。
                self._insert_slots(connection, reservation_id, start, end,
                                   equipment_id, accessory_ids)
                connection.execute(
                    "INSERT INTO booking_reservations(reservation_id,inquiry_id,site_id,equipment_id,"
                    "accessory_ids_json,setup,objective,starts_at,ends_at,priority_tier,"
                    "applicant_actor_id,status,equipment_version,accessory_versions_json,"
                    "equipment_certificate_id,accessory_certificates_json,terminated_at,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?, 'confirmed', ?,?,?,?,NULL,?)",
                    (reservation_id, inquiry_id, inquiry["site_id"], equipment_id,
                     canonical_json(accessory_ids), setup, inquiry["objective"], fmt(start), fmt(end),
                     inquiry["priority_tier"], actor_id, equipment["capability_version"],
                     canonical_json(matched["accessory_versions"]),
                     certificate["certificate_id"] if certificate else None,
                     canonical_json(accessory_certs), self._now()),
                )
                waiver_records = []
                for item in waivers:
                    reason = self._text(str(item.get("reason", "")), "waiver reason", 500)
                    override_id = uuid.uuid4().hex
                    connection.execute(
                        "INSERT INTO booking_manual_overrides(override_id,site_id,target_type,"
                        "target_id,decision,reason,actor_id,created_at) VALUES(?,?,?,?,?,?,?,?)",
                        (override_id, inquiry["site_id"], "reservation", reservation_id,
                         f"waive:{item['violation']}", reason, actor_id, self._now()),
                    )
                    waiver_records.append({"violation": item["violation"], "reason": reason})
                append_event(connection, actor_id=actor_id, action="booking_reservation.confirmed",
                             resource_type="booking_reservation", resource_id=reservation_id,
                             detail={"inquiry_id": inquiry_id, "equipment_id": equipment_id,
                                     "accessory_ids": accessory_ids, "starts_at": fmt(start),
                                     "ends_at": fmt(end), "setup": setup,
                                     "equipment_certificate_id":
                                         certificate["certificate_id"] if certificate else None,
                                     "accessory_certificates": accessory_certs,
                                     "waivers": waiver_records},
                             occurred_at=self._now())
                return "booking_reservation", reservation_id, {"reservation_id": reservation_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="confirm_booking_reservation",
                                    payload=payload, create=create)

    # -------------------------------------------------------------- 维护封锁

    def publish_blockade(self, *, request_id: str, actor_id: str, equipment_id: str,
                         kind: str, reason: str, starts_at: str | None = None,
                         ends_at: str | None = None) -> WriteReceipt:
        """发布计划封锁或紧急停用，并联动未来预约。"""

        payload = {"actor_id": actor_id, "equipment_id": equipment_id, "kind": kind,
                   "reason": reason, "starts_at": starts_at, "ends_at": ends_at}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, *WRITE_ROLES)
            replayed = self._replayed_receipt(connection, request_id,
                                              "publish_booking_blockade", payload)
            if replayed is not None:
                return replayed
            equipment = self._equipment(connection, equipment_id)
            if kind not in ("planned", "emergency"):
                raise ValidationError("kind 只能是 planned 或 emergency")
            reason = self._text(reason, "reason", 500)
            now = self.clock.now().astimezone(timezone.utc).replace(second=0, microsecond=0)
            start = minute_dt(starts_at, "starts_at") if starts_at else now
            if kind == "emergency" and start < now:
                start = now
            end = minute_dt(ends_at, "ends_at") if ends_at else None
            if end is not None and end <= start:
                raise ValidationError("ends_at 必须晚于 starts_at")
            if kind == "planned" and start < now:
                raise ValidationError("计划封锁不能安排在过去")
            blockade_id = uuid.uuid4().hex
            horizon_end = end or now + timedelta(days=3650)

            def create() -> tuple[str, str, dict[str, Any]]:
                connection.execute(
                    "INSERT INTO booking_blockades(blockade_id,equipment_id,kind,starts_at,ends_at,"
                    "reason,status,created_by,created_at) VALUES(?,?,?,?,?,?,'active',?,?)",
                    (blockade_id, equipment_id, kind, fmt(start),
                     fmt(end) if end else None, reason, actor_id, self._now()),
                )
                displaced: list[str] = []
                risks: list[str] = []
                reservation_rows = connection.execute(
                    "SELECT * FROM booking_reservations WHERE equipment_id=? AND status='confirmed'",
                    (equipment_id,),
                ).fetchall()
                for row in reservation_rows:
                    res_start = parse_dt(row["starts_at"], "starts_at")
                    res_end = parse_dt(row["ends_at"], "ends_at")
                    if not overlaps(res_start, res_end, start, horizon_end):
                        continue
                    if res_start <= now < res_end:
                        # 已经开始的使用不自动中断，只登记风险等待人工决定。
                        risk_id = uuid.uuid4().hex
                        connection.execute(
                            "INSERT INTO booking_usage_risks(risk_id,reservation_id,blockade_id,"
                            "equipment_id,detail_json,status,noted_at) VALUES(?,?,?,?,?, 'pending',?)",
                            (risk_id, row["reservation_id"], blockade_id, equipment_id,
                             canonical_json({"kind": kind, "reason": reason,
                                             "blockade_start": fmt(start),
                                             "reservation_start": row["starts_at"],
                                             "reservation_end": row["ends_at"]}),
                             fmt(now)),
                        )
                        risks.append(risk_id)
                        append_event(connection, actor_id=actor_id,
                                     action="booking_usage_risk.noted",
                                     resource_type="booking_usage_risk", resource_id=risk_id,
                                     detail={"reservation_id": row["reservation_id"],
                                             "blockade_id": blockade_id, "kind": kind},
                                     occurred_at=self._now())
                    elif res_start > now:
                        connection.execute(
                            "UPDATE booking_reservations SET status='displaced' "
                            "WHERE reservation_id=?", (row["reservation_id"],),
                        )
                        connection.execute(
                            "DELETE FROM booking_reservation_slots WHERE reservation_id=?",
                            (row["reservation_id"],),
                        )
                        queue_id = uuid.uuid4().hex
                        duration_minutes = int(
                            (res_end - res_start).total_seconds() / 60)
                        connection.execute(
                            "INSERT INTO booking_reschedule_queue(queue_id,reservation_id,site_id,"
                            "equipment_id,accessory_ids_json,setup,objective,duration_minutes,"
                            "priority_tier,applicant_actor_id,original_starts_at,original_ends_at,"
                            "cause_kind,blockade_id,status,enqueued_at) "
                            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,'waiting',?)",
                            (queue_id, row["reservation_id"], row["site_id"], equipment_id,
                             row["accessory_ids_json"], row["setup"], row["objective"],
                             duration_minutes, row["priority_tier"],
                             row["applicant_actor_id"], row["starts_at"], row["ends_at"],
                             kind, blockade_id, fmt(now)),
                        )
                        displaced.append(row["reservation_id"])
                append_event(connection, actor_id=actor_id, action="booking_blockade.published",
                             resource_type="booking_blockade", resource_id=blockade_id,
                             detail={"equipment_id": equipment_id, "kind": kind,
                                     "starts_at": fmt(start),
                                     "ends_at": fmt(end) if end else None, "reason": reason,
                                     "displaced_reservation_ids": displaced,
                                     "risk_ids": risks},
                             occurred_at=self._now())
                return "booking_blockade", blockade_id, {"blockade_id": blockade_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="publish_booking_blockade",
                                    payload=payload, create=create)

    def lift_blockade(self, *, request_id: str, actor_id: str, blockade_id: str,
                      reason: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "blockade_id": blockade_id, "reason": reason}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, *WRITE_ROLES)
            replayed = self._replayed_receipt(connection, request_id,
                                              "lift_booking_blockade", payload)
            if replayed is not None:
                return replayed
            row = connection.execute(
                "SELECT * FROM booking_blockades WHERE blockade_id=?", (blockade_id,),
            ).fetchone()
            if row is None:
                raise NotFoundError("封锁不存在")
            if row["status"] != "active":
                raise ConflictError("封锁已经解除")
            equipment = self._equipment(connection, row["equipment_id"])
            reason = self._text(reason, "reason", 500)

            def create() -> tuple[str, str, dict[str, Any]]:
                now = self._now()
                connection.execute(
                    "UPDATE booking_blockades SET status='lifted', lifted_at=?, lifted_by=? "
                    "WHERE blockade_id=?", (now, actor_id, blockade_id),
                )
                override_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO booking_manual_overrides(override_id,site_id,target_type,"
                    "target_id,decision,reason,actor_id,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (override_id, equipment["site_id"], "booking_blockade", blockade_id,
                     "lifted", reason, actor_id, now),
                )
                append_event(connection, actor_id=actor_id, action="booking_blockade.lifted",
                             resource_type="booking_blockade", resource_id=blockade_id,
                             detail={"reason": reason}, occurred_at=now)
                return "booking_blockade", blockade_id, {"blockade_id": blockade_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="lift_booking_blockade", payload=payload, create=create)

    # -------------------------------------------------------------- 改期队列

    def _queue_view(self, row: Any, position: int | None) -> dict[str, Any]:
        return {
            "queue_id": row["queue_id"], "reservation_id": row["reservation_id"],
            "site_id": row["site_id"], "equipment_id": row["equipment_id"],
            "accessory_ids": json.loads(row["accessory_ids_json"]), "setup": row["setup"],
            "objective": row["objective"], "duration_minutes": row["duration_minutes"],
            "priority_tier": row["priority_tier"], "position": position,
            "applicant_actor_id": row["applicant_actor_id"],
            "original_starts_at": row["original_starts_at"],
            "original_ends_at": row["original_ends_at"],
            "cause_kind": row["cause_kind"], "blockade_id": row["blockade_id"],
            "status": row["status"], "new_reservation_id": row["new_reservation_id"],
            "enqueued_at": row["enqueued_at"], "resolved_at": row["resolved_at"],
        }

    def list_reschedule_queue(self, site_id: str, status: str = "waiting") -> list[dict[str, Any]]:
        result = self.database.connection.execute(
            "SELECT * FROM booking_reschedule_queue WHERE site_id=? AND status=? "
            "ORDER BY priority_tier, original_starts_at, enqueued_at",
            (site_id, status),
        ).fetchall()
        return [self._queue_view(row, index + 1 if status == "waiting" else None)
                for index, row in enumerate(result)]

    def rebook_queue_entry(self, *, request_id: str, actor_id: str, queue_id: str,
                           starts_at: str) -> WriteReceipt:
        """把改期队列条目原子改到新时段。"""

        payload = {"actor_id": actor_id, "queue_id": queue_id, "starts_at": starts_at}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, *WRITE_ROLES)
            replayed = self._replayed_receipt(connection, request_id,
                                              "rebook_booking_reservation", payload)
            if replayed is not None:
                return replayed
            queue = connection.execute(
                "SELECT * FROM booking_reschedule_queue WHERE queue_id=?", (queue_id,),
            ).fetchone()
            if queue is None:
                raise NotFoundError("改期队列条目不存在")
            if queue["status"] != "waiting":
                raise ConflictError("改期队列条目已经处理")
            if queue["applicant_actor_id"] != actor_id and actor.role != "admin":
                raise PermissionDenied("只能处理自己的改期条目")
            start = minute_dt(starts_at, "starts_at")
            duration = timedelta(minutes=queue["duration_minutes"])
            end = start + duration
            accessory_ids = json.loads(queue["accessory_ids_json"])
            violations = self._violations(
                connection, queue["equipment_id"], accessory_ids, start, end, queue["setup"])
            if violations:
                raise ConflictError(f"新时段仍有限制: {','.join(sorted(set(violations)))}")
            equipment = self._equipment(connection, queue["equipment_id"])
            accessory_versions: dict[str, int] = {}
            accessory_certs: dict[str, str] = {}
            for accessory_id in accessory_ids:
                row = self._accessory(connection, accessory_id)
                accessory_versions[accessory_id] = row["capability_version"]
                cert = self._certificate(connection, "accessory", accessory_id, end)
                if cert is None:
                    raise ConflictError(f"附件 {accessory_id} 校准证书已过期")
                accessory_certs[accessory_id] = cert["certificate_id"]
            certificate = self._certificate(connection, "equipment", queue["equipment_id"], end)
            if certificate is None:
                raise ConflictError("设备校准证书已过期")
            new_id = uuid.uuid4().hex

            def create() -> tuple[str, str, dict[str, Any]]:
                self._insert_slots(connection, new_id, start, end,
                                   queue["equipment_id"], accessory_ids)
                connection.execute(
                    "INSERT INTO booking_reservations(reservation_id,inquiry_id,site_id,equipment_id,"
                    "accessory_ids_json,setup,objective,starts_at,ends_at,priority_tier,"
                    "applicant_actor_id,status,equipment_version,accessory_versions_json,"
                    "equipment_certificate_id,accessory_certificates_json,terminated_at,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?, 'confirmed', ?,?,?,?,?,?)",
                    (new_id, None, queue["site_id"], queue["equipment_id"],
                     queue["accessory_ids_json"], queue["setup"], queue["objective"],
                     fmt(start), fmt(end), queue["priority_tier"], queue["applicant_actor_id"],
                     equipment["capability_version"], canonical_json(accessory_versions),
                     certificate["certificate_id"], canonical_json(accessory_certs),
                     None, self._now()),
                )
                connection.execute(
                    "UPDATE booking_reservations SET status='rebooked' WHERE reservation_id=?",
                    (queue["reservation_id"],),
                )
                connection.execute(
                    "UPDATE booking_reschedule_queue SET status='rebooked', new_reservation_id=?, "
                    "resolved_at=? WHERE queue_id=?", (new_id, self._now(), queue_id),
                )
                append_event(connection, actor_id=actor_id,
                             action="booking_reservation.rebooked",
                             resource_type="booking_reservation", resource_id=new_id,
                             detail={"queue_id": queue_id,
                                     "original_reservation_id": queue["reservation_id"],
                                     "equipment_id": queue["equipment_id"],
                                     "accessory_ids": accessory_ids,
                                     "starts_at": fmt(start), "ends_at": fmt(end),
                                     "equipment_certificate_id": certificate["certificate_id"],
                                     "accessory_certificates": accessory_certs},
                             occurred_at=self._now())
                return "booking_reservation", new_id, {"reservation_id": new_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="rebook_booking_reservation",
                                    payload=payload, create=create)

    def cancel_queue_entry(self, *, request_id: str, actor_id: str, queue_id: str,
                           reason: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "queue_id": queue_id, "reason": reason}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, *WRITE_ROLES)
            replayed = self._replayed_receipt(connection, request_id,
                                              "cancel_booking_reservation", payload)
            if replayed is not None:
                return replayed
            queue = connection.execute(
                "SELECT * FROM booking_reschedule_queue WHERE queue_id=?", (queue_id,),
            ).fetchone()
            if queue is None:
                raise NotFoundError("改期队列条目不存在")
            if queue["status"] != "waiting":
                raise ConflictError("改期队列条目已经处理")
            if queue["applicant_actor_id"] != actor_id and actor.role != "admin":
                raise PermissionDenied("只能取消自己的改期条目")
            reason = self._text(reason, "reason", 500)

            def create() -> tuple[str, str, dict[str, Any]]:
                connection.execute(
                    "UPDATE booking_reservations SET status='cancelled' WHERE reservation_id=?",
                    (queue["reservation_id"],),
                )
                connection.execute(
                    "UPDATE booking_reschedule_queue SET status='cancelled', resolved_at=? "
                    "WHERE queue_id=?", (self._now(), queue_id),
                )
                append_event(connection, actor_id=actor_id,
                             action="booking_reservation.cancelled",
                             resource_type="booking_reservation",
                             resource_id=queue["reservation_id"],
                             detail={"queue_id": queue_id, "reason": reason},
                             occurred_at=self._now())
                return "booking_reservation", queue["reservation_id"], \
                    {"reservation_id": queue["reservation_id"]}

            return self._idempotent(connection, request_id=request_id,
                                    action="cancel_booking_reservation",
                                    payload=payload, create=create)

    # -------------------------------------------------------------- 风险与覆盖

    def list_usage_risks(self, site_id: str | None = None,
                         status: str | None = None) -> list[dict[str, Any]]:
        query = ("SELECT r.*, q.reason AS blockade_reason, q.kind AS blockade_kind "
                 "FROM booking_usage_risks r JOIN booking_blockades q ON r.blockade_id=q.blockade_id")
        clauses: list[str] = []
        parameters: list[Any] = []
        if site_id:
            clauses.append("EXISTS(SELECT 1 FROM booking_reservations br WHERE "
                           "br.reservation_id=r.reservation_id AND br.site_id=?)")
            parameters.append(site_id)
        if status:
            clauses.append("r.status=?")
            parameters.append(status)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY r.noted_at"
        rows = self.database.connection.execute(query, parameters).fetchall()
        return [{"risk_id": row["risk_id"], "reservation_id": row["reservation_id"],
                 "blockade_id": row["blockade_id"], "equipment_id": row["equipment_id"],
                 "detail": json.loads(row["detail_json"]), "status": row["status"],
                 "noted_at": row["noted_at"], "resolved_at": row["resolved_at"],
                 "blockade_reason": row["blockade_reason"],
                 "blockade_kind": row["blockade_kind"]} for row in rows]

    def resolve_usage_risk(self, *, request_id: str, actor_id: str, risk_id: str,
                           decision: str, reason: str) -> WriteReceipt:
        """人工决定进行中使用遇到封锁后的处理方式。"""

        payload = {"actor_id": actor_id, "risk_id": risk_id, "decision": decision, "reason": reason}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, *WRITE_ROLES)
            replayed = self._replayed_receipt(connection, request_id,
                                              "resolve_booking_usage_risk", payload)
            if replayed is not None:
                return replayed
            risk = connection.execute(
                "SELECT * FROM booking_usage_risks WHERE risk_id=?", (risk_id,),
            ).fetchone()
            if risk is None:
                raise NotFoundError("使用风险不存在")
            if risk["status"] != "pending":
                raise ConflictError("使用风险已经处理")
            if decision not in ("continued", "terminated", "rescheduled"):
                raise ValidationError("decision 只能是 continued、terminated 或 rescheduled")
            reason = self._text(reason, "reason", 500)
            reservation = connection.execute(
                "SELECT * FROM booking_reservations WHERE reservation_id=?",
                (risk["reservation_id"],),
            ).fetchone()
            now = self.clock.now().astimezone(timezone.utc).replace(second=0, microsecond=0)

            def create() -> tuple[str, str, dict[str, Any]]:
                override_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO booking_manual_overrides(override_id,site_id,target_type,"
                    "target_id,decision,reason,actor_id,created_at) "
                    "VALUES(?,?,'booking_usage_risk',?,?,?,?,?)",
                    (override_id, reservation["site_id"], risk_id, decision, reason,
                     actor_id, self._now()),
                )
                connection.execute(
                    "UPDATE booking_usage_risks SET status=?, resolved_at=? WHERE risk_id=?",
                    (decision, fmt(now), risk_id),
                )
                if decision == "terminated":
                    connection.execute(
                        "UPDATE booking_reservations SET status='terminated', terminated_at=? "
                        "WHERE reservation_id=?", (fmt(now), reservation["reservation_id"]),
                    )
                    connection.execute(
                        "DELETE FROM booking_reservation_slots WHERE reservation_id=? AND slot>?",
                        (reservation["reservation_id"], slot_key(now)),
                    )
                elif decision == "rescheduled":
                    connection.execute(
                        "UPDATE booking_reservations SET status='displaced' "
                        "WHERE reservation_id=?", (reservation["reservation_id"],),
                    )
                    connection.execute(
                        "DELETE FROM booking_reservation_slots WHERE reservation_id=? AND slot>?",
                        (reservation["reservation_id"], slot_key(now)),
                    )
                    queue_id = uuid.uuid4().hex
                    risk_duration = int(
                        (parse_dt(reservation["ends_at"], "ends_at")
                         - parse_dt(reservation["starts_at"], "starts_at")).total_seconds() / 60)
                    connection.execute(
                        "INSERT INTO booking_reschedule_queue(queue_id,reservation_id,site_id,"
                        "equipment_id,accessory_ids_json,setup,objective,duration_minutes,"
                        "priority_tier,applicant_actor_id,original_starts_at,original_ends_at,"
                        "cause_kind,blockade_id,status,enqueued_at) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,'waiting',?)",
                        (queue_id, reservation["reservation_id"], reservation["site_id"],
                         reservation["equipment_id"], reservation["accessory_ids_json"],
                         reservation["setup"], reservation["objective"],
                         risk_duration, reservation["priority_tier"],
                         reservation["applicant_actor_id"], reservation["starts_at"],
                         reservation["ends_at"], "emergency", risk["blockade_id"], fmt(now)),
                    )
                append_event(connection, actor_id=actor_id,
                             action="booking_usage_risk.resolved",
                             resource_type="booking_usage_risk", resource_id=risk_id,
                             detail={"decision": decision, "reason": reason,
                                     "reservation_id": reservation["reservation_id"]},
                             occurred_at=self._now())
                return "booking_usage_risk", risk_id, {"risk_id": risk_id, "decision": decision}

            return self._idempotent(connection, request_id=request_id,
                                    action="resolve_booking_usage_risk",
                                    payload=payload, create=create)

    def list_manual_overrides(self, site_id: str) -> list[dict[str, Any]]:
        rows = self.database.connection.execute(
            "SELECT * FROM booking_manual_overrides WHERE site_id=? ORDER BY created_at",
            (site_id,),
        ).fetchall()
        return [{"override_id": row["override_id"], "site_id": row["site_id"],
                 "target_type": row["target_type"], "target_id": row["target_id"],
                 "decision": row["decision"], "reason": row["reason"],
                 "actor_id": row["actor_id"], "created_at": row["created_at"]} for row in rows]

    # -------------------------------------------------------------- 只读视图

    def list_equipment(self, site_id: str) -> list[dict[str, Any]]:
        connection = self.database.connection
        rows = connection.execute(
            "SELECT * FROM booking_equipment WHERE site_id=? ORDER BY equipment_id", (site_id,),
        ).fetchall()
        result = []
        for row in rows:
            accessory_rows = connection.execute(
                "SELECT a.* FROM booking_accessories a JOIN booking_equipment_accessories l "
                "ON a.accessory_id=l.accessory_id WHERE l.equipment_id=? ORDER BY a.accessory_id",
                (row["equipment_id"],),
            ).fetchall()
            now = fmt(self.clock.now().astimezone(timezone.utc))
            certificate = connection.execute(
                "SELECT * FROM booking_certificates WHERE target_type='equipment' AND target_id=? "
                "AND calibrated_at<=? AND expires_at>=? ORDER BY expires_at DESC LIMIT 1",
                (row["equipment_id"], now, now),
            ).fetchone()
            windows = connection.execute(
                "SELECT starts_at,ends_at FROM booking_open_windows WHERE equipment_id=? "
                "ORDER BY starts_at", (row["equipment_id"],),
            ).fetchall()
            result.append({
                "equipment_id": row["equipment_id"], "site_id": row["site_id"],
                "name": row["name"], "kind": row["kind"],
                "capabilities": json.loads(row["capabilities_json"]),
                "capability_version": row["capability_version"],
                "certificate": self._certificate_view(certificate),
                "open_windows": [{"starts_at": w["starts_at"], "ends_at": w["ends_at"]}
                                 for w in windows],
                "accessories": [{
                    "accessory_id": a["accessory_id"], "name": a["name"],
                    "capabilities": json.loads(a["capabilities_json"]),
                    "capability_version": a["capability_version"],
                    "certificate": self._certificate_view(connection.execute(
                        "SELECT * FROM booking_certificates WHERE target_type='accessory' "
                        "AND target_id=? AND calibrated_at<=? AND expires_at>=? "
                        "ORDER BY expires_at DESC LIMIT 1",
                        (a["accessory_id"], now, now)).fetchone()),
                } for a in accessory_rows],
            })
        return result

    @staticmethod
    def _certificate_view(row: Any) -> dict[str, Any] | None:
        if row is None:
            return None
        return {"certificate_id": row["certificate_id"], "certificate_no": row["certificate_no"],
                "basis": row["basis"], "calibrated_at": row["calibrated_at"],
                "expires_at": row["expires_at"]}

    def _certificate_by_id(self, connection, certificate_id: str | None) -> dict[str, Any] | None:
        if not certificate_id:
            return None
        row = connection.execute(
            "SELECT * FROM booking_certificates WHERE certificate_id=?", (certificate_id,),
        ).fetchone()
        return self._certificate_view(row)

    def list_reservations(self, site_id: str, status: str | None = None,
                          equipment_id: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM booking_reservations WHERE site_id=?"
        parameters: list[Any] = [site_id]
        if status:
            query += " AND status=?"
            parameters.append(status)
        if equipment_id:
            query += " AND equipment_id=?"
            parameters.append(equipment_id)
        query += " ORDER BY starts_at"
        rows = self.database.connection.execute(query, parameters).fetchall()
        result = []
        for row in rows:
            overrides = self.database.connection.execute(
                "SELECT decision,reason,actor_id,created_at FROM booking_manual_overrides "
                "WHERE target_type='reservation' AND target_id=? ORDER BY created_at",
                (row["reservation_id"],),
            ).fetchall()
            cert_map = json.loads(row["accessory_certificates_json"])
            result.append({
                "reservation_id": row["reservation_id"], "inquiry_id": row["inquiry_id"],
                "site_id": row["site_id"], "equipment_id": row["equipment_id"],
                "accessory_ids": json.loads(row["accessory_ids_json"]),
                "setup": row["setup"], "objective": row["objective"],
                "starts_at": row["starts_at"], "ends_at": row["ends_at"],
                "priority_tier": row["priority_tier"],
                "applicant_actor_id": row["applicant_actor_id"], "status": row["status"],
                "equipment_version": row["equipment_version"],
                "accessory_versions": json.loads(row["accessory_versions_json"]),
                "calibration_basis": {
                    "equipment": self._certificate_by_id(
                        self.database.connection, row["equipment_certificate_id"]),
                    "accessories": {
                        accessory_id: self._certificate_by_id(self.database.connection, cert_id)
                        for accessory_id, cert_id in cert_map.items()
                    },
                },
                "terminated_at": row["terminated_at"], "created_at": row["created_at"],
                "manual_overrides": [{"decision": o["decision"], "reason": o["reason"],
                                      "actor_id": o["actor_id"], "created_at": o["created_at"]}
                                     for o in overrides],
            })
        return result

    def resource_occupancy(self, resource_type: str, resource_id: str,
                           starts_at: str | None = None, ends_at: str | None = None
                           ) -> dict[str, Any]:
        """展示单个资源在时间范围内的占用、封锁与改期影响。"""

        if resource_type not in ("equipment", "accessory"):
            raise ValidationError("resource_type 只能是 equipment 或 accessory")
        now = self.clock.now().astimezone(timezone.utc).replace(second=0, microsecond=0)
        start = minute_dt(starts_at, "starts_at") if starts_at else now
        end = minute_dt(ends_at, "ends_at") if ends_at else now + timedelta(days=7)
        if end <= start:
            raise ValidationError("ends_at 必须晚于 starts_at")
        connection = self.database.connection
        busy: list[dict[str, Any]] = []
        reservation_rows = connection.execute(
            "SELECT * FROM booking_reservations WHERE status IN ('confirmed','terminated')"
        ).fetchall()
        for row in reservation_rows:
            if resource_type == "equipment":
                touched = row["equipment_id"] == resource_id
            else:
                touched = resource_id in self._loads(row["accessory_ids_json"])
            res_start = parse_dt(row["starts_at"], "starts_at")
            res_end = parse_dt(row["ends_at"], "ends_at")
            if touched and overlaps(start, end, res_start, res_end):
                busy.append({"kind": "reservation", "reservation_id": row["reservation_id"],
                             "status": row["status"], "setup": row["setup"],
                             "starts_at": row["starts_at"], "ends_at": row["ends_at"],
                             "priority_tier": row["priority_tier"]})
        if resource_type == "equipment":
            blockade_rows = connection.execute(
                "SELECT * FROM booking_blockades WHERE equipment_id=? AND status='active'",
                (resource_id,),
            ).fetchall()
            for row in blockade_rows:
                b_start = parse_dt(row["starts_at"], "starts_at")
                b_end = parse_dt(row["ends_at"], "ends_at") if row["ends_at"] else end
                if overlaps(start, end, b_start, b_end):
                    busy.append({"kind": "blockade", "blockade_id": row["blockade_id"],
                                 "blockade_kind": row["kind"], "reason": row["reason"],
                                 "starts_at": row["starts_at"],
                                 "ends_at": row["ends_at"], "open_ended": row["ends_at"] is None})
        busy.sort(key=lambda item: (item["starts_at"], item["kind"]))
        return {"resource_type": resource_type, "resource_id": resource_id,
                "starts_at": fmt(start), "ends_at": fmt(end), "busy": busy}
