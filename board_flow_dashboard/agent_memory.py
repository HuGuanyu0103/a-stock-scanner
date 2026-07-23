#!/usr/bin/env python3
"""
观澜 Agent 记忆层

把对话记忆从「前端每次传 chat_history」升级为「服务端持久化」，并新增
用户画像记忆，让 Agent 跨会话「记得你」（持仓风格、常问的票）。

两张表：
  - conversations：会话消息（按 session_id 分组，服务端持久化，刷新不失忆）
  - user_profile：用户画像（风格偏好、关注的股票，注入 Agent 上下文实现个性化）

复用系统统一的 SQLite + 线程锁范式（与 decision_store 一致）。
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).parent / "data"
DB_PATH = DATA_DIR / "agent_memory.db"

MAX_TURNS = 20  # 每个会话保留最近 N 条消息


class AgentMemory:
    def __init__(self):
        self._lock = threading.Lock()
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _get_conn(self):
        conn = sqlite3.connect(str(DB_PATH))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_db(self):
        with self._get_conn() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS conversations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at TEXT DEFAULT (datetime('now','localtime'))
                );
                CREATE INDEX IF NOT EXISTS idx_conv_session
                    ON conversations(session_id, id);
                CREATE TABLE IF NOT EXISTS user_profile (
                    key TEXT PRIMARY KEY,
                    value TEXT,
                    updated_at TEXT DEFAULT (datetime('now','localtime'))
                );
                """
            )

    # ── 会话记忆 ──────────────────────────────────────────────
    def append_message(self, session_id: str, role: str, content: str):
        if not session_id or not content:
            return
        with self._lock, self._get_conn() as conn:
            conn.execute(
                "INSERT INTO conversations (session_id, role, content) VALUES (?,?,?)",
                (session_id, role, content[:4000]))

    def get_history(self, session_id: str, limit: int = MAX_TURNS) -> list[dict]:
        """取最近 limit 条消息，按时间正序返回 [{role, content}]。"""
        if not session_id:
            return []
        with self._lock, self._get_conn() as conn:
            rows = conn.execute(
                "SELECT role, content FROM conversations WHERE session_id=? "
                "ORDER BY id DESC LIMIT ?", (session_id, limit)).fetchall()
        return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]

    def clear_session(self, session_id: str):
        with self._lock, self._get_conn() as conn:
            conn.execute("DELETE FROM conversations WHERE session_id=?", (session_id,))

    # ── 用户画像 ──────────────────────────────────────────────
    def set_profile(self, key: str, value):
        with self._lock, self._get_conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO user_profile (key, value, updated_at) "
                "VALUES (?,?,datetime('now','localtime'))",
                (key, json.dumps(value, ensure_ascii=False)))

    def get_profile(self, key: str, default=None):
        with self._lock, self._get_conn() as conn:
            row = conn.execute(
                "SELECT value FROM user_profile WHERE key=?", (key,)).fetchone()
        if not row:
            return default
        try:
            return json.loads(row["value"])
        except Exception:
            return default

    def note_asked_stock(self, code: str, name: str = ""):
        """记录用户问过的股票（用于画像：常关注的票）。"""
        if not code:
            return
        asked = self.get_profile("asked_stocks", []) or []
        asked = [a for a in asked if a.get("code") != code]  # 去重
        asked.insert(0, {"code": code, "name": name})
        self.set_profile("asked_stocks", asked[:20])  # 最多留 20 只

    def get_profile_context(self) -> str:
        """把用户画像拼成可注入 Agent 的上下文文本。无画像返回空串。"""
        parts = []
        style = self.get_profile("style")
        if style:
            parts.append(f"用户交易风格: {style}")
        asked = self.get_profile("asked_stocks", []) or []
        if asked:
            names = "、".join(a.get("name") or a.get("code") for a in asked[:8])
            parts.append(f"用户近期关注: {names}")
        return "；".join(parts)


_memory: Optional[AgentMemory] = None


def get_agent_memory() -> AgentMemory:
    global _memory
    if _memory is None:
        _memory = AgentMemory()
    return _memory
