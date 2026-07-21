#!/usr/bin/env python3
"""
持仓盯盘助手 — 主动盯盘 + 触发提醒（决策链 ④⑤ 段）

在系统已选出票、用户已买入之后，主动帮用户盯盘：
  - 持仓录入 + 点位管理（自动按信号内置止盈止损，支持手动覆盖，支持手动加任意票）
  - 盘中盯盘循环（交易时段轮询实时价，复用 collector 后台线程范式）
  - 触发判断（止损前 0.8% 预警 + 触线硬触发 + 资金异动/板块退潮辅助提示）
  - 微信推送 + AI 观澜决策话术

设计原则：
  - 复用 decision_store 的 SQLite/结算/胜率底座，仅新增 watch_positions 表承载「盯盘点位」
  - 复用 push.send 推送 + 告警去重范式
  - 复用 data_fetcher 的实时行情(_fetch_tencent_market) 与交易时段判断(_is_trading_time)
  - 复用 stock_selector.SIGNAL_GUIDE 的信号内置止盈止损点位

用法:
  from position_watcher import get_position_watcher
  pw = get_position_watcher()
  pw.add_position(code="600519", name="贵州茅台", cost=1680.0, signal="放量上攻")
  pw.start()   # 启动盘中盯盘后台线程
"""

from __future__ import annotations

import logging
import re
import sqlite3
import threading
import time
from datetime import date, datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).parent / "data"
DB_PATH = DATA_DIR / "watch_positions.db"

# ── 盯盘参数 ──────────────────────────────────────────────────
WATCH_INTERVAL = 45.0            # 盘中轮询间隔（秒），介于及时性与 API 压力之间
STOP_WARN_BUFFER = 0.008         # 止损前预警缓冲：距止损线 0.8% 触发预警
ALERT_COOLDOWN_SEC = 1800        # 同一持仓同类提醒冷却（30 分钟），与 stock_selector 一致

# 手动添加、无对应系统信号时的默认点位（保守超短线设定）
DEFAULT_TAKE_PROFIT_PCT = 6.0    # 默认止盈 +6%
DEFAULT_STOP_LOSS_PCT = -4.0     # 默认止损 -4%


def _parse_pct_range(text: str) -> Optional[float]:
    """把 SIGNAL_GUIDE 里的止盈止损文本解析为百分比数值。

    支持格式："4-6%" → 取上沿 6.0；"5%" → 5.0；"-3%" → -3.0；"-" → None。
    止盈取区间上沿（更宽松，到位才提醒）；止损文本本身即为负数。
    """
    if not text or text.strip() == "-":
        return None
    nums = re.findall(r"-?\d+\.?\d*", text)
    if not nums:
        return None
    vals = [float(n) for n in nums]
    # 止损文本以 '-' 开头 → 取更深处（绝对值最大，带负号）
    if text.strip().startswith("-"):
        return -max(abs(v) for v in vals)
    # 止盈区间（如 "4-6%"）连字符是范围分隔符，取绝对值上沿
    return max(abs(v) for v in vals)


def _default_stops_for_signal(signal: str) -> tuple[float, float]:
    """按买入信号返回 (止盈%, 止损%)。无匹配信号则用默认值。"""
    try:
        try:
            from .stock_selector import SIGNAL_GUIDE  # type: ignore
        except ImportError:
            from stock_selector import SIGNAL_GUIDE  # type: ignore
    except Exception:
        SIGNAL_GUIDE = {}

    guide = SIGNAL_GUIDE.get(signal or "", {})
    tp = _parse_pct_range(guide.get("take_profit", "")) if guide else None
    sl = _parse_pct_range(guide.get("stop_loss", "")) if guide else None
    tp = tp if tp is not None else DEFAULT_TAKE_PROFIT_PCT
    sl = sl if sl is not None else DEFAULT_STOP_LOSS_PCT
    return tp, sl


class PositionWatcher:
    """持仓盯盘助手。

    独立 SQLite 表 watch_positions 存储盯盘点位；实时行情/推送/交易时段
    判断均复用系统已有能力，不重复造轮子。
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._alert_cooldown: dict[str, float] = {}  # key=f"{code}:{kind}" → 上次推送时间
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self._init_db()

    # ── DB ────────────────────────────────────────────────────
    def _get_conn(self):
        conn = sqlite3.connect(str(DB_PATH))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_db(self):
        with self._get_conn() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS watch_positions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    stock_code TEXT NOT NULL,
                    stock_name TEXT DEFAULT '',
                    sector TEXT DEFAULT '',
                    signal TEXT DEFAULT '',
                    cost REAL NOT NULL,
                    take_profit_pct REAL,
                    stop_loss_pct REAL,
                    target_price REAL,
                    stop_price REAL,
                    status TEXT DEFAULT 'watching',
                    source TEXT DEFAULT 'manual',
                    created_at TEXT DEFAULT (datetime('now','localtime'))
                );
                CREATE INDEX IF NOT EXISTS idx_watch_status
                    ON watch_positions(status);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_watch_code_active
                    ON watch_positions(stock_code, status);
                """
            )

    # ── 持仓录入与点位管理 ────────────────────────────────────
    def add_position(self, code: str, name: str = "", cost: float = 0.0,
                     signal: str = "", sector: str = "",
                     take_profit_pct: Optional[float] = None,
                     stop_loss_pct: Optional[float] = None,
                     source: str = "manual") -> dict:
        """新增/更新一条盯盘持仓。

        点位策略：自动按信号给默认 + 用户可手动覆盖（方案 C）。
        支持手动添加任意股票（不限于系统推荐的票）。
        """
        code = str(code).strip()
        if not re.match(r"^\d{6}$", code):
            return {"ok": False, "error": "股票代码格式错误（需 6 位数字）"}
        if not cost or cost <= 0:
            return {"ok": False, "error": "请提供有效成本价"}

        # 点位：手动优先，否则按信号自动
        auto_tp, auto_sl = _default_stops_for_signal(signal)
        tp_pct = take_profit_pct if take_profit_pct is not None else auto_tp
        sl_pct = stop_loss_pct if stop_loss_pct is not None else auto_sl
        target_price = round(cost * (1 + tp_pct / 100), 3)
        stop_price = round(cost * (1 + sl_pct / 100), 3)

        with self._lock, self._get_conn() as conn:
            # 同一代码若已在盯盘，先关闭旧记录（唯一索引约束）
            conn.execute(
                "UPDATE watch_positions SET status='replaced' "
                "WHERE stock_code=? AND status='watching'", (code,))
            cur = conn.execute(
                "INSERT INTO watch_positions "
                "(stock_code,stock_name,sector,signal,cost,take_profit_pct,"
                "stop_loss_pct,target_price,stop_price,status,source,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,'watching',?,datetime('now','localtime'))",
                (code, name, sector, signal, cost, tp_pct, sl_pct,
                 target_price, stop_price, source))
            pid = cur.lastrowid
        logger.info("盯盘录入: %s %s 成本%.2f 止盈%.2f(%.1f%%) 止损%.2f(%.1f%%)",
                    code, name, cost, target_price, tp_pct, stop_price, sl_pct)
        return {"ok": True, "id": pid, "code": code, "name": name,
                "cost": cost, "target_price": target_price,
                "stop_price": stop_price, "take_profit_pct": tp_pct,
                "stop_loss_pct": sl_pct}

    def update_stops(self, pid: int, take_profit_pct: Optional[float] = None,
                     stop_loss_pct: Optional[float] = None) -> dict:
        """手动修改点位（重算目标价/止损价）。"""
        with self._lock, self._get_conn() as conn:
            row = conn.execute(
                "SELECT cost FROM watch_positions WHERE id=? AND status='watching'",
                (pid,)).fetchone()
            if not row:
                return {"ok": False, "error": "持仓不存在或已了结"}
            cost = row["cost"]
            sets, args = [], []
            if take_profit_pct is not None:
                sets += ["take_profit_pct=?", "target_price=?"]
                args += [take_profit_pct, round(cost * (1 + take_profit_pct / 100), 3)]
            if stop_loss_pct is not None:
                sets += ["stop_loss_pct=?", "stop_price=?"]
                args += [stop_loss_pct, round(cost * (1 + stop_loss_pct / 100), 3)]
            if not sets:
                return {"ok": False, "error": "无可更新点位"}
            args.append(pid)
            conn.execute(
                f"UPDATE watch_positions SET {','.join(sets)} WHERE id=?", args)
        return {"ok": True, "id": pid}

    def remove_position(self, pid: int) -> dict:
        with self._lock, self._get_conn() as conn:
            conn.execute(
                "UPDATE watch_positions SET status='closed' WHERE id=?", (pid,))
        return {"ok": True, "id": pid}

    def get_positions(self) -> list[dict]:
        with self._lock, self._get_conn() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM watch_positions WHERE status='watching' "
                "ORDER BY created_at DESC").fetchall()]

    def get_positions_enriched(self) -> list[dict]:
        """带实时价与浮盈的持仓列表（供前端盯盘卡片展示现价/盈亏）。

        取价失败时 price 为 None，前端优雅降级为「--」。
        """
        positions = self.get_positions()
        if not positions:
            return []
        prices = self._fetch_prices([p["stock_code"] for p in positions])
        for p in positions:
            price = prices.get(p["stock_code"])
            p["current_price"] = round(price, 3) if price else None
            cost = p.get("cost") or 0
            if price and cost:
                p["pnl_pct"] = round((price - cost) / cost * 100, 2)
                # 现价在「止损→止盈」标尺上的位置(0~1)，供前端进度条
                stop = p.get("stop_price") or 0
                target = p.get("target_price") or 0
                if target > stop:
                    p["track_pos"] = max(0.0, min(1.0,
                                                  (price - stop) / (target - stop)))
            else:
                p["pnl_pct"] = None
        return positions

    # ── 盯盘循环 ──────────────────────────────────────────────
    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._watch_loop, daemon=True, name="position-watcher")
        self._thread.start()
        logger.info("持仓盯盘助手已启动 (间隔 %.0fs)", WATCH_INTERVAL)

    def stop(self):
        self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5.0)

    def _watch_loop(self):
        # 复用系统统一的交易时段判断
        try:
            try:
                from .data_fetcher import _is_trading_time  # type: ignore
            except ImportError:
                from data_fetcher import _is_trading_time  # type: ignore
        except Exception:
            _is_trading_time = lambda: False  # noqa: E731

        while self._running:
            try:
                if _is_trading_time():
                    self._scan_once()
            except Exception as e:
                logger.warning("盯盘循环异常: %s", e)
            time.sleep(WATCH_INTERVAL)

    def _scan_once(self):
        """扫描所有盯盘持仓，判断触发并推送。"""
        positions = self.get_positions()
        if not positions:
            return

        prices = self._fetch_prices([p["stock_code"] for p in positions])
        if not prices:
            return

        for p in positions:
            price = prices.get(p["stock_code"])
            if not price:
                continue
            self._evaluate(p, price)

    def _fetch_prices(self, codes: list[str]) -> dict[str, float]:
        """批量取实时价，复用腾讯行情降级接口。"""
        try:
            try:
                from .data_fetcher import _fetch_tencent_market  # type: ignore
            except ImportError:
                from data_fetcher import _fetch_tencent_market  # type: ignore
            df = _fetch_tencent_market(codes)
            if df is None or df.empty:
                return {}
            out = {}
            for _, r in df.iterrows():
                c = str(r.get("stock_code", "")).zfill(6)
                out[c] = float(r.get("price", 0) or 0)
            return out
        except Exception as e:
            logger.debug("盯盘取价失败: %s", e)
            return {}

    def _evaluate(self, pos: dict, price: float):
        """对单只持仓判断触发条件。

        优先级：硬触发(止盈/止损) > 止损前预警。
        资金异动/板块退潮作为辅助提示叠加在提醒文本中（不独立触发）。
        """
        cost = pos["cost"]
        target = pos.get("target_price") or 0
        stop = pos.get("stop_price") or 0
        pnl_pct = round((price - cost) / cost * 100, 2) if cost else 0

        kind = None
        action = None
        if target and price >= target:
            kind, action = "take_profit", "止盈：达到目标价，建议减仓/止盈"
        elif stop and price <= stop:
            kind, action = "stop_loss", "止损：触及止损线，建议卖出"
        elif stop and price <= stop * (1 + STOP_WARN_BUFFER):
            # 距止损线 0.8% 内 → 提前预警（仅止损方向做提前预警）
            kind, action = "stop_warn", "预警：接近止损线，注意风险、准备减仓"

        if not kind:
            return
        if not self._cooldown_ok(pos["stock_code"], kind):
            return

        aux = self._aux_hints(pos)
        self._push_alert(pos, price, pnl_pct, kind, action, aux)

        # P0-4: 硬触发(止盈/止损)= 一次完整决策了结，写入决策闭环参与胜率统计
        if kind in ("take_profit", "stop_loss"):
            self._record_to_loop(pos, price)

    def _record_to_loop(self, pos: dict, exit_price: float):
        """把触发了结的持仓写入 decision_store（record + 立即结算），进入胜率飞轮。

        盯盘助手是「选→盯→结算→统计」闭环的最后一环：一旦触及止盈/止损，
        意味着一笔完整交易结束，据此为决策闭环补充真实样本。
        """
        try:
            try:
                from .decision_store import get_decision_store  # type: ignore
            except ImportError:
                from decision_store import get_decision_store  # type: ignore
            ds = get_decision_store()
            did = ds.record_decision(
                stock_code=pos["stock_code"],
                stock_name=pos.get("stock_name", ""),
                entry_price=pos["cost"],
                sector=pos.get("sector", ""),
                pool=pos.get("pool", ""),
                signal=pos.get("signal", ""),
                source="watcher",
            )
            ds.mark_exited(did, exit_price, notes=" via 盯盘触发")
            # 该持仓已了结，停止继续盯盘
            self.remove_position(pos["id"])
            logger.info("盯盘了结入闭环: %s 成本%.2f 卖出%.2f",
                        pos["stock_code"], pos["cost"], exit_price)
        except Exception as e:
            logger.warning("盯盘了结写入决策闭环失败: %s", e)

    def _cooldown_ok(self, code: str, kind: str) -> bool:
        key = f"{code}:{kind}"
        now = time.time()
        if now - self._alert_cooldown.get(key, 0) < ALERT_COOLDOWN_SEC:
            return False
        self._alert_cooldown[key] = now
        return True

    def _aux_hints(self, pos: dict) -> str:
        """辅助提示：所属板块资金退潮。资金异动/板块退潮为辅助，非主判据。"""
        sector = pos.get("sector", "")
        if not sector:
            return ""
        try:
            try:
                from .app import collector  # type: ignore
            except ImportError:
                from app import collector  # type: ignore
            ts = collector.get_sector_timeseries(sector, recent_minutes=30)
            trend = ts.get("trend", "")
            if trend in ("加速流出", "温和流出"):
                return f"辅助信号：所属板块「{sector}」资金{trend}"
        except Exception:
            pass
        return ""

    def _push_alert(self, pos: dict, price: float, pnl_pct: float,
                    kind: str, action: str, aux: str):
        """推送微信提醒 + AI 观澜决策话术。"""
        name = pos.get("stock_name") or pos.get("stock_code")
        icon = {"take_profit": "🎯", "stop_loss": "🛑", "stop_warn": "⚠️"}.get(kind, "🔔")
        title = f"{icon} 盯盘提醒 · {name} {action.split('：')[0]}"

        lines = [
            f"标的: {name}({pos['stock_code']})",
            f"现价: {price:.2f}  成本: {pos['cost']:.2f}  盈亏: {pnl_pct:+.2f}%",
            f"止盈: {pos.get('target_price')}  止损: {pos.get('stop_price')}",
            f"动作建议: {action}",
        ]
        if aux:
            lines.append(aux)

        # AI 观澜决策话术（失败则跳过，不阻断提醒）
        talk = self._ai_talk(pos, price, pnl_pct, kind, aux)
        if talk:
            lines.append(f"观澜: {talk}")

        content = "\n".join(lines)
        try:
            try:
                from .push import send as push_send  # type: ignore
            except ImportError:
                import os as _os
                import sys as _sys
                _parent = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
                if _parent not in _sys.path:
                    _sys.path.insert(0, _parent)
                from push import send as push_send  # type: ignore
            push_send(title, content)
            logger.info("盯盘提醒已推送: %s %s", title, pos["stock_code"])
        except Exception as e:
            logger.warning("盯盘提醒推送失败: %s", e)

    def _ai_talk(self, pos: dict, price: float, pnl_pct: float,
                 kind: str, aux: str) -> str:
        """让 AI 观澜为提醒配一句决策话术。失败返回空串。"""
        try:
            try:
                from .agent import get_agent  # type: ignore
            except ImportError:
                from agent import get_agent  # type: ignore
        except Exception:
            return ""
        try:
            reason = {
                "take_profit": "已到止盈目标",
                "stop_loss": "已触止损线",
                "stop_warn": "接近止损线",
            }.get(kind, "")
            prompt = (
                f"持仓 {pos.get('stock_name')}({pos['stock_code']}) 现价{price:.2f}"
                f"，成本{pos['cost']:.2f}，盈亏{pnl_pct:+.2f}%，{reason}。"
                f"{aux}。用一句话（30字内）给出持股决策建议（持有/加仓/减仓/清仓），不要客套。")
            agent = get_agent()
            # 复用 agent.chat 轻量问答（持股决策类问题会自动匹配对应 prompt）
            reply = agent.chat(
                user_message=prompt,
                candidates=[],
                hot_sectors=[],
                signals={},
                resolved_code=pos["stock_code"],
            )
            if reply:
                return str(reply).strip().replace("\n", " ")[:60]
        except Exception as e:
            logger.debug("AI 话术生成跳过: %s", e)
        return ""


_watcher: Optional[PositionWatcher] = None


def get_position_watcher() -> PositionWatcher:
    global _watcher
    if _watcher is None:
        _watcher = PositionWatcher()
    return _watcher
