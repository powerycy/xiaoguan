#!/usr/bin/env python3
"""Local, bounded, consent-gated customer memory store for the xiaoguan skill."""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


POLICY_VERSION = "1"
MAX_VALUE_CHARS = 400
MAX_SOURCE_CHARS = 200
MAX_CLIENTS = 30
MAX_ROWS = 6000
MAX_OPERATIONS = 100
SCOPE_LIMITS = {
    "user": 30,
    "account": 40,
    "contact": 40,
    "deal": 40,
    "event": 40,
    "hypothesis": 10,
}
SCOPES = set(SCOPE_LIMITS)
SOURCE_TYPES = {
    "user_explicit",
    "user_report",
    "public_source",
    "tool",
    "assistant_inference",
}
CONFIDENCE_LEVELS = {"high", "medium", "low"}
PUBLIC_CONTACT_FIELDS = {
    "display_label",
    "public_name",
    "public_role",
    "public_bio",
    "public_career",
    "public_statements",
    "public_topics",
}
SENSITIVE_FIELD_PARTS = {
    "private_phone",
    "private_email",
    "home_address",
    "health",
    "religion",
    "ethnicity",
    "sexual_orientation",
    "financial_account",
    "government_id",
}
LEGACY_CLIENT_ALIASES = {
    "client-a": "client-01",
    "client-b": "client-02",
    "client-c": "client-03",
    "客户a": "client-01",
    "客户b": "client-02",
    "客户c": "client-03",
}
CANONICAL_TO_LEGACY = {
    "client-01": "client-a",
    "client-02": "client-b",
    "client-03": "client-c",
}


class MemoryErrorWithCode(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def memory_dir() -> Path:
    override = os.environ.get("XIAOGUAN_MEMORY_DIR")
    if override:
        return Path(override).expanduser().resolve()
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "xiaoguan"
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", str(Path.home())))
        return base / "xiaoguan"
    base = Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share")))
    return base / "xiaoguan"


def db_path() -> Path:
    return memory_dir() / "memory.sqlite3"


def emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def connect(create: bool = False) -> sqlite3.Connection:
    target = db_path()
    if not target.exists() and not create:
        raise MemoryErrorWithCode("NOT_INITIALIZED", "尚未启用长期记忆")
    if create:
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            target.parent.chmod(0o700)
        except OSError:
            pass
    conn = sqlite3.connect(target)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS memories (
            id TEXT PRIMARY KEY,
            scope TEXT NOT NULL,
            subject_id TEXT NOT NULL,
            field TEXT NOT NULL,
            value TEXT NOT NULL,
            source_type TEXT NOT NULL,
            source_ref TEXT NOT NULL DEFAULT '',
            occurred_at TEXT,
            observed_at TEXT NOT NULL,
            confidence TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_memories_lookup
            ON memories(scope, subject_id, status, updated_at);
        CREATE TABLE IF NOT EXISTS operations (
            op_id TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            action TEXT NOT NULL,
            before_json TEXT NOT NULL,
            after_json TEXT NOT NULL
        );
        """
    )
    conn.commit()
    try:
        target.chmod(0o600)
    except OSError:
        pass
    return conn


def get_setting(conn: sqlite3.Connection, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return str(row["value"]) if row else default


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO settings(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def require_enabled(conn: sqlite3.Connection) -> None:
    if get_setting(conn, "consent_enabled") != "true":
        raise MemoryErrorWithCode("CONSENT_REQUIRED", "长期记忆尚未获得用户同意")
    if get_setting(conn, "paused") == "true":
        raise MemoryErrorWithCode("MEMORY_PAUSED", "长期记忆当前已暂停")


def row_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}


def validate_text(name: str, value: Any, maximum: int, required: bool = True) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise MemoryErrorWithCode("INVALID_DELTA", f"{name} 必须是字符串")
    value = value.strip()
    if required and not value:
        raise MemoryErrorWithCode("INVALID_DELTA", f"{name} 不能为空")
    if len(value) > maximum:
        raise MemoryErrorWithCode("INVALID_DELTA", f"{name} 超过 {maximum} 字符")
    if any(ord(char) < 32 and char not in "\t\n" for char in value):
        raise MemoryErrorWithCode("INVALID_DELTA", f"{name} 包含控制字符")
    return value


def canonical_subject_id(value: Any, allow_user: bool = False) -> str:
    subject_id = validate_text("subject_id", value, 64)
    lowered = subject_id.lower()
    if lowered in {"temporary", "temp", "临时", "临时会话"}:
        raise MemoryErrorWithCode("TEMPORARY_SESSION", "临时会话不得写入或读取长期客户档案")
    if allow_user and lowered == "user":
        return "user"
    if lowered in LEGACY_CLIENT_ALIASES:
        return LEGACY_CLIENT_ALIASES[lowered]
    match = re.fullmatch(r"client-(\d{1,2})", lowered)
    if not match:
        match = re.fullmatch(r"客户(\d{1,2})", lowered)
    if not match:
        raise MemoryErrorWithCode(
            "INVALID_SUBJECT",
            "客户槽位必须是 client-01 至 client-30；客户A/B/C兼容映射到01/02/03",
        )
    number = int(match.group(1))
    if not 1 <= number <= MAX_CLIENTS:
        raise MemoryErrorWithCode("CLIENT_LIMIT", f"客户槽位只支持 01 至 {MAX_CLIENTS:02d}")
    return f"client-{number:02d}"


def subject_variants(subject_id: str) -> list[str]:
    variants = [subject_id]
    legacy = CANONICAL_TO_LEGACY.get(subject_id)
    if legacy:
        variants.append(legacy)
    return variants


def sql_in(values: list[str]) -> tuple[str, tuple[str, ...]]:
    return ",".join("?" for _ in values), tuple(values)


def validate_delta(raw: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise MemoryErrorWithCode("INVALID_DELTA", "记忆变更必须是 JSON 对象")
    scope = validate_text("scope", raw.get("scope"), 32)
    if scope not in SCOPES:
        raise MemoryErrorWithCode("INVALID_DELTA", f"不支持的 scope: {scope}")
    subject_id = canonical_subject_id(raw.get("subject_id"), allow_user=scope == "user")
    if scope == "user" and subject_id != "user":
        raise MemoryErrorWithCode("INVALID_SUBJECT", "user scope 必须使用 subject_id=user")
    field = validate_text("field", raw.get("field"), 64)
    lowered_field = field.lower()
    if any(part in lowered_field for part in SENSITIVE_FIELD_PARTS):
        raise MemoryErrorWithCode("SENSITIVE_FIELD", "不得保存敏感或私人联系信息")
    value = validate_text("value", raw.get("value"), MAX_VALUE_CHARS)
    source_type = validate_text("source_type", raw.get("source_type"), 32)
    if source_type not in SOURCE_TYPES:
        raise MemoryErrorWithCode("INVALID_DELTA", f"不支持的 source_type: {source_type}")
    confidence = validate_text("confidence", raw.get("confidence", "medium"), 16)
    if confidence not in CONFIDENCE_LEVELS:
        raise MemoryErrorWithCode("INVALID_DELTA", f"不支持的 confidence: {confidence}")
    source_ref = validate_text(
        "source_ref", raw.get("source_ref", ""), MAX_SOURCE_CHARS, required=False
    )
    occurred_at = validate_text(
        "occurred_at", raw.get("occurred_at", ""), 64, required=False
    )
    if scope == "user" and source_type != "user_explicit":
        raise MemoryErrorWithCode(
            "SOURCE_NOT_ELIGIBLE", "用户稳定档案只接受用户明确陈述"
        )
    if scope in {"account", "contact", "deal"} and source_type not in {
        "user_explicit",
        "user_report",
        "public_source",
    }:
        raise MemoryErrorWithCode(
            "SOURCE_NOT_ELIGIBLE", "客户、联系人和商机事实只接受用户陈述、转述或公开来源"
        )
    if source_type in {"tool", "assistant_inference"} and scope not in {
        "event",
        "hypothesis",
    }:
        raise MemoryErrorWithCode(
            "SOURCE_NOT_ELIGIBLE", "外部工具和模型推断只能写入事件或假设"
        )
    if source_type == "assistant_inference" and scope != "hypothesis":
        raise MemoryErrorWithCode(
            "SOURCE_NOT_ELIGIBLE", "模型推断只能写入带置信度的假设"
        )
    if source_type == "public_source":
        if scope not in {"account", "contact", "event"}:
            raise MemoryErrorWithCode(
                "SOURCE_NOT_ELIGIBLE", "公开来源只能写入企业、公开职业信息或公开事件"
            )
        public_contact_field = field.rsplit(".", 1)[-1]
        if scope == "contact" and public_contact_field not in PUBLIC_CONTACT_FIELDS:
            raise MemoryErrorWithCode(
                "SOURCE_NOT_ELIGIBLE", "公开联系人资料只允许保存公开职业字段"
            )
    return {
        "scope": scope,
        "subject_id": subject_id,
        "field": field,
        "value": value,
        "source_type": source_type,
        "source_ref": source_ref,
        "occurred_at": occurred_at or None,
        "confidence": confidence,
    }


def prune_operation_history(conn: sqlite3.Connection) -> None:
    conn.execute(
        "DELETE FROM operations WHERE op_id NOT IN ("
        "SELECT op_id FROM operations ORDER BY created_at DESC, rowid DESC LIMIT ?)",
        (MAX_OPERATIONS,),
    )


def prune_rows(conn: sqlite3.Connection, delta: dict[str, Any]) -> list[dict[str, Any]]:
    removed: list[dict[str, Any]] = []
    scope = delta["scope"]
    subject_id = delta["subject_id"]
    variants = subject_variants(subject_id) if subject_id != "user" else ["user"]
    placeholders, variant_args = sql_in(variants)
    scope_count = conn.execute(
        "SELECT COUNT(*) AS n FROM memories "
        f"WHERE scope = ? AND subject_id IN ({placeholders}) AND status = 'active'",
        (scope, *variant_args),
    ).fetchone()["n"]
    if scope in {"user", "account", "contact", "deal"} and scope_count > SCOPE_LIMITS[scope]:
        raise MemoryErrorWithCode(
            "MEMORY_LIMIT_REACHED",
            f"{scope} 档案已达 {SCOPE_LIMITS[scope]} 条上限，请先合并或删除旧字段",
        )
    if scope in {"event", "hypothesis"}:
        excess = scope_count - SCOPE_LIMITS[scope]
        if excess > 0:
            rows = conn.execute(
                f"SELECT * FROM memories WHERE scope = ? AND subject_id IN ({placeholders}) "
                "AND status = 'active' ORDER BY updated_at ASC LIMIT ?",
                (scope, *variant_args, excess),
            ).fetchall()
            for row in rows:
                removed.append(row_dict(row))
                conn.execute("DELETE FROM memories WHERE id = ?", (row["id"],))

    total = conn.execute("SELECT COUNT(*) AS n FROM memories").fetchone()["n"]
    excess_total = total - MAX_ROWS
    if excess_total > 0:
        rows = conn.execute(
            "SELECT * FROM memories WHERE scope IN ('event', 'hypothesis') "
            "ORDER BY updated_at ASC LIMIT ?",
            (excess_total,),
        ).fetchall()
        for row in rows:
            if not any(item["id"] == row["id"] for item in removed):
                removed.append(row_dict(row))
                conn.execute("DELETE FROM memories WHERE id = ?", (row["id"],))
    if conn.execute("SELECT COUNT(*) AS n FROM memories").fetchone()["n"] > MAX_ROWS:
        raise MemoryErrorWithCode(
            "MEMORY_LIMIT_REACHED", "长期记忆已达上限，请先归档或删除旧客户"
        )
    return removed


def command_status(_: argparse.Namespace) -> None:
    target = db_path()
    if not target.exists():
        emit(
            {
                "exists": False,
                "consent_enabled": False,
                "paused": False,
                "policy_version": POLICY_VERSION,
                "client_capacity": MAX_CLIENTS,
                "memory_capacity": MAX_ROWS,
                "path": str(target),
            }
        )
        return
    with connect() as conn:
        emit(
            {
                "exists": True,
                "consent_enabled": get_setting(conn, "consent_enabled") == "true",
                "consent_at": get_setting(conn, "consent_at") or None,
                "paused": get_setting(conn, "paused") == "true",
                "policy_version": get_setting(conn, "policy_version", POLICY_VERSION),
                "memory_count": conn.execute("SELECT COUNT(*) AS n FROM memories").fetchone()["n"],
                "undo_count": conn.execute("SELECT COUNT(*) AS n FROM operations").fetchone()["n"],
                "client_capacity": MAX_CLIENTS,
                "memory_capacity": MAX_ROWS,
                "path": str(target),
            }
        )


def command_enable(args: argparse.Namespace) -> None:
    if not args.confirm:
        raise MemoryErrorWithCode("CONFIRMATION_REQUIRED", "需要 --confirm 表示用户已明确同意")
    with connect(create=True) as conn:
        timestamp = now_iso()
        if not get_setting(conn, "consent_at"):
            set_setting(conn, "consent_at", timestamp)
        set_setting(conn, "consent_enabled", "true")
        set_setting(conn, "paused", "false")
        set_setting(conn, "policy_version", POLICY_VERSION)
        conn.commit()
    emit({"ok": True, "consent_enabled": True, "paused": False})


def command_pause(_: argparse.Namespace) -> None:
    with connect() as conn:
        if get_setting(conn, "consent_enabled") != "true":
            raise MemoryErrorWithCode("CONSENT_REQUIRED", "长期记忆尚未启用")
        set_setting(conn, "paused", "true")
        conn.commit()
    emit({"ok": True, "paused": True})


def command_resume(_: argparse.Namespace) -> None:
    with connect() as conn:
        if get_setting(conn, "consent_enabled") != "true":
            raise MemoryErrorWithCode("CONSENT_REQUIRED", "长期记忆尚未启用")
        set_setting(conn, "paused", "false")
        conn.commit()
    emit({"ok": True, "paused": False})


def load_delta(args: argparse.Namespace) -> dict[str, Any]:
    if args.file:
        raw = json.loads(Path(args.file).read_text(encoding="utf-8"))
    elif args.json:
        raw = json.loads(args.json)
    else:
        raw = json.load(sys.stdin)
    return validate_delta(raw)


def command_apply(args: argparse.Namespace) -> None:
    delta = load_delta(args)
    timestamp = now_iso()
    with connect() as conn:
        require_enabled(conn)
        conn.execute("BEGIN IMMEDIATE")
        before: list[dict[str, Any]] = []
        was_update = False
        variants = subject_variants(delta["subject_id"]) if delta["subject_id"] != "user" else ["user"]
        placeholders, variant_args = sql_in(variants)
        if delta["scope"] == "event":
            existing_rows = conn.execute(
                f"SELECT * FROM memories WHERE scope = 'event' AND subject_id IN ({placeholders}) "
                "AND field = ? AND occurred_at IS ? AND status = 'active' "
                "ORDER BY updated_at DESC, rowid DESC",
                (*variant_args, delta["field"], delta["occurred_at"]),
            ).fetchall()
            existing = existing_rows[0] if existing_rows else None
            if existing_rows:
                before.extend(row_dict(row) for row in existing_rows)
                for duplicate in existing_rows[1:]:
                    conn.execute("DELETE FROM memories WHERE id = ?", (duplicate["id"],))
        else:
            existing = conn.execute(
                f"SELECT * FROM memories WHERE scope = ? AND subject_id IN ({placeholders}) "
                "AND field = ? AND status = 'active' LIMIT 1",
                (delta["scope"], *variant_args, delta["field"]),
            ).fetchone()
            if existing:
                before.append(row_dict(existing))
        if existing:
            memory_id = existing["id"]
            created_at = existing["created_at"]
            was_update = True
        else:
            memory_id = uuid.uuid4().hex
            created_at = timestamp
        row = {
            "id": memory_id,
            **delta,
            "observed_at": timestamp,
            "status": "active",
            "created_at": created_at,
            "updated_at": timestamp,
        }
        conn.execute(
            """
            INSERT INTO memories(
                id, scope, subject_id, field, value, source_type, source_ref,
                occurred_at, observed_at, confidence, status, created_at, updated_at
            ) VALUES(
                :id, :scope, :subject_id, :field, :value, :source_type, :source_ref,
                :occurred_at, :observed_at, :confidence, :status, :created_at, :updated_at
            ) ON CONFLICT(id) DO UPDATE SET
                subject_id = excluded.subject_id,
                value = excluded.value,
                source_type = excluded.source_type,
                source_ref = excluded.source_ref,
                occurred_at = excluded.occurred_at,
                observed_at = excluded.observed_at,
                confidence = excluded.confidence,
                status = excluded.status,
                updated_at = excluded.updated_at
            """,
            row,
        )
        before.extend(prune_rows(conn, delta))
        op_id = uuid.uuid4().hex
        conn.execute(
            "INSERT INTO operations(op_id, created_at, action, before_json, after_json) "
            "VALUES(?, ?, 'apply', ?, ?)",
            (
                op_id,
                timestamp,
                json.dumps(before, ensure_ascii=False),
                json.dumps([row], ensure_ascii=False),
            ),
        )
        prune_operation_history(conn)
        conn.commit()
    emit(
        {
            "ok": True,
            "op_id": op_id,
            "memory": row,
            "pruned": len(before) - (1 if was_update else 0),
        }
    )


def restore_rows(conn: sqlite3.Connection, rows: list[dict[str, Any]]) -> None:
    for row in rows:
        conn.execute(
            """
            INSERT OR REPLACE INTO memories(
                id, scope, subject_id, field, value, source_type, source_ref,
                occurred_at, observed_at, confidence, status, created_at, updated_at
            ) VALUES(
                :id, :scope, :subject_id, :field, :value, :source_type, :source_ref,
                :occurred_at, :observed_at, :confidence, :status, :created_at, :updated_at
            )
            """,
            row,
        )


def command_undo(args: argparse.Namespace) -> None:
    with connect() as conn:
        if args.op_id:
            operation = conn.execute(
                "SELECT * FROM operations WHERE op_id = ?", (args.op_id,)
            ).fetchone()
        else:
            operation = conn.execute(
                "SELECT * FROM operations ORDER BY created_at DESC, rowid DESC LIMIT 1"
            ).fetchone()
        if not operation:
            raise MemoryErrorWithCode("NOTHING_TO_UNDO", "没有可撤销的记忆更新")
        before = json.loads(operation["before_json"])
        after = json.loads(operation["after_json"])
        affected_ids = {row["id"] for row in before + after}
        conn.execute("BEGIN IMMEDIATE")
        for memory_id in affected_ids:
            conn.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
        restore_rows(conn, before)
        conn.execute("DELETE FROM operations WHERE op_id = ?", (operation["op_id"],))
        conn.commit()
    emit({"ok": True, "undone_op_id": operation["op_id"]})


def list_memories(conn: sqlite3.Connection, subject_id: str | None) -> list[dict[str, Any]]:
    if subject_id:
        subjects = ["user"] if subject_id == "user" else ["user", *subject_variants(subject_id)]
        placeholders, subject_args = sql_in(subjects)
        rows = conn.execute(
            f"SELECT * FROM memories WHERE subject_id IN ({placeholders}) AND status = 'active' "
            "ORDER BY CASE scope WHEN 'user' THEN 0 WHEN 'account' THEN 1 "
            "WHEN 'contact' THEN 2 WHEN 'deal' THEN 3 WHEN 'event' THEN 4 ELSE 5 END, updated_at DESC",
            subject_args,
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM memories WHERE status = 'active' ORDER BY updated_at DESC"
        ).fetchall()
    return [row_dict(row) for row in rows]


def command_show(args: argparse.Namespace) -> None:
    subject_id = (
        canonical_subject_id(args.subject_id, allow_user=True) if args.subject_id else None
    )
    with connect() as conn:
        rows = list_memories(conn, subject_id)
    emit({"count": len(rows), "memories": rows})


def command_clients(_: argparse.Namespace) -> None:
    with connect() as conn:
        rows = conn.execute(
            "SELECT subject_id, MAX(updated_at) AS updated_at, COUNT(*) AS memory_count "
            "FROM memories WHERE subject_id != 'user' AND status = 'active' "
            "GROUP BY subject_id ORDER BY updated_at DESC"
        ).fetchall()
        grouped: dict[str, dict[str, Any]] = {}
        for row in rows:
            try:
                canonical = canonical_subject_id(row["subject_id"])
            except MemoryErrorWithCode:
                continue
            item = grouped.setdefault(
                canonical,
                {"subject_id": canonical, "memory_count": 0, "updated_at": row["updated_at"]},
            )
            item["memory_count"] += row["memory_count"]
            if row["updated_at"] > item["updated_at"]:
                item["updated_at"] = row["updated_at"]
        clients = []
        for canonical, item in grouped.items():
            variants = subject_variants(canonical)
            placeholders, variant_args = sql_in(variants)
            label = conn.execute(
                f"SELECT value FROM memories WHERE subject_id IN ({placeholders}) "
                "AND scope = 'account' AND field = 'display_label' AND status = 'active' "
                "ORDER BY updated_at DESC LIMIT 1",
                variant_args,
            ).fetchone()
            clients.append(
                {
                    **item,
                    "display_label": label["value"] if label else None,
                }
            )
        clients.sort(key=lambda item: item["updated_at"], reverse=True)
    emit(
        {
            "capacity": MAX_CLIENTS,
            "used": len(clients),
            "available": MAX_CLIENTS - len(clients),
            "clients": clients,
        }
    )


def command_context(args: argparse.Namespace) -> None:
    subject_id = canonical_subject_id(args.subject_id)
    with connect() as conn:
        require_enabled(conn)
        rows = list_memories(conn, subject_id)
    events = [row for row in rows if row["scope"] == "event"][:8]
    rows = [row for row in rows if row["scope"] != "event"] + events
    compact = [
        {
            "id": row["id"],
            "scope": row["scope"],
            "subject_id": row["subject_id"],
            "field": row["field"],
            "value": row["value"],
            "occurred_at": row["occurred_at"],
            "observed_at": row["observed_at"],
            "confidence": row["confidence"],
            "source_type": row["source_type"],
            "source_ref": row["source_ref"],
        }
        for row in rows
    ]
    while compact and len(json.dumps(compact, ensure_ascii=False)) > args.max_chars:
        compact.pop()
    emit({"count": len(compact), "max_chars": args.max_chars, "memories": compact})


def command_forget_client(args: argparse.Namespace) -> None:
    if not args.confirm:
        raise MemoryErrorWithCode("CONFIRMATION_REQUIRED", "需要 --confirm 才能永久删除客户档案")
    subject_id = canonical_subject_id(args.subject_id)
    variants = subject_variants(subject_id)
    placeholders, variant_args = sql_in(variants)
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        deleted = conn.execute(
            f"SELECT COUNT(*) AS n FROM memories WHERE subject_id IN ({placeholders})",
            variant_args,
        ).fetchone()["n"]
        conn.execute(
            f"DELETE FROM memories WHERE subject_id IN ({placeholders})", variant_args
        )
        conn.execute("DELETE FROM operations")
        conn.commit()
        conn.execute("VACUUM")
    emit({"ok": True, "subject_id": subject_id, "deleted": deleted, "undo_history_cleared": True})


def command_revoke(args: argparse.Namespace) -> None:
    if not args.confirm:
        raise MemoryErrorWithCode("CONFIRMATION_REQUIRED", "需要 --confirm 才能撤回长期记忆同意")
    if args.delete:
        target = db_path()
        if target.exists():
            target.unlink()
        for suffix in ("-wal", "-shm"):
            sidecar = Path(str(target) + suffix)
            if sidecar.exists():
                sidecar.unlink()
        emit({"ok": True, "consent_enabled": False, "deleted": True})
        return
    with connect() as conn:
        set_setting(conn, "consent_enabled", "false")
        set_setting(conn, "paused", "true")
        conn.commit()
    emit({"ok": True, "consent_enabled": False, "deleted": False})


def command_clear(args: argparse.Namespace) -> None:
    if not args.confirm:
        raise MemoryErrorWithCode("CONFIRMATION_REQUIRED", "需要 --confirm 才能永久清空长期记忆")
    target = db_path()
    if target.exists():
        target.unlink()
    for suffix in ("-wal", "-shm"):
        sidecar = Path(str(target) + suffix)
        if sidecar.exists():
            sidecar.unlink()
    emit({"ok": True, "deleted": True})


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("status").set_defaults(func=command_status)
    enable = subparsers.add_parser("enable")
    enable.add_argument("--confirm", action="store_true")
    enable.set_defaults(func=command_enable)
    subparsers.add_parser("pause").set_defaults(func=command_pause)
    subparsers.add_parser("resume").set_defaults(func=command_resume)

    apply_cmd = subparsers.add_parser("apply")
    source = apply_cmd.add_mutually_exclusive_group()
    source.add_argument("--json")
    source.add_argument("--file")
    apply_cmd.set_defaults(func=command_apply)

    undo = subparsers.add_parser("undo")
    undo.add_argument("--op-id")
    undo.set_defaults(func=command_undo)

    show = subparsers.add_parser("show")
    show.add_argument("--subject-id")
    show.set_defaults(func=command_show)
    subparsers.add_parser("clients").set_defaults(func=command_clients)
    context = subparsers.add_parser("context")
    context.add_argument("--subject-id", required=True)
    context.add_argument("--max-chars", type=int, default=4000, choices=range(500, 8001))
    context.set_defaults(func=command_context)

    forget = subparsers.add_parser("forget-client")
    forget.add_argument("subject_id")
    forget.add_argument("--confirm", action="store_true")
    forget.set_defaults(func=command_forget_client)

    revoke = subparsers.add_parser("revoke")
    revoke.add_argument("--delete", action="store_true")
    revoke.add_argument("--confirm", action="store_true")
    revoke.set_defaults(func=command_revoke)
    clear = subparsers.add_parser("clear")
    clear.add_argument("--confirm", action="store_true")
    clear.set_defaults(func=command_clear)
    return parser


def main() -> int:
    try:
        args = build_parser().parse_args()
        args.func(args)
        return 0
    except (MemoryErrorWithCode, json.JSONDecodeError, OSError, sqlite3.Error) as exc:
        code = exc.code if isinstance(exc, MemoryErrorWithCode) else exc.__class__.__name__.upper()
        emit({"ok": False, "error": {"code": code, "message": str(exc)}})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
