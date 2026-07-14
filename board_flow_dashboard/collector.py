#!/usr/bin/env python3
"""
板块资金流向实时采集器 v3

通过后台线程轮询东方财富 push2 API，在交易时段每 3 秒采集一次：
  - 概念板块资金流向 (m:90+t:3)
  - 行业板块资金流向 (m:90+t:2)
  - 北向资金实时流向 (沪深港通)

数据持久化到 SQLite，累积形成分钟级时间序列，供前端看板实时展示和回放。

v3 改进：
  - 行业/北向增量持久化，不再截断
  - 持久化触发改为时间驱动
  - 连续失败自动降速
  - 看板只展示 Top 25 板块 + 动态配色
  - 北向资金存增量（更直观的走势）
"""

import hashlib
import json
import logging
import sqlite3
import threading
import time
from datetime import datetime, date
from pathlib import Path
from typing import Optional

try:
    from .data_fetcher import (
        fetch_all_sectors_snapshot,
        fetch_industry_sectors_snapshot,
        fetch_northbound_flow,
        fetch_dashboard_data,
        _build_trade_minutes,
        is_trading_day,
        load_trading_calendar,
    )
except ImportError:
    from data_fetcher import (  # type: ignore[no-redef]
        fetch_all_sectors_snapshot,
        fetch_industry_sectors_snapshot,
        fetch_northbound_flow,
        fetch_dashboard_data,
        _build_trade_minutes,
        is_trading_day,
        load_trading_calendar,
    )

logger = logging.getLogger(__name__)

# ── 看板展示参数 ────────────────────────────────────────────

DASHBOARD_TOP_N = 25          # 看板只展示前 N 个板块
WATCH_SECTORS = {             # 自选板块白名单（你的原始板块名）
    "光通信模块", "通信设备", "人形机器人", "数据中心", "玻璃基板",
    "商业航天", "AI芯片", "半导体", "军工", "消费电子",
    "低空经济", "可控核聚变", "光伏设备", "固态电池", "证券",
    "医药商业", "白酒", "稀土永磁", "锂电池", "银行",
    "创新药", "黄金概念", "有色金属", "电网概念", "存储芯片",
}

# 你的板块名 → 东方财富 API 实际名称（仅名称不一致时需要映射）
SECTOR_NAME_MAP = {
    "消费电子":   "消费电子概念",
    "锂电池":    "锂电池概念",
    "半导体":    "半导体概念",
    "银行":     "参股银行",
}

# 缺失板块加权合成: 你的板块名 → [(API实际存在的板块名, 权重), ...]
# 注意：如果板块名在 API 中精确匹配，不要放在这里！SECTOR_COMPOSITE 仅用于无法精确匹配的板块。
# 权重和应为 1.0。
SECTOR_COMPOSITE = {
    # 通信设备：API 中无精确匹配，用通信技术+5G+6G 加权
    "通信设备":   [("通信技术", 0.4), ("5G概念", 0.3), ("6G概念", 0.3)],
    # 人形机器人：API 中无精确匹配，用机器人概念+减速器
    "人形机器人": [("机器人概念", 0.55), ("减速器", 0.45)],
    # 商业航天：API 中无精确匹配，用卫星互联网+军工
    "商业航天":   [("卫星互联网", 0.5), ("军工", 0.5)],
    # 光伏设备：API 中无精确匹配，用光伏概念+绿色电力
    "光伏设备":   [("光伏概念", 0.6), ("绿色电力", 0.4)],
    # 证券：API 中无精确匹配，用参股券商+互联网金融
    "证券":      [("参股券商", 0.55), ("互联网金融", 0.45)],
    # 医药商业：API 中无精确匹配，用医药医疗风格+互联医疗
    "医药商业":   [("医药医疗风格", 0.5), ("互联医疗", 0.5)],
    # 有色金属：API 中无精确匹配，用小金属概念+稀土永磁
    "有色金属":   [("小金属概念", 0.5), ("稀土永磁", 0.5)],
}
COLOR_PALETTE = [
    "#E6194B", "#3CB44B", "#FFE119", "#4363D8", "#F58231",
    "#911EB4", "#42D4F4", "#F032E6", "#BFEF45", "#FABED4",
    "#469990", "#DCBEFF", "#9A6324", "#FFFAC8", "#800000",
    "#AAFFC3", "#808000", "#FFD8B1", "#000075", "#A9A9A9",
    "#E6BEFF", "#FF6347", "#00CED1", "#7B68EE", "#FF69B4",
    "#1E90FF", "#FFA07A", "#20B2AA", "#9370DB", "#98FB98",
]

# ── SQLite 表结构 ──────────────────────────────────────────

DB_SCHEMA = """
CREATE TABLE IF NOT EXISTS concept_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    time TEXT NOT NULL,
    data JSON NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS industry_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    time TEXT NOT NULL,
    data JSON NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS northbound_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    time TEXT NOT NULL,
    net_inflow REAL NOT NULL DEFAULT 0,
    hk2sh REAL NOT NULL DEFAULT 0,
    hk2sz REAL NOT NULL DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_concept_date_time
    ON concept_snapshots(date, time);
CREATE UNIQUE INDEX IF NOT EXISTS idx_industry_date_time
    ON industry_snapshots(date, time);
CREATE UNIQUE INDEX IF NOT EXISTS idx_northbound_date_time
    ON northbound_snapshots(date, time);
"""


def _hash_color(name: str) -> str:
    """基于名称 hash 从调色板取色，确保同一板块总是同一颜色。"""
    idx = int(hashlib.md5(name.encode()).hexdigest(), 16) % len(COLOR_PALETTE)
    return COLOR_PALETTE[idx]


def _unwrap_rank(data) -> list:
    """兼容 DB 存储格式：{time, rank} 整体序列化，提取 rank 列表。"""
    if isinstance(data, dict) and "rank" in data:
        return data["rank"]
    if isinstance(data, list):
        return data
    return []


class Storage:
    """SQLite 持久化层（v4: 持久连接 + WAL 模式，减少磁盘 I/O）。"""

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._lock = threading.Lock()
        self._conn: Optional[sqlite3.Connection] = None
        self._init_db()

    def _init_db(self):
        with self._lock:
            conn = self._get_conn_unsafe()
            conn.executescript(DB_SCHEMA)
            conn.commit()

    def _get_conn_unsafe(self) -> sqlite3.Connection:
        """获取持久连接（需在 _lock 内调用）。"""
        if self._conn is None:
            self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA cache_size=-8000")  # 8MB 缓存
        return self._conn

    def close(self):
        """关闭持久连接（仅在进程退出时调用）。"""
        with self._lock:
            if self._conn:
                try:
                    self._conn.close()
                except Exception:
                    pass
                self._conn = None

    # ── concept ────────────────────────────────────────────

    def save_concept_snapshot(self, date_str: str, time_str: str, data: list):
        with self._lock:
            conn = self._get_conn_unsafe()
            conn.execute(
                "INSERT OR REPLACE INTO concept_snapshots (date, time, data) VALUES (?, ?, ?)",
                (date_str, time_str, json.dumps(data, ensure_ascii=False)),
            )
            conn.commit()

    def load_concept_snapshots(self, date_str: str) -> list[dict]:
        with self._lock:
            conn = self._get_conn_unsafe()
            rows = conn.execute(
                "SELECT time, data FROM concept_snapshots WHERE date = ? ORDER BY time",
                (date_str,),
            ).fetchall()
            return [{"time": r[0], "rank": _unwrap_rank(json.loads(r[1]))} for r in rows]

    def get_latest_concept_time(self, date_str: str) -> Optional[str]:
        with self._lock:
            conn = self._get_conn_unsafe()
            row = conn.execute(
                "SELECT time FROM concept_snapshots WHERE date = ? ORDER BY id DESC LIMIT 1",
                (date_str,),
            ).fetchone()
            return row[0] if row else None

    # ── industry ───────────────────────────────────────────

    def save_industry_snapshot(self, date_str: str, time_str: str, data: list):
        with self._lock:
            conn = self._get_conn_unsafe()
            conn.execute(
                "INSERT OR REPLACE INTO industry_snapshots (date, time, data) VALUES (?, ?, ?)",
                (date_str, time_str, json.dumps(data, ensure_ascii=False)),
            )
            conn.commit()

    def load_industry_snapshots(self, date_str: str) -> list[dict]:
        with self._lock:
            conn = self._get_conn_unsafe()
            rows = conn.execute(
                "SELECT time, data FROM industry_snapshots WHERE date = ? ORDER BY time",
                (date_str,),
            ).fetchall()
            return [{"time": r[0], "rank": _unwrap_rank(json.loads(r[1]))} for r in rows]

    def get_latest_industry_time(self, date_str: str) -> Optional[str]:
        with self._lock:
            conn = self._get_conn_unsafe()
            row = conn.execute(
                "SELECT time FROM industry_snapshots WHERE date = ? ORDER BY id DESC LIMIT 1",
                (date_str,),
            ).fetchone()
            return row[0] if row else None

    # ── northbound ─────────────────────────────────────────

    def save_northbound_snapshot(self, date_str: str, time_str: str,
                                  net_inflow: float, hk2sh: float, hk2sz: float):
        with self._lock:
            conn = self._get_conn_unsafe()
            conn.execute(
                "INSERT OR REPLACE INTO northbound_snapshots "
                "(date, time, net_inflow, hk2sh, hk2sz) VALUES (?, ?, ?, ?, ?)",
                (date_str, time_str, net_inflow, hk2sh, hk2sz),
            )
            conn.commit()

    def load_northbound_snapshots(self, date_str: str) -> list[dict]:
        with self._lock:
            conn = self._get_conn_unsafe()
            rows = conn.execute(
                "SELECT time, net_inflow, hk2sh, hk2sz "
                "FROM northbound_snapshots WHERE date = ? ORDER BY time",
                (date_str,),
            ).fetchall()
            return [
                {"time": r[0], "net_inflow": r[1],
                 "hk2sh": r[2], "hk2sz": r[3]}
                for r in rows
            ]

    def get_latest_northbound_time(self, date_str: str) -> Optional[str]:
        with self._lock:
            conn = self._get_conn_unsafe()
            row = conn.execute(
                "SELECT time FROM northbound_snapshots WHERE date = ? ORDER BY id DESC LIMIT 1",
                (date_str,),
            ).fetchone()
            return row[0] if row else None

    # ── maintenance ────────────────────────────────────────

    def cleanup_old_data(self, keep_days: int = 30):
        cutoff = date.today().isoformat()
        with self._lock:
            conn = self._get_conn_unsafe()
            for table in ("concept_snapshots", "industry_snapshots",
                          "northbound_snapshots"):
                conn.execute(
                    f"DELETE FROM {table} WHERE date < date(?, ?)",
                    (cutoff, f"-{keep_days} days"),
                )
            conn.commit()
            logger.info("清理 %d 天前的历史数据", keep_days)


def _ffill(seq: list) -> list:
    """不再填充：缺失即缺失，不编造数据。"""
    return list(seq)


class SectorFlowCollector:
    """板块资金流向实时采集器 v3。"""

    def __init__(self, poll_interval: float = 180.0):
        self._poll_interval = poll_interval
        self._base_poll_interval = poll_interval  # 记录基准间隔
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._last_poll_time: float = 0.0
        self._consecutive_failures: int = 0
        self._max_failures_before_slowdown: int = 5
        self._last_save_time: float = 0.0   # v3: 时间驱动保存
        self._save_interval: float = 60.0    # 每 60 秒保存一次
        self._daily_summary_saved: bool = False  # 当日收盘摘要已保存
        self._trade_minutes = _build_trade_minutes()

        self._data_dir = Path(__file__).parent / "data"
        self._data_dir.mkdir(exist_ok=True)
        self._storage = Storage(self._data_dir / "collector.db")

        self._today = datetime.now().strftime("%Y%m%d")
        self._data_date: str = datetime.now().strftime("%Y-%m-%d")  # 数据真实日期（可能来自历史DB）
        self._new_day_data_arrived: bool = False  # 新一天首次 polling 成功标记
        self._concept_snapshots: list[dict] = []
        self._industry_snapshots: list[dict] = []
        self._northbound_snapshots: list[dict] = []
        self._last_nb_cumulative: dict = {}  # v3: 用于计算北向增量

        # v4: 增量构建缓存
        self._display_minutes: list[str] = []       # 预计算的显示时间轴
        self._display_minutes_cache_date: str = ""   # 时间轴缓存的日期
        self._watch_api_cache: dict = {}             # api_data_by_time 增量缓存
        self._watch_api_cache_key: tuple = ()        # (concept_len, industry_len, data_date)

        load_trading_calendar()

    # ── 生命周期 ────────────────────────────────────────────

    def start(self):
        if self._running:
            return
        self._load_from_db()
        self._running = True
        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()
        logger.info("采集器 v3 已启动 (间隔 %.1fs)", self._poll_interval)

    def stop(self):
        self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5.0)
        self._save_all_to_db()
        self._storage.close()
        logger.info("采集器已停止 | 概念:%d 行业:%d 北向:%d",
                     len(self._concept_snapshots),
                     len(self._industry_snapshots),
                     len(self._northbound_snapshots))

    def reset(self):
        with self._lock:
            self._concept_snapshots.clear()
            self._industry_snapshots.clear()
            self._northbound_snapshots.clear()
            self._last_nb_cumulative = {}
        self._consecutive_failures = 0
        self._poll_interval = self._base_poll_interval
        # v4: 清除增量缓存
        self._watch_api_cache = {}
        self._watch_api_cache_key = ()
        self._watch_cache = None
        self._dash_cache = {}
        logger.info("采集器已重置")

    # ── 轮询主循环 ──────────────────────────────────────────

    def _poll_loop(self):
        while self._running:
            try:
                if self._is_market_open():
                    self._poll_once()
                    self._maybe_save()
                    time.sleep(self._poll_interval)
                else:
                    self._maybe_save_daily_summary()
                    time.sleep(30)
                    self._check_date_rollover()
            except Exception as e:
                logger.error("采集循环异常: %s", e)
                time.sleep(5)

    def _poll_once(self):
        now = datetime.now()
        minute_key = self._round_to_nearest_minute(now)
        if minute_key is None:
            return

        any_success = False

        # 概念板块
        concept = fetch_all_sectors_snapshot(timeout=8.0)
        if concept and concept.get("sectors"):
            self._last_poll_time = time.time()
            any_success = True
            self._store_snapshot("concept", minute_key, concept["sectors"])

        # 行业板块（概念分页已有冷却，但最后一页到行业仍需间隔）
        time.sleep(30.0)
        industry = fetch_industry_sectors_snapshot(timeout=8.0)
        if industry and industry.get("sectors"):
            any_success = True
            self._store_snapshot("industry", minute_key, industry["sectors"])

        # 完整性检查：仅告警，不补拉（补拉触发额外请求会加剧 API 限流）
        if any_success:
            missing = self._check_watch_completeness()
            if missing:
                logger.warning("自选板块缺失 %d 个 API 名称: %s",
                              len(missing), ", ".join(sorted(list(missing))[:8]))

        # 北向资金（v3: 时间驱动，间隙约 6s 即每 2 个 poll 周期采一次）
        if self._should_poll_northbound():
            nb = fetch_northbound_flow(timeout=6.0)
            if nb:
                self._store_northbound(minute_key, nb)

        # 新一天首次成功：先存旧数据到 DB，再切换至今日
        if any_success and not self._new_day_data_arrived:
            today_str = datetime.now().strftime("%Y-%m-%d")
            if self._data_date != today_str:
                logger.info("新交易日首次 polling 成功，保存 %s 旧数据后切换至 %s",
                           self._data_date, today_str)
                # 清除 push2 熔断器（新交易日重新尝试）
                try:
                    from .data_fetcher import reset_circuit_breakers
                except ImportError:
                    from data_fetcher import reset_circuit_breakers  # type: ignore[no-redef]
                reset_circuit_breakers()
                # 先把旧日期的内存数据写入 DB（防止重启丢失）
                self._save_all_to_db()
                # 再清空并切换日期
                with self._lock:
                    self._concept_snapshots.clear()
                    self._industry_snapshots.clear()
                    self._northbound_snapshots.clear()
                    self._last_nb_cumulative = {}
                self._data_date = today_str
                # v4: 清除增量缓存（新日期需要重建时间轴和索引）
                self._display_minutes = []
                self._display_minutes_cache_date = ""
                self._watch_api_cache = {}
                self._watch_api_cache_key = ()
                self._watch_cache = None
                self._dash_cache = {}
            self._new_day_data_arrived = True

        # 失败处理
        if any_success:
            self._consecutive_failures = 0
            self._poll_interval = self._base_poll_interval
        else:
            self._consecutive_failures += 1
            if self._consecutive_failures >= self._max_failures_before_slowdown:
                self._poll_interval = min(self._base_poll_interval * 6, 30.0)
                logger.warning("连续 %d 次失败，降速至 %.0fs",
                               self._consecutive_failures, self._poll_interval)

    def _should_poll_northbound(self) -> bool:
        """v3: 基于时间判断是否采集北向，而非计数。避免重启后不触发。"""
        elapsed = time.time() - self._last_poll_time if self._last_poll_time else 999
        return elapsed > self._base_poll_interval * 1.5

    def _store_snapshot(self, snap_type: str, minute_key: str, sectors: list):
        attr = f"_{snap_type}_snapshots"
        with self._lock:
            snapshots: list = getattr(self, attr)
            if snapshots and snapshots[-1]["time"] == minute_key:
                snapshots[-1] = {"time": minute_key, "rank": sectors}
            else:
                snapshots.append({"time": minute_key, "rank": sectors})

    def _store_northbound(self, minute_key: str, nb_data: dict):
        """v3: 存储北向增量而非累计值，使走势图更有意义。"""
        net_inflow = nb_data["net_inflow"]
        hk2sh = nb_data.get("hk2sh", 0)
        hk2sz = nb_data.get("hk2sz", 0)

        prev = self._last_nb_cumulative
        delta_net = net_inflow - prev.get("net_inflow", net_inflow)
        delta_sh = hk2sh - prev.get("hk2sh", hk2sh)
        delta_sz = hk2sz - prev.get("hk2sz", hk2sz)

        self._last_nb_cumulative = {
            "net_inflow": net_inflow, "hk2sh": hk2sh, "hk2sz": hk2sz,
        }

        # 首次采集不存增量（没有基准）
        if not prev:
            return

        with self._lock:
            self._northbound_snapshots.append({
                "time": minute_key,
                "net_inflow": round(delta_net, 2),
                "hk2sh": round(delta_sh, 2),
                "hk2sz": round(delta_sz, 2),
            })

    # ── 时间判断 ────────────────────────────────────────────

    @staticmethod
    def _is_market_open() -> bool:
        now = datetime.now()
        if not is_trading_day(now.date()):
            return False
        t = now.time()
        morning = (
            (t.hour == 9 and t.minute >= 29) or
            (t.hour == 10) or
            (t.hour == 11 and t.minute <= 30)
        )
        afternoon = t.hour in (13, 14) or (t.hour == 15 and t.minute == 0)
        return morning or afternoon

    def _round_to_nearest_minute(self, ts: datetime) -> Optional[str]:
        h, m = ts.hour, ts.minute
        if h == 11 and m > 30:
            return None
        if h == 12:
            return None
        if h < 9 or (h == 9 and m < 31):
            return None
        if h > 15 or (h == 15 and m > 0):
            return None
        return f"{h:02d}:{m:02d}"

    def _check_date_rollover(self):
        new_today = datetime.now().strftime("%Y%m%d")
        if new_today != self._today:
            self._today = new_today
            self._daily_summary_saved = False
            self._new_day_data_arrived = False  # 等待首次 polling 成功后清旧数据
            self._consecutive_failures = 0
            self._poll_interval = self._base_poll_interval
            # 日期切换时自动清理 30 天前的旧数据
            try:
                self._storage.cleanup_old_data(keep_days=30)
            except Exception:
                pass
            logger.info("日期切换至 %s（保留旧数据至首次 polling 成功）", new_today)

    def _maybe_save_daily_summary(self):
        """收盘后（15:00+）自动保存当日板块摘要，供 B 池回溯使用。"""
        if self._daily_summary_saved:
            return
        now = datetime.now()
        # 交易日 15:00 后触发一次
        if not is_trading_day(now.date()):
            return
        if now.hour < 15:
            return
        # 检查是否有快照数据
        with self._lock:
            if not self._concept_snapshots:
                return
        try:
            from sector_reviewer import generate_daily_summary
            generate_daily_summary(collector=self)
            self._daily_summary_saved = True
            logger.info("收盘板块摘要已自动保存")
        except Exception as e:
            logger.warning("收盘摘要自动保存失败: %s", e)

    # ── 持久化（v3: 时间驱动 + 全部增量保存）─────────────────

    def _maybe_save(self):
        """v3: 基于时间间隔触发持久化，不依赖任何一种数据的计数。"""
        now = time.time()
        if self._last_save_time and (now - self._last_save_time) < self._save_interval:
            return
        self._last_save_time = now
        self._save_all_to_db()

    def _save_all_to_db(self):
        # 使用真正的数据日期而非当前日期，防止将历史数据写入今天
        save_date = self._data_date
        self._save_to_db("concept", save_date, self._storage.get_latest_concept_time,
                          self._storage.save_concept_snapshot)
        self._save_to_db("industry", save_date, self._storage.get_latest_industry_time,
                          self._storage.save_industry_snapshot)
        self._save_to_db("northbound", save_date, self._storage.get_latest_northbound_time,
                          self._save_nb_item)

    def _save_to_db(self, snap_type: str, today_str: str,
                    get_latest_fn, save_fn):
        attr = f"_{snap_type}_snapshots"
        with self._lock:
            snapshots: list = list(getattr(self, attr))
        if not snapshots:
            return
        last_saved = get_latest_fn(today_str)
        new_items = snapshots if not last_saved else [
            s for s in snapshots if s["time"] > last_saved
        ]
        for item in new_items:
            try:
                save_fn(today_str, item["time"], item)
            except Exception:
                pass

    def _save_nb_item(self, date_str: str, time_str: str, item: dict):
        self._storage.save_northbound_snapshot(
            date_str, time_str,
            item["net_inflow"], item.get("hk2sh", 0), item.get("hk2sz", 0),
        )

    def _load_from_db(self):
        """启动时从 DB 恢复数据。今日无数据时回溯最近交易日。"""
        # 尝试今日 → 回溯最多 5 个交易日
        from datetime import timedelta
        loaded = False
        for offset in range(6):  # 0=today, 1=yesterday, ..., 5
            d = date.today() - timedelta(days=offset)
            ds = d.isoformat()
            concept = self._storage.load_concept_snapshots(ds)
            industry = self._storage.load_industry_snapshots(ds)
            northbound = self._storage.load_northbound_snapshots(ds)
            if concept or industry:
                with self._lock:
                    self._concept_snapshots = concept
                    self._industry_snapshots = industry
                    self._northbound_snapshots = northbound
                # 恢复北向累计基准：DB 存的是增量，从所有增量累加重建累计值
                if northbound:
                    total_net = sum(s.get("net_inflow", 0) for s in northbound)
                    total_sh = sum(s.get("hk2sh", 0) for s in northbound)
                    total_sz = sum(s.get("hk2sz", 0) for s in northbound)
                    self._last_nb_cumulative = {
                        "net_inflow": round(total_net, 2),
                        "hk2sh": round(total_sh, 2),
                        "hk2sz": round(total_sz, 2),
                    }
                self._data_date = ds
                loaded = True
                if offset == 0:
                    logger.info("从 DB 恢复今日数据: 概念 %d, 行业 %d, 北向 %d",
                                len(concept), len(industry), len(northbound))
                else:
                    logger.info("从 DB 恢复历史数据 (%s): 概念 %d, 行业 %d, 北向 %d",
                                ds, len(concept), len(industry), len(northbound))
                break
        if not loaded:
            logger.info("DB 中无近期数据，从空快照启动")

    # ── Dashboard 数据构建 ──────────────────────────────────

    def _build_dashboard_from_snapshots(self, snapshots: list[dict]) -> dict:
        """从快照列表构建前端看板数据（仅 Top N 板块）。

        单快照也能构建 — 非交易时段可能只有 DB 中的最后一条数据。
        x轴固定使用完整交易时间轴 9:30-15:00。
        """
        if not snapshots:
            # 无任何数据时返回空（不降级到 mock）
            return {
                "date": datetime.now().strftime("%Y-%m-%d"),
                "time_label": "--:--",
                "time_index": 0,
                "total_times": 0,
                "minutes": [],
                "rank": [],
                "series": {},
                "is_trading": False,
                "data_date": self._data_date,
            }

        # 构建实际快照的时间→数据索引
        snap_map: dict[str, list[dict]] = {s["time"]: s["rank"] for s in snapshots}

        # v4: 复用预计算的显示时间轴
        if not self._display_minutes or self._display_minutes_cache_date != self._data_date:
            full_minutes = _build_trade_minutes()
            display_step = max(1, len(full_minutes) // 120)
            self._display_minutes = full_minutes[::display_step]
            if full_minutes and full_minutes[-1] not in self._display_minutes:
                self._display_minutes.append(full_minutes[-1])
            self._display_minutes_cache_date = self._data_date
        minutes = self._display_minutes

        # 最新时刻的排名 — 只取 Top N
        latest_rank = snapshots[-1]["rank"] if snapshots else []
        top_sectors = latest_rank[:DASHBOARD_TOP_N]
        top_names = {item["name"] for item in top_sectors}

        # 构建时序（只包含 Top N 板块）
        series = {}
        # 预建索引: {time: {name: (net_main, ratio)}} — 避免每板块每分钟两次 next() 扫描
        snap_index: dict[str, dict[str, tuple]] = {}
        for t in minutes:
            rank = snap_map.get(t, [])
            snap_index[t] = {}
            for r in rank:
                snap_index[t][r["name"]] = (r["net_main"], r.get("net_main_ratio"))

        for item in top_sectors:
            sector_name = item["name"]
            values = []
            ratio_values = []
            for t in minutes:
                entry = snap_index[t].get(sector_name)
                if entry is not None:
                    values.append(entry[0])
                    ratio_values.append(entry[1])
                else:
                    values.append(None)
                    ratio_values.append(None)
            series[sector_name] = {
                "name": sector_name,
                "color": _hash_color(sector_name),
                "times": minutes,
                "values": _ffill(values),
                "ratio_values": _ffill(ratio_values),
            }

        rank_data = [
            {"name": item["name"], "value": item["net_main"],
             "pct_chg": item.get("pct_chg", 0),
             "net_main_ratio": item.get("net_main_ratio", 0),
             "color": _hash_color(item["name"])}
            for item in top_sectors
        ]
        rank_data.sort(key=lambda x: x["value"], reverse=True)

        # 确定当前时刻在 minutes 中的位置（不超最后实际快照）
        last_snap_time = snapshots[-1]["time"] if snapshots else "15:00"
        now_idx = len(minutes) - 1
        for i, m in enumerate(minutes):
            if m <= last_snap_time:
                now_idx = i
        now_label = minutes[now_idx] if now_idx < len(minutes) else "15:00"

        # 截断：now_idx 之后的值置 None
        for sdata in series.values():
            for k in ("values", "ratio_values"):
                arr = sdata.get(k, [])
                for j in range(now_idx + 1, len(arr)):
                    if j < len(arr):
                        arr[j] = None

        return {
            "date": datetime.now().strftime("%Y-%m-%d"),
            "time_label": now_label,
            "time_index": now_idx,
            "total_times": len(minutes),
            "minutes": minutes,
            "rank": rank_data,
            "series": series,
            "is_trading": self._is_market_open(),
            "data_date": self._data_date,
        }

    def get_dashboard_data(self, sector_type: str = "concept",
                           watch_sectors: Optional[list[str]] = None) -> dict:
        """返回看板数据。watch 模式有缓存，避免每次请求都重建。

        Args:
            watch_sectors: 可选的自选板块列表，用于覆盖默认 WATCH_SECTORS
        """
        # watch 模式：缓存结果，仅在快照变化时重建
        if sector_type == "watch":
            # 自定义列表不用缓存
            if watch_sectors:
                return self._build_watch_dashboard(watch_sectors)
            with self._lock:
                c_len = len(self._concept_snapshots)
                i_len = len(self._industry_snapshots)
            cache_key = (c_len, i_len, self._data_date)
            cached = getattr(self, '_watch_cache', None)
            if cached and cached[0] == cache_key:
                return cached[1]
            result = self._build_watch_dashboard()
            self._watch_cache = (cache_key, result)
            return result

        with self._lock:
            if sector_type == "industry":
                snapshots = list(self._industry_snapshots)
            else:
                snapshots = list(self._concept_snapshots)
            c_len = len(snapshots)
        # concept/industry 模式也缓存
        cache_key = (sector_type, c_len, self._data_date)
        cached = getattr(self, '_dash_cache', {})
        if sector_type in cached and cached[sector_type][0] == cache_key:
            return cached[sector_type][1]
        result = self._build_dashboard_from_snapshots(snapshots)
        cached[sector_type] = (cache_key, result)
        self._dash_cache = cached
        return result

    def get_dashboard_delta(self, since_time_idx: int,
                            watch_sectors: Optional[list[str]] = None) -> dict:
        """增量更新：仅返回 since_time_idx 之后的新数据点。

        前端轮询时使用此方法，避免每次传输全量 ~30KB JSON。
        首次加载仍用 get_dashboard_data() 获取全量数据。

        Returns:
            {"time_label": str, "time_index": int, "new_data": {name: {values, ratios}}, "rank": [...]}
            如果无新数据，new_data 为空 dict。
        """
        sectors = watch_sectors if watch_sectors else list(WATCH_SECTORS)
        full = self._build_watch_dashboard(sectors)
        minutes = full["minutes"]
        total = len(minutes)

        if since_time_idx >= total - 1:
            return {
                "time_label": full["time_label"],
                "time_index": full["time_index"],
                "total_times": total,
                "new_data": {},
                "rank": full["rank"],
                "is_trading": full.get("is_trading", False),
                "data_date": full.get("data_date", ""),
            }

        # 提取 since+1 到当前的新数据
        start = since_time_idx + 1
        end = full["time_index"] + 1
        new_minutes = minutes[start:end]

        new_data = {}
        for name, sdata in full["series"].items():
            vals = sdata.get("values", [])
            ratio_vals = sdata.get("ratio_values", [])
            new_data[name] = {
                "values": vals[start:end] if len(vals) > start else [],
                "ratios": ratio_vals[start:end] if len(ratio_vals) > start else [],
                "color": sdata.get("color", ""),
            }

        return {
            "time_label": full["time_label"],
            "time_index": full["time_index"],
            "total_times": total,
            "new_minutes": new_minutes,
            "new_data": new_data,
            "rank": full["rank"],
            "is_trading": full.get("is_trading", False),
            "data_date": full.get("data_date", ""),
        }

    def _check_watch_completeness(self, sectors: Optional[list[str]] = None) -> set[str]:
        """检查自选板块所需 API 名称是否在最新快照中齐全。

        返回缺失的 API 名称集合；空集表示完整。
        """
        targets = sectors if sectors else list(WATCH_SECTORS)
        needed: set[str] = set()
        for name in targets:
            if name in SECTOR_COMPOSITE:
                for api_name, _ in SECTOR_COMPOSITE[name]:
                    needed.add(api_name)
            elif name in SECTOR_NAME_MAP:
                needed.add(SECTOR_NAME_MAP[name])
            else:
                needed.add(name)

        with self._lock:
            concept_names = set()
            for cs in self._concept_snapshots[-1:]:
                for r in cs.get("rank", []):
                    concept_names.add(r["name"])
            industry_names = set()
            for ind in self._industry_snapshots[-1:]:
                for r in ind.get("rank", []):
                    industry_names.add(r["name"])

        all_names = concept_names | industry_names
        # 模糊匹配：API 名包含目标名 或 目标名包含 API 名
        missing: set[str] = set()
        for n in needed:
            if n in all_names:
                continue
            matched = any(n in an or an in n for an in all_names)
            if not matched:
                missing.add(n)
        return missing

    def _build_watch_dashboard(self, watch_sectors: Optional[list[str]] = None) -> dict:
        """合并概念+行业快照，仅展示白名单板块。

        - SECTOR_NAME_MAP: 1对1名称映射
        - SECTOR_COMPOSITE: 加权合成（缺失板块用关联板块加权平均）

        Args:
            watch_sectors: 可选的自定义板块列表，默认使用 WATCH_SECTORS
        """
        sectors = watch_sectors if watch_sectors else list(WATCH_SECTORS)

        with self._lock:
            concept = list(self._concept_snapshots)
            industry = list(self._industry_snapshots)

        # 收集所有需要的 API 名称
        api_to_user: dict[str, str] = {}  # API名 → 你的原始名
        for name in sectors:
            api_to_user[name] = name  # 精确匹配
        for user_name, api_name in SECTOR_NAME_MAP.items():
            if user_name in sectors:
                api_to_user[api_name] = user_name
        for user_name, components in SECTOR_COMPOSITE.items():
            for api_name, _ in components:
                if api_name not in api_to_user:
                    api_to_user[api_name] = user_name  # 首次出现的合成板块获得该API名
        allowed_api_names = set(api_to_user.keys())

        # 按时间合并概念+行业 rank
        merged_by_time: dict[str, list] = {}
        for cs in concept:
            t = cs["time"]
            if t not in merged_by_time:
                merged_by_time[t] = []
            merged_by_time[t].extend(cs["rank"])
        for ind in industry:
            t = ind["time"]
            if t not in merged_by_time:
                merged_by_time[t] = []
            merged_by_time[t].extend(ind["rank"])

        # 模糊匹配：从API实际返回的板块名中找到每个WATCH板块的真实名称
        all_api_names = set()
        for t in merged_by_time:
            for r in merged_by_time[t]:
                all_api_names.add(r["name"])

        for watch_name in sectors:
            if watch_name in all_api_names:
                continue  # 已经精确匹配
            # 尝试模糊匹配：API名包含WATCH名 或 WATCH名包含API名
            matched = None
            for api_name in all_api_names:
                if watch_name in api_name or api_name in watch_name:
                    matched = api_name
                    break
            if matched:
                api_to_user[matched] = watch_name
                allowed_api_names.add(matched)

        # 每 N 个时间点取一个，减少前端渲染压力（分钟级数据对图表显示冗余）
        all_minutes = sorted(merged_by_time.keys())

        # v4: 预计算显示时间轴（静态，每天只算一次）
        cache_key = (len(concept), len(industry), self._data_date)
        if not self._display_minutes or self._display_minutes_cache_date != self._data_date:
            full_minutes = _build_trade_minutes()
            display_step = max(1, len(full_minutes) // 120)
            self._display_minutes = full_minutes[::display_step]
            if full_minutes and full_minutes[-1] not in self._display_minutes:
                self._display_minutes.append(full_minutes[-1])
            self._display_minutes_cache_date = self._data_date
        minutes = self._display_minutes

        # 确定"当前时刻"在 minutes 中的位置：用最后一条实际快照时间
        now_idx = len(minutes) - 1
        if all_minutes:
            last_data = all_minutes[-1]
            for i, m in enumerate(minutes):
                if m <= last_data:
                    now_idx = i
        now_time_label = minutes[now_idx] if now_idx < len(minutes) else minutes[-1]

        # 无数据时返回空
        if not minutes:
            return {
                "date": datetime.now().strftime("%Y-%m-%d"),
                "time_label": "--:--",
                "time_index": 0,
                "total_times": 0,
                "minutes": [],
                "rank": [],
                "series": {},
                "sector_type": "watch",
                "is_trading": False,
                "data_date": self._data_date,
            }

        # v4: 增量构建 api_data_by_time — 仅在新增时间点时追加，避免每次全量扫描
        if self._watch_api_cache_key == cache_key and self._watch_api_cache:
            api_data_by_time = self._watch_api_cache
            new_times = [t for t in minutes if t not in api_data_by_time]
            for t in new_times:
                rank = merged_by_time.get(t, [])
                api_data_by_time[t] = {}
                for r in rank:
                    name = r["name"]
                    if name in allowed_api_names:
                        api_data_by_time[t][name] = r
        else:
            api_data_by_time: dict[str, dict[str, dict]] = {}
            for t in minutes:
                rank = merged_by_time.get(t, [])
                api_data_by_time[t] = {}
                for r in rank:
                    name = r["name"]
                    if name in allowed_api_names:
                        api_data_by_time[t][name] = r
            self._watch_api_cache = api_data_by_time
            self._watch_api_cache_key = cache_key

        # ── 处理 1对1 板块（精确匹配 + SECTOR_NAME_MAP）──
        seen_user_names: set[str] = set()
        rank_data: list[dict] = []
        series: dict = {}

        def _add_sector(user_name: str, api_name: str, is_synth: bool = False):
            """添加板块：按 API 名查数据，按用户名显示。

            从全天所有时间点中取该板块最新数据，而非仅看最后一刻。
            这样即使板块收盘跌出前N名，也能显示盘中最后的有效数据。
            """
            if user_name in seen_user_names:
                return
            seen_user_names.add(user_name)
            values = []
            ratio_values = []
            for t in minutes:
                item = api_data_by_time[t].get(api_name)
                values.append(item["net_main"] if item else None)
                ratio_values.append(item.get("net_main_ratio") if item else None)
            # 从后往前找最新有效数据
            val, pct, ratio = 0.0, 0.0, 0.0
            for t in reversed(minutes):
                item = api_data_by_time[t].get(api_name)
                if item is not None:
                    val, pct = item["net_main"], item.get("pct_chg", 0)
                    ratio = item.get("net_main_ratio", 0)
                    break
            rank_data.append({
                "name": user_name, "value": val, "pct_chg": pct,
                "net_main_ratio": ratio,
                "color": _hash_color(user_name),
                "_synth": is_synth,
            })
            series[user_name] = {
                "name": user_name, "color": _hash_color(user_name),
                "times": minutes, "values": _ffill(values),
                "ratio_values": _ffill(ratio_values),
            }

        # 精确匹配的板块
        for name in sectors:
            if name in SECTOR_NAME_MAP or name in SECTOR_COMPOSITE:
                continue  # 由映射或合成处理
            _add_sector(name, name)

        # 1对1 映射板块
        for user_name, api_name in SECTOR_NAME_MAP.items():
            if user_name in sectors:
                _add_sector(user_name, api_name)

        # ── 处理加权合成板块 ──
        for user_name, components in SECTOR_COMPOSITE.items():
            if user_name not in sectors or not components:
                continue
            if user_name in seen_user_names:
                continue
            seen_user_names.add(user_name)

            # 计算加权时间序列（净额 + 净占比）
            values = []
            ratio_values = []
            for t in minutes:
                weighted = 0.0
                weighted_ratio = 0.0
                total_w = 0.0
                for api_name, w in components:
                    item = api_data_by_time[t].get(api_name)
                    if item is not None:
                        weighted += item["net_main"] * w
                        weighted_ratio += item.get("net_main_ratio", 0) * w
                        total_w += w
                if total_w > 0:
                    values.append(weighted / total_w)
                    ratio_values.append(round(weighted_ratio / total_w, 2))
                else:
                    values.append(None)
                    ratio_values.append(None)

            # 加权最新值（从后往前找每个组件的最近有效数据）
            weighted_val = 0.0
            weighted_pct = 0.0
            weighted_ratio = 0.0
            total_w = 0.0
            if minutes:
                latest_map = {}
                for api_name, _ in components:
                    for t in reversed(minutes):
                        item = api_data_by_time[t].get(api_name)
                        if item is not None:
                            latest_map[api_name] = item
                            break
                for api_name, w in components:
                    item = latest_map.get(api_name)
                    if item is not None:
                        weighted_val += item["net_main"] * w
                        weighted_pct += item.get("pct_chg", 0) * w
                        weighted_ratio += item.get("net_main_ratio", 0) * w
                        total_w += w
                if total_w > 0:
                    weighted_val /= total_w
                    weighted_pct /= total_w
                    weighted_ratio /= total_w

            rank_data.append({
                "name": user_name, "value": round(weighted_val, 2),
                "pct_chg": round(weighted_pct, 2),
                "net_main_ratio": round(weighted_ratio, 2),
                "color": _hash_color(user_name),
                "_synth": True,
            })
            series[user_name] = {
                "name": user_name, "color": _hash_color(user_name),
                "times": minutes, "values": _ffill(values),
                "ratio_values": _ffill(ratio_values),
            }

        rank_data.sort(key=lambda x: x["value"], reverse=True)

        # 截断数据：now_idx 之后的值置 None，避免图线画到未来
        for sdata in series.values():
            for k in ("values", "ratio_values"):
                arr = sdata.get(k, [])
                for j in range(now_idx + 1, len(arr)):
                    if j < len(arr):
                        arr[j] = None

        return {
            "date": datetime.now().strftime("%Y-%m-%d"),
            "time_label": now_time_label,
            "time_index": now_idx,
            "total_times": len(minutes),
            "minutes": minutes,
            "rank": rank_data,
            "series": series,
            "sector_type": "watch",
            "is_trading": self._is_market_open(),
            "data_date": self._data_date,
        }

    def get_snapshot(self, time_idx: int, sector_type: str = "concept") -> dict:
        data = self.get_dashboard_data(sector_type)
        minutes = data["minutes"]
        series = data["series"]

        time_idx = max(0, min(time_idx, len(minutes) - 1))
        time_label = minutes[time_idx] if time_idx < len(minutes) else "15:00"

        snapshot_rank = []
        for sec_name, sec_data in series.items():
            vals = sec_data["values"]
            ratio_vals = sec_data.get("ratio_values", [])
            val = vals[time_idx] if time_idx < len(vals) else (vals[-1] if vals else 0)
            ratio = ratio_vals[time_idx] if time_idx < len(ratio_vals) else (ratio_vals[-1] if ratio_vals else 0)
            if val is not None:
                snapshot_rank.append({
                    "name": sec_name, "value": val, "color": sec_data["color"],
                    "net_main_ratio": ratio if ratio is not None else 0,
                })
        snapshot_rank.sort(key=lambda x: x["value"], reverse=True)

        return {
            "date": data["date"],
            "time_label": time_label,
            "time_index": time_idx,
            "total_times": len(minutes),
            "minutes": minutes,
            "rank": snapshot_rank,
            "series": {
                name: {
                    "name": sdata["name"],
                    "color": sdata["color"],
                    "times": sdata["times"][:time_idx + 1],
                    "values": sdata["values"][:time_idx + 1],
                }
                for name, sdata in series.items()
            },
        }

    def get_top_sectors(self, top_n: int = 5) -> list[dict]:
        """获取当前资金流入最强的板块（含 code，供选股引擎下钻）。

        v3 新增：替代 stock_selector 直接调 API，复用采集器数据。
        """
        result = []
        for stype in ("concept", "industry"):
            with self._lock:
                snapshots = list(
                    self._concept_snapshots if stype == "concept"
                    else self._industry_snapshots
                )
            if not snapshots:
                continue
            latest_rank = snapshots[-1].get("rank", [])
            for item in latest_rank[:top_n]:
                code = item.get("code", "")
                name = item.get("name", "")
                if name:
                    result.append({
                        "code": code,
                        "name": name,
                        "net_main": item.get("net_main", 0),
                        "net_main_ratio": item.get("net_main_ratio", 0),
                        "pct_chg": item.get("pct_chg", 0),
                        "type": stype,
                    })
        return result

    def get_all_sectors_data(self) -> list[dict]:
        """获取全量板块实时数据（含 code/pct_chg），供 sector_reviewer 使用。

        与 get_top_sectors 的区别：返回全部板块而非 Top N，
        且包含 pct_chg 用于回调判定。
        """
        result = []
        for stype in ("concept", "industry"):
            with self._lock:
                snapshots = list(
                    self._concept_snapshots if stype == "concept"
                    else self._industry_snapshots
                )
            if not snapshots:
                continue
            latest_rank = snapshots[-1].get("rank", [])
            for i, item in enumerate(latest_rank):
                name = item.get("name", "")
                if not name:
                    continue
                result.append({
                    "code": item.get("code", ""),
                    "name": name,
                    "net_main": item.get("net_main", 0),
                    "net_main_ratio": item.get("net_main_ratio", 0),
                    "pct_chg": item.get("pct_chg", 0),
                    "type": stype,
                    "rank": i + 1,
                })
        return result

    def get_sector_timeseries(self, sector_name: str, recent_minutes: int = 30) -> dict:
        """获取指定板块最近 N 分钟的资金流向时间序列。

        从内存快照中提取该板块在每个时间点的主力净流入额和净占比。
        支持 SECTOR_NAME_MAP 名称映射 和 SECTOR_COMPOSITE 加权合成。

        Returns:
            {"name": str, "times": [str], "values": [float|None], "ratios": [float|None],
             "latest_value": float, "latest_ratio": float, "trend": str}
        """
        with self._lock:
            concept_snaps = list(self._concept_snapshots)
            industry_snaps = list(self._industry_snapshots)

        if not concept_snaps and not industry_snaps:
            return {"name": sector_name, "error": "无今日快照数据"}

        # 解析搜索名称：原始名 + NAME_MAP 映射名
        search_names = [sector_name]
        mapped = SECTOR_NAME_MAP.get(sector_name)
        if mapped:
            search_names.append(mapped)

        # 检查是否是加权合成板块
        composite = SECTOR_COMPOSITE.get(sector_name)

        # 从概念和行业快照中提取时间序列
        time_data = {}  # {time: (net_main, net_main_ratio, pct_chg)}
        for snap_list in (concept_snaps, industry_snaps):
            for snap in snap_list:
                t = snap.get("time", "")
                if t in time_data:
                    continue
                rank = snap.get("rank", [])

                if composite:
                    # 加权合成：从多个子板块聚合
                    comp_values = {}  # {comp_name: (net_main, ratio, pct)}
                    for item in rank:
                        item_name = item.get("name", "")
                        for comp_name, weight in composite:
                            if item_name == comp_name:
                                comp_values[comp_name] = (
                                    item.get("net_main", 0),
                                    item.get("net_main_ratio", 0),
                                    item.get("pct_chg", 0),
                                )
                    if len(comp_values) == len(composite):
                        net_main = sum(
                            comp_values[cn][0] * w for cn, w in composite
                        )
                        net_ratio = sum(
                            comp_values[cn][1] * w for cn, w in composite
                        )
                        pct_chg = sum(
                            comp_values[cn][2] * w for cn, w in composite
                        )
                        time_data[t] = (
                            round(net_main, 2),
                            round(net_ratio, 2),
                            round(pct_chg, 2),
                        )
                else:
                    # 直接名称匹配（含 NAME_MAP 映射）
                    for item in rank:
                        if item.get("name") in search_names:
                            time_data[t] = (
                                item.get("net_main", 0),
                                item.get("net_main_ratio", 0),
                                item.get("pct_chg", 0),
                            )
                            break

        sorted_times = sorted(time_data.keys())
        if recent_minutes > 0 and len(sorted_times) > recent_minutes:
            sorted_times = sorted_times[-recent_minutes:]

        values = []
        ratios = []
        for t in sorted_times:
            if t in time_data:
                values.append(time_data[t][0])
                ratios.append(time_data[t][1])
            else:
                values.append(None)
                ratios.append(None)

        # 判断趋势
        valid_vals = [v for v in values if v is not None]
        if len(valid_vals) >= 2:
            delta = valid_vals[-1] - valid_vals[0]
            if delta > 5:
                trend = "持续加速流入"
            elif delta > 1:
                trend = "温和流入"
            elif delta < -5:
                trend = "加速流出"
            elif delta < -1:
                trend = "温和流出"
            else:
                trend = "资金平稳"
        else:
            trend = "数据不足"

        latest_val = valid_vals[-1] if valid_vals else 0
        latest_ratio = ratios[-1] if ratios else 0

        return {
            "name": sector_name,
            "times": sorted_times,
            "values": values,
            "ratios": ratios,
            "latest_value": round(latest_val, 2),
            "latest_ratio": round(latest_ratio, 2) if latest_ratio else 0,
            "trend": trend,
            "data_points": len(sorted_times),
        }

    def get_northbound_data(self) -> dict:
        with self._lock:
            snapshots = list(self._northbound_snapshots)

        if not snapshots:
            nb = fetch_northbound_flow()
            if nb:
                return {
                    "date": datetime.now().strftime("%Y-%m-%d"),
                    "latest": nb,
                    "history": [],
                }
            return {
                "date": datetime.now().strftime("%Y-%m-%d"),
                "latest": {"time": "--:--", "net_inflow": 0, "hk2sh": 0, "hk2sz": 0},
                "history": [],
            }

        times = [s["time"] for s in snapshots]
        net_inflows = [s["net_inflow"] for s in snapshots]

        return {
            "date": datetime.now().strftime("%Y-%m-%d"),
            "latest": snapshots[-1] if snapshots else None,
            "history": {
                "times": times,
                "net_inflows": net_inflows,
            },
        }

    def get_status(self) -> dict:
        with self._lock:
            concept_count = len(self._concept_snapshots)
            industry_count = len(self._industry_snapshots)
            nb_count = len(self._northbound_snapshots)
        return {
            "running": self._running,
            "snapshots_concept": concept_count,
            "snapshots_industry": industry_count,
            "snapshots_northbound": nb_count,
            "last_poll_time": self._last_poll_time,
            "last_poll_iso": (
                datetime.fromtimestamp(self._last_poll_time).isoformat()
                if self._last_poll_time else None
            ),
            "consecutive_failures": self._consecutive_failures,
            "poll_interval": self._poll_interval,
            "market_open": self._is_market_open(),
            "is_trading_day": is_trading_day(date.today()),
            "date": datetime.now().strftime("%Y-%m-%d"),
            "data_date": self._data_date,
        }

    def get_available_dates(self) -> list[str]:
        """返回 DB 中所有有数据的交易日期，供前端日期选择器使用。"""
        try:
            with self._storage._lock:
                conn = self._storage._get_conn_unsafe()
                dates = set()
                for table in ("concept_snapshots", "industry_snapshots"):
                    rows = conn.execute(
                        f"SELECT DISTINCT date FROM {table} ORDER BY date DESC LIMIT 30"
                    ).fetchall()
                    dates.update(r[0] for r in rows)
                return sorted(dates, reverse=True)
        except Exception:
            return [datetime.now().strftime("%Y-%m-%d")]

    def get_dashboard_data_for_date(self, date_str: str, sector_type: str = "concept") -> dict:
        """加载指定日期的板块数据并构建看板数据。直接从 DB 读取。"""
        if sector_type not in ("concept", "industry", "watch"):
            sector_type = "watch"

        if sector_type == "watch":
            concept = self._storage.load_concept_snapshots(date_str)
            industry = self._storage.load_industry_snapshots(date_str)
            if not concept and not industry:
                return {"date": date_str, "rank": [], "series": [],
                        "minutes": [], "total_times": 0, "data_date": date_str}
            with self._lock:
                saved_c, saved_i = self._concept_snapshots, self._industry_snapshots
                saved_dd = self._data_date
                self._concept_snapshots = concept
                self._industry_snapshots = industry
                self._data_date = date_str
            try:
                result = self._build_watch_dashboard()
                result["date"] = date_str
                result["data_date"] = date_str
                result["is_trading"] = False
                return result
            finally:
                with self._lock:
                    self._concept_snapshots = saved_c
                    self._industry_snapshots = saved_i
                    self._data_date = saved_dd

        if sector_type == "industry":
            snaps = self._storage.load_industry_snapshots(date_str)
        else:
            snaps = self._storage.load_concept_snapshots(date_str)

        result = self._build_dashboard_from_snapshots(snaps)
        result["date"] = date_str
        result["data_date"] = date_str
        result["is_trading"] = False
        return result

    def cleanup_history(self, keep_days: int = 30):
        self._storage.cleanup_old_data(keep_days)
