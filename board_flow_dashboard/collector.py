#!/usr/bin/env python3
"""
板块资金流向实时采集器

通过后台线程轮询东方财富 push2 API，在交易时段每 3 秒采集一次全板块资金流向快照，
累积形成分钟级时间序列，供前端看板实时展示和回放。
"""

import json
import logging
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from data_fetcher import (
    fetch_all_sectors_snapshot,
    generate_mock_dashboard_data,
    _build_trade_minutes,
    SECTOR_COLORS,
)

logger = logging.getLogger(__name__)


class SectorFlowCollector:
    """板块资金流向实时采集器。"""

    def __init__(self, poll_interval: float = 3.0):
        self._poll_interval = poll_interval
        self._snapshots: list = []
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._last_poll_time: float = 0.0
        self._consecutive_failures: int = 0
        self._max_failures_before_slowdown: int = 3
        self._trade_minutes = _build_trade_minutes()
        self._cache_dir = Path(__file__).parent / "data"
        self._cache_dir.mkdir(exist_ok=True)
        self._today = datetime.now().strftime("%Y%m%d")

    @property
    def cache_file(self) -> Path:
        return self._cache_dir / f"snapshots_{self._today}.json"

    def start(self):
        if self._running:
            return
        loaded = self._load_from_disk()
        if loaded:
            logger.info("从缓存恢复 %d 个快照", len(self._snapshots))
        self._running = True
        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()
        logger.info("采集器已启动 (间隔 %.1fs)", self._poll_interval)

    def stop(self):
        self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5.0)
        self._save_to_disk()
        logger.info("采集器已停止，共 %d 个快照", len(self._snapshots))

    def reset(self):
        with self._lock:
            self._snapshots.clear()
        self._consecutive_failures = 0
        logger.info("采集器已重置")

    def _poll_loop(self):
        while self._running:
            try:
                if self._is_market_open():
                    self._poll_once_and_store()
                    if len(self._snapshots) > 0 and len(self._snapshots) % 20 == 0:
                        self._save_to_disk()
                    time.sleep(self._poll_interval)
                else:
                    time.sleep(30)
                    new_today = datetime.now().strftime("%Y%m%d")
                    if new_today != self._today:
                        self._today = new_today
                        with self._lock:
                            self._snapshots.clear()
                        logger.info("日期切换至 %s，清空快照", new_today)
            except Exception as e:
                logger.error("采集循环异常: %s", e)
                time.sleep(5)

    def _poll_once_and_store(self):
        snapshot = fetch_all_sectors_snapshot(timeout=8.0)
        if snapshot is None:
            self._consecutive_failures += 1
            if self._consecutive_failures <= 3:
                logger.warning("API 轮询失败 (连续 %d 次)", self._consecutive_failures)
            return

        self._consecutive_failures = 0
        self._last_poll_time = time.time()

        now = datetime.now()
        minute_key = self._round_to_nearest_minute(now)
        if minute_key is None:
            return

        sectors = snapshot.get("sectors", [])
        if not sectors:
            return

        with self._lock:
            if self._snapshots and self._snapshots[-1]["time"] == minute_key:
                self._snapshots[-1] = {"time": minute_key, "rank": sectors}
            else:
                self._snapshots.append({"time": minute_key, "rank": sectors})

    @staticmethod
    def _is_market_open() -> bool:
        now = datetime.now()
        if now.weekday() > 4:
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

    def get_dashboard_data(self) -> dict:
        with self._lock:
            snapshots = list(self._snapshots)

        if len(snapshots) < 2:
            logger.info("快照不足 (%d)，降级为模拟数据", len(snapshots))
            return generate_mock_dashboard_data()

        minutes = [s["time"] for s in snapshots]

        all_sectors: dict[str, set] = {}
        for s in snapshots:
            for item in s["rank"]:
                name = item["name"]
                if name not in all_sectors:
                    all_sectors[name] = set()
                all_sectors[name].add(s["time"])

        series = {}
        for sector_name in all_sectors:
            snap_map = {s["time"]: s["rank"] for s in snapshots}
            values = []
            for t in minutes:
                rank = snap_map.get(t, [])
                found = next(
                    (item["net_main"] for item in rank if item["name"] == sector_name),
                    None,
                )
                values.append(found)
            color = SECTOR_COLORS.get(sector_name, "#666666")
            series[sector_name] = {
                "name": sector_name,
                "color": color,
                "times": minutes,
                "values": values,
            }

        latest_rank = snapshots[-1]["rank"] if snapshots else []
        rank_data = []
        for item in latest_rank:
            rank_data.append({
                "name": item["name"],
                "value": item["net_main"],
                "color": SECTOR_COLORS.get(item["name"], "#666666"),
            })
        rank_data.sort(key=lambda x: x["value"], reverse=True)

        return {
            "date": datetime.now().strftime("%Y-%m-%d"),
            "time_label": minutes[-1] if minutes else "15:00",
            "time_index": len(minutes) - 1,
            "total_times": len(minutes),
            "minutes": minutes,
            "rank": rank_data,
            "series": series,
        }

    def get_snapshot(self, time_idx: int) -> dict:
        data = self.get_dashboard_data()
        minutes = data["minutes"]
        series = data["series"]

        time_idx = max(0, min(time_idx, len(minutes) - 1))
        time_label = minutes[time_idx] if time_idx < len(minutes) else "15:00"

        snapshot_rank = []
        for sec_name, sec_data in series.items():
            vals = sec_data["values"]
            if time_idx < len(vals):
                val = vals[time_idx]
            else:
                val = vals[-1] if vals else 0
            if val is not None:
                snapshot_rank.append({
                    "name": sec_name,
                    "value": val,
                    "color": sec_data["color"],
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

    def get_status(self) -> dict:
        with self._lock:
            count = len(self._snapshots)
        return {
            "running": self._running,
            "snapshots_count": count,
            "last_poll_time": self._last_poll_time,
            "last_poll_iso": (
                datetime.fromtimestamp(self._last_poll_time).isoformat()
                if self._last_poll_time else None
            ),
            "consecutive_failures": self._consecutive_failures,
            "market_open": self._is_market_open(),
            "date": datetime.now().strftime("%Y-%m-%d"),
            "poll_interval": self._poll_interval,
        }

    def _save_to_disk(self):
        with self._lock:
            data = list(self._snapshots)
        if not data:
            return
        try:
            tmp = str(self.cache_file) + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            import os
            os.replace(tmp, str(self.cache_file))
        except Exception as e:
            logger.warning("快照缓存写入失败: %s", e)

    def _load_from_disk(self) -> bool:
        if not self.cache_file.exists():
            return False
        try:
            with open(self.cache_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, list) or len(data) == 0:
                return False
            with self._lock:
                self._snapshots = data
            logger.info("从缓存恢复 %d 个快照", len(data))
            return True
        except Exception as e:
            logger.warning("快照缓存读取失败: %s", e)
            return False
