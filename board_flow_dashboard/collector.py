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
    "人形机器人": "机器人执行器",
    "消费电子":   "品牌消费电子",
    "医药商业":   "医药生物",
    "锂电池":    "锂矿概念",
    "固态电池":   "电池",
    "电网概念":   "绿色电力",
    "数据中心":   "数据确权",
    "AI芯片":    "华为昇腾",
    "存储芯片":   "模拟芯片设计",
}

# 缺失板块加权合成: 你的板块名 → [(API板块名, 权重), ...]
# 权重和应为 1.0。空列表表示无可关联板块，不会显示。
SECTOR_COMPOSITE = {
    "半导体":    [("模拟芯片设计", 0.6), ("华为昇腾", 0.4)],
    "光通信模块": [("华为昇腾", 1.0)],
    "通信设备":   [("华为昇腾", 1.0)],
    "白酒":      [("食品饮料", 1.0)],
    "军工":      [("民爆制品", 0.4), ("减速器", 0.3), ("工程机械概念", 0.3)],
    "光伏设备":   [("绿色电力", 0.6), ("电力", 0.4)],
    "证券":      [("保险Ⅱ", 0.4), ("保险Ⅲ", 0.3), ("银行Ⅱ", 0.3)],
    "可控核聚变": [("核污染防治", 0.5), ("绿色电力", 0.5)],
    "低空经济":   [("飞行汽车(eVTOL)", 1.0)],
    "商业航天":   [("航天装备Ⅱ", 0.5), ("国防军工", 0.5)],
    "玻璃基板":   [("裸眼3D", 0.35), ("品牌消费电子", 0.30),
                   ("模拟芯片设计", 0.20), ("华为昇腾", 0.15)],
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
    """SQLite 持久化层。"""

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._lock = threading.Lock()
        self._init_db()

    def _init_db(self):
        with self._lock:
            conn = sqlite3.connect(str(self.db_path))
            conn.executescript(DB_SCHEMA)
            conn.commit()
            conn.close()

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    # ── concept ────────────────────────────────────────────

    def save_concept_snapshot(self, date_str: str, time_str: str, data: list):
        with self._lock:
            conn = self._get_conn()
            try:
                conn.execute(
                    "INSERT OR REPLACE INTO concept_snapshots (date, time, data) VALUES (?, ?, ?)",
                    (date_str, time_str, json.dumps(data, ensure_ascii=False)),
                )
                conn.commit()
            finally:
                conn.close()

    def load_concept_snapshots(self, date_str: str) -> list[dict]:
        with self._lock:
            conn = self._get_conn()
            try:
                rows = conn.execute(
                    "SELECT time, data FROM concept_snapshots WHERE date = ? ORDER BY time",
                    (date_str,),
                ).fetchall()
                return [{"time": r[0], "rank": _unwrap_rank(json.loads(r[1]))} for r in rows]
            finally:
                conn.close()

    def get_latest_concept_time(self, date_str: str) -> Optional[str]:
        with self._lock:
            conn = self._get_conn()
            try:
                row = conn.execute(
                    "SELECT time FROM concept_snapshots WHERE date = ? ORDER BY id DESC LIMIT 1",
                    (date_str,),
                ).fetchone()
                return row[0] if row else None
            finally:
                conn.close()

    # ── industry ───────────────────────────────────────────

    def save_industry_snapshot(self, date_str: str, time_str: str, data: list):
        with self._lock:
            conn = self._get_conn()
            try:
                conn.execute(
                    "INSERT OR REPLACE INTO industry_snapshots (date, time, data) VALUES (?, ?, ?)",
                    (date_str, time_str, json.dumps(data, ensure_ascii=False)),
                )
                conn.commit()
            finally:
                conn.close()

    def load_industry_snapshots(self, date_str: str) -> list[dict]:
        with self._lock:
            conn = self._get_conn()
            try:
                rows = conn.execute(
                    "SELECT time, data FROM industry_snapshots WHERE date = ? ORDER BY time",
                    (date_str,),
                ).fetchall()
                return [{"time": r[0], "rank": _unwrap_rank(json.loads(r[1]))} for r in rows]
            finally:
                conn.close()

    def get_latest_industry_time(self, date_str: str) -> Optional[str]:
        with self._lock:
            conn = self._get_conn()
            try:
                row = conn.execute(
                    "SELECT time FROM industry_snapshots WHERE date = ? ORDER BY id DESC LIMIT 1",
                    (date_str,),
                ).fetchone()
                return row[0] if row else None
            finally:
                conn.close()

    # ── northbound ─────────────────────────────────────────

    def save_northbound_snapshot(self, date_str: str, time_str: str,
                                  net_inflow: float, hk2sh: float, hk2sz: float):
        with self._lock:
            conn = self._get_conn()
            try:
                conn.execute(
                    "INSERT OR REPLACE INTO northbound_snapshots "
                    "(date, time, net_inflow, hk2sh, hk2sz) VALUES (?, ?, ?, ?, ?)",
                    (date_str, time_str, net_inflow, hk2sh, hk2sz),
                )
                conn.commit()
            finally:
                conn.close()

    def load_northbound_snapshots(self, date_str: str) -> list[dict]:
        with self._lock:
            conn = self._get_conn()
            try:
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
            finally:
                conn.close()

    def get_latest_northbound_time(self, date_str: str) -> Optional[str]:
        with self._lock:
            conn = self._get_conn()
            try:
                row = conn.execute(
                    "SELECT time FROM northbound_snapshots WHERE date = ? ORDER BY id DESC LIMIT 1",
                    (date_str,),
                ).fetchone()
                return row[0] if row else None
            finally:
                conn.close()

    # ── maintenance ────────────────────────────────────────

    def cleanup_old_data(self, keep_days: int = 30):
        cutoff = date.today().isoformat()
        with self._lock:
            conn = self._get_conn()
            try:
                for table in ("concept_snapshots", "industry_snapshots",
                              "northbound_snapshots"):
                    conn.execute(
                        f"DELETE FROM {table} WHERE date < date(?, ?)",
                        (cutoff, f"-{keep_days} days"),
                    )
                conn.commit()
                logger.info("清理 %d 天前的历史数据", keep_days)
            finally:
                conn.close()


class SectorFlowCollector:
    """板块资金流向实时采集器 v3。"""

    def __init__(self, poll_interval: float = 3.0):
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

        # 行业板块
        industry = fetch_industry_sectors_snapshot(timeout=8.0)
        if industry and industry.get("sectors"):
            any_success = True
            self._store_snapshot("industry", minute_key, industry["sectors"])

        # 北向资金（v3: 时间驱动，间隙约 6s 即每 2 个 poll 周期采一次）
        if self._should_poll_northbound():
            nb = fetch_northbound_flow(timeout=6.0)
            if nb:
                self._store_northbound(minute_key, nb)

        # 新一天首次成功：清空昨日快照，切换至今日
        if any_success and not self._new_day_data_arrived:
            today_str = datetime.now().strftime("%Y-%m-%d")
            if self._data_date != today_str:
                logger.info("新交易日首次 polling 成功，清空 %s 旧数据", self._data_date)
                with self._lock:
                    self._concept_snapshots.clear()
                    self._industry_snapshots.clear()
                    self._northbound_snapshots.clear()
                    self._last_nb_cumulative = {}
                self._data_date = today_str
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
        today_str = datetime.now().strftime("%Y-%m-%d")
        self._save_to_db("concept", today_str, self._storage.get_latest_concept_time,
                          self._storage.save_concept_snapshot)
        self._save_to_db("industry", today_str, self._storage.get_latest_industry_time,
                          self._storage.save_industry_snapshot)
        self._save_to_db("northbound", today_str, self._storage.get_latest_northbound_time,
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
                # 恢复北向累计基准
                if northbound:
                    last = northbound[-1]
                    self._last_nb_cumulative = {
                        "net_inflow": last.get("net_inflow", 0),
                        "hk2sh": last.get("hk2sh", 0),
                        "hk2sz": last.get("hk2sz", 0),
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

        minutes = [s["time"] for s in snapshots]

        # 构建 {time: rank} 索引
        snap_map: dict[str, list[dict]] = {s["time"]: s["rank"] for s in snapshots}

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
                "values": values,
                "ratio_values": ratio_values,
            }

        rank_data = [
            {"name": item["name"], "value": item["net_main"],
             "pct_chg": item.get("pct_chg", 0),
             "net_main_ratio": item.get("net_main_ratio", 0),
             "color": _hash_color(item["name"])}
            for item in top_sectors
        ]
        rank_data.sort(key=lambda x: x["value"], reverse=True)

        return {
            "date": datetime.now().strftime("%Y-%m-%d"),
            "time_label": minutes[-1] if minutes else "15:00",
            "time_index": len(minutes) - 1,
            "total_times": len(minutes),
            "minutes": minutes,
            "rank": rank_data,
            "series": series,
            "is_trading": self._is_market_open(),
            "data_date": self._data_date,
        }

    def get_dashboard_data(self, sector_type: str = "concept") -> dict:
        if sector_type == "watch":
            return self._build_watch_dashboard()

        with self._lock:
            if sector_type == "industry":
                snapshots = list(self._industry_snapshots)
            else:
                snapshots = list(self._concept_snapshots)

        return self._build_dashboard_from_snapshots(snapshots)

    def _build_watch_dashboard(self) -> dict:
        """合并概念+行业快照，仅展示白名单板块。

        - SECTOR_NAME_MAP: 1对1名称映射
        - SECTOR_COMPOSITE: 加权合成（缺失板块用关联板块加权平均）
        """
        with self._lock:
            concept = list(self._concept_snapshots)
            industry = list(self._industry_snapshots)

        # 收集所有需要的 API 名称
        api_to_user: dict[str, str] = {}  # API名 → 你的原始名
        for name in WATCH_SECTORS:
            api_to_user[name] = name  # 精确匹配
        for user_name, api_name in SECTOR_NAME_MAP.items():
            if user_name in WATCH_SECTORS:
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

        minutes = sorted(merged_by_time.keys())

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

        # 构建每时刻的 API 名 → 数据 索引
        api_data_by_time: dict[str, dict[str, dict]] = {}
        for t in minutes:
            rank = merged_by_time.get(t, [])
            api_data_by_time[t] = {}
            for r in rank:
                name = r["name"]
                if name in allowed_api_names:
                    api_data_by_time[t][name] = r

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
                "times": minutes, "values": values,
                "ratio_values": ratio_values,
            }

        # 精确匹配的板块
        for name in WATCH_SECTORS:
            if name in SECTOR_NAME_MAP or name in SECTOR_COMPOSITE:
                continue  # 由映射或合成处理
            _add_sector(name, name)

        # 1对1 映射板块
        for user_name, api_name in SECTOR_NAME_MAP.items():
            if user_name in WATCH_SECTORS:
                _add_sector(user_name, api_name)

        # ── 处理加权合成板块 ──
        for user_name, components in SECTOR_COMPOSITE.items():
            if user_name not in WATCH_SECTORS or not components:
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
                "times": minutes, "values": values,
                "ratio_values": ratio_values,
            }

        rank_data.sort(key=lambda x: x["value"], reverse=True)

        return {
            "date": datetime.now().strftime("%Y-%m-%d"),
            "time_label": minutes[-1] if minutes else "15:00",
            "time_index": len(minutes) - 1,
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

    def cleanup_history(self, keep_days: int = 30):
        self._storage.cleanup_old_data(keep_days)
