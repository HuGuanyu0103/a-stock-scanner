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
                CREATE TABLE IF NOT EXISTS session_summary (
                    session_id TEXT PRIMARY KEY,
                    summary TEXT NOT NULL,
                    covered_up_to INTEGER NOT NULL DEFAULT 0,
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
            conn.execute("DELETE FROM session_summary WHERE session_id=?", (session_id,))

    # ── 滚动摘要（解决长对话超出 recent window 后失忆）──────────────
    def get_summary(self, session_id: str) -> str:
        """取该会话已压缩的历史摘要（无则空串）。"""
        if not session_id:
            return ""
        with self._lock, self._get_conn() as conn:
            row = conn.execute(
                "SELECT summary FROM session_summary WHERE session_id=?",
                (session_id,)).fetchone()
        return row["summary"] if row else ""

    def maybe_summarize(self, session_id: str, summarizer,
                        keep_recent: int = 10, trigger_at: int = 16):
        """当会话消息数超过 trigger_at 时，把「较早的、尚未纳入摘要的」消息压成摘要。

        - summarizer: Callable[[str, str], str]，入参 (已有摘要, 新增待压缩文本)，返回新摘要。
          由调用方注入（避免 agent_memory 反向依赖 agent，防循环 import）。
        - keep_recent: 保留最近 N 条不压缩（仍走原始 history）。
        - 只压缩 id 大于 covered_up_to、且在 keep_recent 之前的消息，天然增量。
        """
        if not session_id or summarizer is None:
            return
        with self._lock, self._get_conn() as conn:
            total = conn.execute(
                "SELECT COUNT(*) c FROM conversations WHERE session_id=?",
                (session_id,)).fetchone()["c"]
            if total <= trigger_at:
                return
            row = conn.execute(
                "SELECT covered_up_to, summary FROM session_summary WHERE session_id=?",
                (session_id,)).fetchone()
            covered = row["covered_up_to"] if row else 0
            old_summary = row["summary"] if row else ""
            # 找到「最近 keep_recent 条」的起始 id：这之前的才可压缩
            recent_ids = conn.execute(
                "SELECT id FROM conversations WHERE session_id=? ORDER BY id DESC LIMIT ?",
                (session_id, keep_recent)).fetchall()
            if len(recent_ids) < keep_recent:
                return
            recent_floor = recent_ids[-1]["id"]  # 最近窗口里最小的 id
            rows = conn.execute(
                "SELECT id, role, content FROM conversations "
                "WHERE session_id=? AND id>? AND id<? ORDER BY id ASC",
                (session_id, covered, recent_floor)).fetchall()
            if not rows:
                return
            new_max_id = rows[-1]["id"]
            to_compress = "\n".join(f"{r['role']}: {r['content']}" for r in rows)

        # 生成摘要在锁外做（可能调 LLM，耗时）
        try:
            new_summary = summarizer(old_summary, to_compress)
        except Exception as e:
            logger.warning("会话摘要生成失败: %s", e)
            return
        if not new_summary:
            return
        with self._lock, self._get_conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO session_summary "
                "(session_id, summary, covered_up_to, updated_at) "
                "VALUES (?,?,?,datetime('now','localtime'))",
                (session_id, new_summary[:4000], new_max_id))

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

    # 交易风格关键词 → 画像标签（轻量规则抽取，无需额外 LLM 调用）
    _STYLE_PATTERNS = [
        ("超短线", ["超短", "打板", "打首板", "接力", "隔日", "t+0", "做t", "龙头战法"]),
        ("短线波段", ["短线", "波段", "3-5天", "做波段", "低吸高抛"]),
        ("稳健低频", ["稳健", "价值", "长线", "中长线", "定投", "不看盘", "保守"]),
        ("追涨型", ["追涨", "追高", "打涨停", "强势股"]),
        ("左侧抄底", ["抄底", "低吸", "左侧", "补跌", "越跌越买"]),
    ]

    def infer_and_note_style(self, message: str) -> Optional[str]:
        """从用户单条消息里轻量抽取交易风格并写入画像。

        命中则更新 style（含命中计数，多次出现的风格权重更高）。返回本次命中的风格或 None。
        让 get_profile_context 的 style 分支不再是死代码。
        """
        if not message:
            return None
        msg = message.lower()
        hit = None
        for style, kws in self._STYLE_PATTERNS:
            if any(kw in msg for kw in kws):
                hit = style
                break
        if not hit:
            return None
        # 记命中计数，取累计最高频的风格作为画像
        counts = self.get_profile("style_counts", {}) or {}
        counts[hit] = counts.get(hit, 0) + 1
        self.set_profile("style_counts", counts)
        dominant = max(counts.items(), key=lambda x: x[1])[0]
        self.set_profile("style", dominant)
        return hit

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
