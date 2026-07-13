#!/usr/bin/env python3
"""
情绪面采集器 (System B) — 独立线程运行

采集维度：
  1. 板块轮动速度（Spearman 秩相关，从 collector DB 读取）
  2. 涨停板数据（AKShare stock_em_zt_pool）
  3. 市场情绪综合指数
  4. 板块涨停热度分布

频率：每 5 分钟刷新一次（市场时段）
输出：写入 SignalStore
"""

import json
import logging
import sqlite3
import threading
import time
from datetime import datetime, date
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).parent / "data"

# ── 参数 ──────────────────────────────────────────────────────

SENTIMENT_INTERVAL = 300       # 采集间隔（秒）
ROTATION_LOOKBACK = 30         # 轮动速度回溯（分钟）
ROTATION_TOP_N = 20            # 比较前 N 个板块的排名变化

# ── 轮动速度计算 ──────────────────────────────────────────────

def compute_rotation_speed(db_path: Path, lookback_minutes: int = ROTATION_LOOKBACK,
                            top_n: int = ROTATION_TOP_N) -> Optional[float]:
    """计算板块轮动速度 0~1。

    方法：比较当前 Top N 板块排名和 N 分钟前的排名，
    用 Spearman 秩相关系数，越低 = 轮动越快。

    Returns:
        0.0（排名完全一致，零轮动）~ 1.0（排名完全打乱，极快轮动）
        None 表示数据不足
    """
    try:
        conn = sqlite3.connect(str(db_path))
        today = date.today().isoformat()

        # 获取最近两条快照（至少间隔 lookback_minutes）
        rows = conn.execute(
            "SELECT time, data FROM concept_snapshots WHERE date = ? ORDER BY id",
            (today,)
        ).fetchall()
        conn.close()

        if len(rows) < 2:
            return None

        # 找当前时间和 N 分钟前的快照
        now_row = rows[-1]
        now_time = _parse_minute(now_row[0])
        if now_time is None:
            return None

        # 向前找最接近 (now - lookback) 的快照
        past_row = None
        for row in reversed(rows[:-1]):
            t = _parse_minute(row[0])
            if t is not None and now_time - t >= lookback_minutes:
                past_row = row
                break
        if past_row is None:
            past_row = rows[0]  # 找不到时用最早的

        # 解析每时刻的 Top N 板块排名
        def top_n_names(data_json: str) -> list[str]:
            data = json.loads(data_json)
            rank = data.get("rank", []) if isinstance(data, dict) else data
            return [item["name"] for item in rank[:top_n]]

        now_names = top_n_names(now_row[1])
        past_names = top_n_names(past_row[1])

        # Spearman 秩相关
        common = set(now_names) & set(past_names)
        # v4.0: 快照不足时返回 None 而非 1.0，避免误判极快轮动
        if len(rows) < 10:
            return None  # 数据不足
        if len(common) < 8:
            return 1.0  # 面貌全非 = 极快轮动

        now_rank = {name: i for i, name in enumerate(now_names) if name in common}
        past_rank = {name: i for i, name in enumerate(past_names) if name in common}

        d_sq = sum((now_rank[name] - past_rank[name]) ** 2 for name in common)
        n = len(common)
        spearman = 1 - (6 * d_sq) / (n * (n**2 - 1))

        rotation = round(1 - max(0, spearman), 4)
        return rotation
    except Exception as e:
        logger.warning("轮动速度计算失败: %s", e)
        return None


def _parse_minute(time_str: str) -> Optional[int]:
    """解析 HH:MM 为分钟数。"""
    try:
        parts = time_str.strip().split(":")
        return int(parts[0]) * 60 + int(parts[1])
    except (ValueError, IndexError, AttributeError):
        return None


# ── 涨停板数据采集 ────────────────────────────────────────────

def fetch_limit_up_data() -> Optional[dict]:
    """获取当日涨停板数据。

    Returns:
        {
            "total": int,           # 涨停总数（含一字板）
            "lianban": {1: n, 2: n, ...},  # 连板分布
            "po_ban": int,          # 破板数（曾涨停未封住）
            "seal_amount_avg": float,  # 平均封单额（亿）
            "sectors": {name: count, ...},  # 板块涨停分布
            "stocks": [{code, name, lianban, seal_amount, sector}, ...]
        }
    """
    try:
        import akshare as ak
    except ImportError:
        logger.warning("akshare 未安装，跳过涨停板采集")
        return None

    try:
        today = date.today().strftime("%Y%m%d")
        df = ak.stock_zt_pool_em(date=today)

        if df is None or df.empty:
            logger.info("今日暂无涨停板数据")
            return None

        # 连板分布
        lianban_col = "连板数"
        lianban_dist = {}
        if lianban_col in df.columns:
            for v in df[lianban_col].value_counts().items():
                lianban_dist[int(v[0])] = int(v[1])

        # 破板数
        po_ban = 0
        if "炸板次数" in df.columns:
            po_ban = int((df["炸板次数"] > 0).sum())

        # 封单金额（列名: 封板资金，单位: 元）
        seal_avg = 0.0
        if "封板资金" in df.columns:
            seal_vals = df["封板资金"].dropna()
            if len(seal_vals) > 0:
                seal_avg = round(float(seal_vals.mean()) / 1e8, 2)

        # 板块分布
        sector_col = "所属行业" if "所属行业" in df.columns else None
        sector_dist = {}
        if sector_col:
            for s in df[sector_col].dropna():
                sector_dist[s] = sector_dist.get(s, 0) + 1

        # 个股列表
        stocks = []
        for _, row in df.iterrows():
            stocks.append({
                "code": str(row.get("代码", "")),
                "name": str(row.get("名称", "")),
                "lianban": int(row.get("连板数", 1)),
                "seal_amount": float(row.get("封板资金", 0)),
                "sector": str(row.get("所属行业", "")),
            })

        return {
            "total": len(df),
            "lianban": lianban_dist,
            "po_ban": po_ban,
            "seal_amount_avg": seal_avg,
            "sectors": sector_dist,
            "stocks": stocks[:50],  # 只保留前 50 只详情
        }
    except Exception as e:
        logger.warning("涨停板数据采集失败: %s", e)
        return None


# ── 情绪指数计算 ──────────────────────────────────────────────

def compute_sentiment_index(limit_up_data: Optional[dict],
                             market_breadth: float = 0.5) -> dict:
    """根据涨停数据和市场广度计算综合情绪指标。

    Returns:
        {
            "sentiment_index": float,      # 0~100
            "lianban_rate": float,         # 连板率 0~1
            "po_ban_rate": float,          # 破板率 0~1
            "seal_strength": float,        # 封单强度 0~1
            "limit_up_count": int,         # 涨停家数
        }
    """
    if limit_up_data is None:
        return {
            "sentiment_index": 50.0,
            "lianban_rate": 0.0,
            "po_ban_rate": 1.0,
            "seal_strength": 0.0,
            "limit_up_count": 0,
        }

    total = limit_up_data["total"]
    po_ban = limit_up_data.get("po_ban", 0)
    seal_avg = limit_up_data.get("seal_amount_avg", 0)

    # 连板率 = 2连板及以上 / 涨停总数
    lianban = limit_up_data.get("lianban", {})
    lianban_2plus = sum(v for k, v in lianban.items() if k >= 2)
    lianban_rate = lianban_2plus / total if total > 0 else 0

    # 破板率（越低越好）
    po_ban_rate = po_ban / (total + po_ban) if (total + po_ban) > 0 else 0.5

    # 封单强度（归一化：10亿=满分）
    seal_strength = min(1.0, seal_avg / 10)

    # 综合情绪指数
    sentiment = (
        lianban_rate * 30 +
        (1 - po_ban_rate) * 30 +
        seal_strength * 20 +
        market_breadth * 20
    )
    sentiment_index = round(min(100, max(0, sentiment)), 1)

    return {
        "sentiment_index": sentiment_index,
        "lianban_rate": round(lianban_rate, 3),
        "po_ban_rate": round(po_ban_rate, 3),
        "seal_strength": round(seal_strength, 3),
        "limit_up_count": total,
    }


# ── 板块热度计算 ──────────────────────────────────────────────

def compute_sector_heat(limit_up_data: Optional[dict]) -> dict[str, dict]:
    """计算各板块涨停热度。

    Returns:
        {sector_name: {"zt_count": int, "max_lianban": int, "heat_score": float}, ...}
    """
    if limit_up_data is None:
        return {}

    sectors = limit_up_data.get("sectors", {})
    stocks = limit_up_data.get("stocks", [])

    # 每板块最高连板
    max_lianban_by_sector = {}
    for s in stocks:
        sec = s.get("sector", "")
        if sec:
            max_lianban_by_sector[sec] = max(
                max_lianban_by_sector.get(sec, 0),
                s.get("lianban", 1)
            )

    result = {}
    for sec, count in sectors.items():
        max_lb = max_lianban_by_sector.get(sec, 1)
        # heat_score: 涨停数归一化（10只=满分）×0.5 + 连板高度（5连板=满分）×0.5
        heat = min(1.0, count / 10) * 0.5 + min(1.0, max_lb / 5) * 0.5
        result[sec] = {
            "zt_count": count,
            "max_lianban": max_lb,
            "heat_score": round(heat, 2),
        }

    return result


# ── 采集器主线程 ──────────────────────────────────────────────

class SentimentCollector:
    """情绪面采集器 — 独立线程运行。"""

    def __init__(self, db_path: Optional[Path] = None):
        if db_path is None:
            db_path = DATA_DIR / "collector.db"
        self._db_path = db_path
        self._thread: Optional[threading.Thread] = None
        self._running = False

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True, name="sentiment")
        self._thread.start()
        logger.info("SentimentCollector 启动")

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=2)

    def collect_once(self) -> dict:
        """执行一次完整采集。返回信号数据。"""
        from signals import get_signal_store

        # 1. 轮动速度
        rotation = compute_rotation_speed(self._db_path)
        if rotation is None:
            rotation = 0.5  # 数据不足时中性默认

        # 2. 涨停板数据
        limit_up = fetch_limit_up_data()

        # 3. 市场广度（从 collector DB 自己算，避免循环依赖）
        breadth = self._get_market_breadth()

        # 4. 情绪指数
        sentiment = compute_sentiment_index(limit_up, breadth)

        # 5. 板块热度
        sector_heat = compute_sector_heat(limit_up)

        # 组装
        market_data = {
            "rotation_speed": rotation,
            "market_breadth": breadth,
            **sentiment,
        }

        signal_data = {
            "market": market_data,
            "sector_heat": sector_heat,
            "limit_up_detail": limit_up,
        }

        # 写入 SignalStore
        store = get_signal_store()
        store.update("sentiment", signal_data)

        return signal_data

    def _get_market_breadth(self) -> float:
        """从 collector DB 计算市场广度。"""
        try:
            conn = sqlite3.connect(str(self._db_path))
            today = date.today().isoformat()
            row = conn.execute(
                "SELECT data FROM concept_snapshots WHERE date=? ORDER BY id DESC LIMIT 1",
                (today,)
            ).fetchone()
            conn.close()

            if not row:
                return 0.5

            data = json.loads(row[0])
            sectors = data.get("sectors", []) if isinstance(data, dict) else data
            if not sectors:
                return 0.5
            up = sum(1 for s in sectors if (s.get("pct_chg") or 0) > 0)
            return round(up / len(sectors), 2)
        except Exception:
            return 0.5

    def _run(self):
        while self._running:
            try:
                self.collect_once()
                logger.info("SentimentCollector: 采集完成")
            except Exception as e:
                logger.warning("SentimentCollector 采集异常: %s", e)
                try:
                    from signals import get_signal_store
                    get_signal_store().set_error("sentiment")
                except Exception:
                    pass

            # 等到下一个采集周期
            for _ in range(SENTIMENT_INTERVAL):
                if not self._running:
                    break
                time.sleep(1)


# ── CLI ────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")
    collector = SentimentCollector()
    data = collector.collect_once()
    print(json.dumps(data, ensure_ascii=False, indent=2)[:2000])
