#!/usr/bin/env python3
"""
周期板块回溯模块 — 筛选近 2-3 日强势但当日回调的板块（分歧低吸机会）

供 stock_selector.py 的 B 池（回调低吸池）使用。

数据来源:
  1. collector 内存 → 当日实时板块排名
  2. SQLite (collector.db) → 前 2 日板块收盘快照

核心逻辑:
  近 3 日累计资金流入大 + 累计涨幅高 → 确认是主线
  当日资金流出/板块收跌 → 主线分歧回调 → 低吸买点
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# ── 回调筛选参数 ──────────────────────────────────────────────

PULLBACK_LOOKBACK_DAYS = 3        # 回溯 N 个交易日
PULLBACK_ACCUMULATED_FLOW = 5.0   # 近 3 日累计主力净流入 >= N 亿
PULLBACK_ACCUMULATED_RETURN = 8.0 # 近 3 日累计涨幅 >= N%
PULLBACK_DAILY_RETURN_MAX = -1.2  # 当日涨跌幅上限（≤ -1.2% 才算回调）
PULLBACK_DAILY_RETURN_MIN = -6.0  # 当日最大跌幅（超过则视为破位，放弃）
PULLBACK_MAX_SECTORS = 5          # 最多输出板块数
PULLBACK_MIN_HOT_RANK = 50        # 过去 3 天至少有一天排名 ≤ N


# ── 板块摘要持久化 ────────────────────────────────────────────

DATA_DIR = Path(__file__).parent / "data"
SUMMARY_PREFIX = "sector_summary_"


def _today_str() -> str:
    return date.today().isoformat()


def save_daily_summary(date_str: str, sectors: list[dict]):
    """保存当日收盘板块摘要 JSON，供次日回溯使用。"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    path = DATA_DIR / f"{SUMMARY_PREFIX}{date_str}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(sectors, f, ensure_ascii=False)


def _load_daily_summary(date_str: str) -> dict[str, dict]:
    """加载某日板块摘要。

    Returns:
        {sector_name: {"net_main": float, "pct_chg": float, "rank": int}}
    """
    path = DATA_DIR / f"{SUMMARY_PREFIX}{date_str}.json"
    if not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            items = json.load(f)
        result = {}
        for i, item in enumerate(items):
            name = item.get("name", "")
            if name:
                result[name] = {
                    "net_main": item.get("net_main", 0),
                    "pct_chg": item.get("pct_chg", 0),
                    "rank": i + 1,
                }
        return result
    except Exception:
        return {}


def _load_from_sqlite(db_path: Path, date_str: str) -> dict[str, dict]:
    """从 SQLite 加载某日最后一条板块快照作为收盘数据。"""
    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT data FROM concept_snapshots WHERE date = ? ORDER BY id DESC LIMIT 1",
            (date_str,),
        ).fetchall()
        conn.close()

        if not rows:
            return {}

        raw = json.loads(rows[0]["data"])
        if isinstance(raw, dict):
            rank = raw.get("rank", [])
        elif isinstance(raw, list):
            rank = raw
        else:
            return {}

        result = {}
        for i, item in enumerate(rank):
            name = item.get("name", "")
            if name:
                result[name] = {
                    "net_main": item.get("net_main", 0),
                    "pct_chg": item.get("pct_chg", 0),
                    "rank": i + 1,
                }
        return result
    except Exception as e:
        logger.warning("从 SQLite 加载 %s 失败: %s", date_str, e)
        return {}


def _get_trading_dates(n: int = 3) -> list[str]:
    """回溯最近 n 个交易日（跳过周末）。"""
    dates = []
    d = date.today()
    while len(dates) < n:
        d_str = d.isoformat()
        if d.weekday() < 5:
            dates.append(d_str)
        d -= timedelta(days=1)
    return dates


# ── 核心：回调板块筛选 ────────────────────────────────────────

class SectorReviewer:
    """周期板块回溯分析器。

    使用方式:
        reviewer = SectorReviewer()
        pullback_sectors = reviewer.get_pullback_sectors(collector=collector)
    """

    def __init__(self, db_path: Optional[Path] = None):
        if db_path is None:
            db_path = DATA_DIR / "collector.db"
        self._db_path = db_path
        self._cache: dict = {}
        self._cache_date: str = ""

    def get_pullback_sectors(self, collector=None,
                              top_n: int = PULLBACK_MAX_SECTORS) -> list[dict]:
        """获取回调潜力板块列表。

        Args:
            collector: SectorFlowCollector 实例（用于获取当日实时排名）
            top_n: 最多返回板块数

        Returns:
            [{
                "name": str, "code": str, "type": "concept",
                "accumulated_flow": float,   # 近 3 日累计主力净流入（亿）
                "accumulated_return": float,  # 近 3 日累计涨幅（%）
                "today_flow": float,          # 当日主力净流入（亿）
                "today_return": float,        # 当日涨跌幅（%）
                "pullback_score": float,      # 回调得分（越高越好）
            }, ...]
        """
        dates = _get_trading_dates(PULLBACK_LOOKBACK_DAYS)
        today_str = dates[0]
        past_dates = dates[1:]

        # ── 获取当日数据 ──────────────────────────────────────
        today_summary: dict[str, dict] = {}

        if collector:
            # 优先用 collector 全量板块数据（含 code/pct_chg）
            try:
                all_sectors = collector.get_all_sectors_data()
            except AttributeError:
                # 兼容旧版 collector（无此方法时降级）
                all_sectors = []
            for item in all_sectors:
                name = item.get("name", "")
                if name and name not in today_summary:
                    today_summary[name] = {
                        "net_main": item.get("net_main", 0),
                        "pct_chg": item.get("pct_chg", 0),
                        "rank": item.get("rank", 999),
                        "type": item.get("type", "concept"),
                        "code": item.get("code", ""),
                    }

        if not today_summary:
            # 降级：从 SQLite 获取
            today_summary = _load_from_sqlite(self._db_path, today_str)

        if not today_summary:
            today_summary = _load_daily_summary(today_str)

        if not today_summary:
            logger.warning("今日板块数据为空")
            return []

        # ── 获取历史数据 ──────────────────────────────────────
        past_summaries: list[dict[str, dict]] = []
        for d in past_dates:
            s = _load_daily_summary(d)
            if not s:
                s = _load_from_sqlite(self._db_path, d)
            past_summaries.append(s)

        # ── 计算每个板块的 3 日累计指标 ────────────────────────
        candidates = []

        for sector_name, today_data in today_summary.items():
            past_flow = 0.0
            past_return = 0.0
            was_hot = False

            for past in past_summaries:
                if sector_name in past:
                    pd = past[sector_name]
                    past_flow += pd.get("net_main", 0)
                    past_return += pd.get("pct_chg", 0)
                    if pd.get("rank", 999) <= PULLBACK_MIN_HOT_RANK:
                        was_hot = True

            today_flow = today_data.get("net_main", 0)
            today_return = today_data.get("pct_chg", 0)
            accumulated_flow = past_flow + today_flow
            accumulated_return = past_return + today_return

            # ── 筛选条件 ──────────────────────────────────────

            # 1. 近 3 日累计资金流入达标
            if accumulated_flow < PULLBACK_ACCUMULATED_FLOW:
                continue

            # 2. 近 3 日累计涨幅达标（确认为主线板块）
            if accumulated_return < PULLBACK_ACCUMULATED_RETURN:
                continue

            # 3. 必须曾是主线（有排名证据）
            if not was_hot:
                continue

            # 4. 当日回调确认（收跌 或 资金流出+收跌）
            is_pullback = (
                today_return <= PULLBACK_DAILY_RETURN_MAX or
                (today_flow < 0 and today_return < 0)
            )
            if not is_pullback:
                continue

            # 5. 跌幅不能太大（排除趋势破位）
            if today_return < PULLBACK_DAILY_RETURN_MIN:
                continue

            # ── 回调得分 ──────────────────────────────────────
            # 累积越强 + 回调越浅 = 越好
            pullback_score = round(
                min(accumulated_flow / 10, 10) * 0.4 +
                min(accumulated_return / 5, 10) * 0.3 +
                max(0, (1 - abs(today_return) / 6)) * 10 * 0.3, 1
            )

            candidates.append({
                "name": sector_name,
                "code": today_data.get("code", ""),
                "type": today_data.get("type", "concept"),
                "accumulated_flow": round(accumulated_flow, 2),
                "accumulated_return": round(accumulated_return, 1),
                "today_flow": round(today_flow, 2),
                "today_return": round(today_return, 2),
                "pullback_score": pullback_score,
            })

        candidates.sort(key=lambda x: x["pullback_score"], reverse=True)
        result = candidates[:top_n]

        if result:
            logger.info("回调板块: %d 个 → %s",
                         len(result),
                         ", ".join(f"{c['name']}(累计{c['accumulated_return']:+.1f}% 当日{c['today_return']:+.1f}%)"
                                  for c in result[:5]))

        return result


# ── 收盘摘要生成（供 app.py/定时任务调用）─────────────────────

def generate_daily_summary(collector=None):
    """生成当日收盘板块摘要 JSON，供次日回溯。

    在收盘后调用一次即可。如果 collector 可用则优先用实时数据，
    否则从 SQLite 读取最后一条快照。
    """
    today_str = _today_str()

    if collector:
        sectors = []
        for stype in ("concept", "industry"):
            data = collector.get_dashboard_data(sector_type=stype)
            rank = data.get("rank", [])
            for item in rank:
                sectors.append({
                    "name": item.get("name", ""),
                    "net_main": item.get("value", 0),
                    "pct_chg": item.get("pct_chg", 0),
                    "type": stype,
                })
        if sectors:
            save_daily_summary(today_str, sectors)
            logger.info("收盘板块摘要已保存: %d 个板块", len(sectors))
            return

    # 降级：从 SQLite 读取
    db_path = DATA_DIR / "collector.db"
    concept = _load_from_sqlite(db_path, today_str)
    if concept:
        items = []
        for name, data in sorted(concept.items(), key=lambda x: x[1].get("rank", 999)):
            items.append({
                "name": name,
                "net_main": data["net_main"],
                "pct_chg": data["pct_chg"],
                "type": "concept",
            })
        save_daily_summary(today_str, items)
        logger.info("收盘板块摘要已保存 (SQLite): %d 个板块", len(items))


# ── CLI ────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")
    reviewer = SectorReviewer()
    sectors = reviewer.get_pullback_sectors()
    if sectors:
        print(json.dumps(sectors, ensure_ascii=False, indent=2))
    else:
        print("未找到符合条件的回调板块（非交易时段或无历史数据）")
