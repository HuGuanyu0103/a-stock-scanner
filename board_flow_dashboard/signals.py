#!/usr/bin/env python3
"""
信号存储模块 — 多系统协作的共享内存

三个系统各自写入，聚合器统一消费。通过 threading.Lock 保证线程安全。
每个信号带 status 和 updated_at，聚合器根据新鲜度动态分配权重。
"""

import json
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = __import__('logging').getLogger(__name__)

DATA_DIR = Path(__file__).parent / "data"

# ── 信号配置 ────────────────────────────────────────────────

SIGNAL_CONFIG = {
    "tech_fund": {
        "weight": 0.70,        # 正常权重
        "max_age": 120,        # 最大新鲜度（秒）
        "description": "技术+资金面（System A）",
    },
    "sentiment": {
        "weight": 0.20,
        "max_age": 600,        # 10分钟
        "description": "情绪面（System B）",
    },
    "news": {
        "weight": 0.10,
        "max_age": 1800,        # 30分钟
        "description": "消息面（System C）",
    },
}

# ── 全局信号存储 ─────────────────────────────────────────────

class SignalStore:
    """线程安全的信号存储。

    使用方式:
        store = get_signal_store()
        store.update("sentiment", {"market": {...}, "sectors": {...}})
        signals = store.get_all()
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._store: dict[str, dict] = {
            "tech_fund": {"status": "stale", "updated_at": None, "data": {}},
            "sentiment": {"status": "stale", "updated_at": None, "data": {}},
            "news": {"status": "stale", "updated_at": None, "data": {}},
        }
        self._file = DATA_DIR / "signals.json"

    def update(self, system: str, data: dict):
        """更新某个系统的信号。"""
        with self._lock:
            self._store[system] = {
                "status": "ok",
                "updated_at": datetime.now().strftime("%H:%M:%S"),
                "data": data,
            }
            logger.debug("SignalStore: %s updated", system)

    def set_error(self, system: str):
        """标记某系统出错。"""
        with self._lock:
            if self._store[system]["status"] != "stale":
                self._store[system]["status"] = "error"
                logger.warning("SignalStore: %s set to error", system)

    def get_all(self) -> dict:
        """获取所有系统的信号状态，含新鲜度检查。"""
        with self._lock:
            result = {}
            now = time.time()
            for name, entry in self._store.items():
                status = entry["status"]
                if status == "ok" and entry["updated_at"]:
                    try:
                        t = datetime.strptime(entry["updated_at"], "%H:%M:%S")
                        age = (datetime.now() - t).total_seconds()
                        if age > SIGNAL_CONFIG[name]["max_age"]:
                            status = "stale"
                    except ValueError:
                        status = "stale"
                result[name] = {
                    "status": status,
                    "updated_at": entry["updated_at"],
                    "data": entry["data"],
                }
            return result

    def get_effective_weights(self) -> dict[str, float]:
        """计算有效权重：stale/error 系统的权重按比例分给活着的。"""
        signals = self.get_all()
        alive = {k for k, v in signals.items() if v["status"] == "ok"}

        if not alive:
            return {"tech_fund": 1.0}  # 全挂了，回退纯技术面

        dead = set(SIGNAL_CONFIG) - alive
        dead_weight = sum(SIGNAL_CONFIG[k]["weight"] for k in dead)

        effective = {}
        alive_weight = sum(SIGNAL_CONFIG[k]["weight"] for k in alive)
        for k in alive:
            # 活着的系统按比例分配自己的权重 + 死亡系统的权重
            redistribution = dead_weight * (SIGNAL_CONFIG[k]["weight"] / alive_weight)
            effective[k] = SIGNAL_CONFIG[k]["weight"] + redistribution

        # 归一化
        total = sum(effective.values())
        return {k: round(v / total, 4) for k, v in effective.items()}

    def persist(self):
        """持久化到 JSON 文件（用于灾备恢复）。"""
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            with self._lock:
                with open(self._file, "w", encoding="utf-8") as f:
                    json.dump(self._store, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning("SignalStore persist failed: %s", e)

    def load_persisted(self):
        """从 JSON 文件恢复（启动时调用）。"""
        try:
            if self._file.exists():
                with open(self._file, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                with self._lock:
                    for k, v in loaded.items():
                        if k in self._store:
                            v["status"] = "stale"  # 恢复的数据标记为 stale
                            self._store[k] = v
                logger.info("SignalStore: loaded persisted signals")
        except Exception as e:
            logger.warning("SignalStore load failed: %s", e)

    def start_auto_persist(self, interval: int = 60):
        """启动后台定期持久化线程。"""
        import threading as _threading

        def _persist_loop():
            while True:
                time.sleep(interval)
                self.persist()

        t = _threading.Thread(target=_persist_loop, daemon=True, name="signal_persist")
        t.start()


# ── 全局单例 ──────────────────────────────────────────────────

_store: Optional[SignalStore] = None


def get_signal_store() -> SignalStore:
    global _store
    if _store is None:
        _store = SignalStore()
        _store.load_persisted()
    return _store


# ── 聚合计算 ──────────────────────────────────────────────────

def compute_final_score(stock: dict, signals: dict,
                         effective_weights: dict) -> float:
    """根据三系统信号计算单只股票的最终评分。

    Args:
        stock: 股票 dict（含 score, sector 等字段）
        signals: SignalStore.get_all() 的返回值
        effective_weights: get_effective_weights() 的返回值

    Returns:
        聚合后的最终评分
    """
    base = stock.get("score", 0)
    sector = stock.get("sector", "")

    # ── 技术面 (基础分) ──────────────────────────────────────
    tech_weight = effective_weights.get("tech_fund", 0.70)
    final = base * tech_weight

    # ── 情绪面 (板块热度加成 + 轮动乘数) ─────────────────────
    sent_data = signals.get("sentiment", {}).get("data", {})
    sent_weight = effective_weights.get("sentiment", 0)

    if sent_weight > 0 and sent_data:
        market = sent_data.get("market", {})

        # 轮动乘数：轮动快→降 base，轮动慢→不变
        rotation = market.get("rotation_speed", 0.5)
        rotation_mult = 1.0 - rotation * 0.3  # rotation=0→1.0, rotation=1→0.7

        # ── v4.0: 情绪全局乘数 ──────────────────────────────
        sentiment_index = market.get("sentiment_index", 50)
        pool = stock.get("pool", "A")

        if pool == "A" and sentiment_index > 80:
            # 情绪过热：抑制追涨冲动
            final *= 0.8
        elif pool == "B" and sentiment_index < 30:
            # 情绪冰点：鼓励左侧布局
            final *= 1.2

        # ── v4.0: 市场炸板率惩罚 ────────────────────────────
        po_ban_rate = market.get("po_ban_rate", 0)
        if po_ban_rate > 0.4:
            final -= 0.1

        # 板块涨停热度加成
        sector_heat = sent_data.get("sector_heat", {}).get(sector, {})
        heat_score = sector_heat.get("heat_score", 0)
        sector_bonus = 0
        if heat_score >= 0.8:
            sector_bonus = 0.08
        elif heat_score >= 0.6:
            sector_bonus = 0.05
        elif heat_score >= 0.3:
            sector_bonus = 0.02

        final = final * rotation_mult + sector_bonus
        final += base * sent_weight * 0.1  # 情绪面的直接贡献

    # ── v4.0: 消息面 (逻辑验证+风险过滤，非追涨触发) ─────────
    news_data = signals.get("news", {}).get("data", {})
    news_weight = effective_weights.get("news", 0)

    if news_weight > 0 and news_data:
        news_bonus = 0.0
        now = datetime.now()
        for event in news_data.get("events", []):
            # 相关性检查
            if sector in event.get("sectors", []) or stock.get("code") in event.get("stocks", []):
                # v4.0: 按事件级别分级衰减
                decay_h = event.get("decay_hours", 2)
                try:
                    t = datetime.strptime(event.get("time", ""), "%H:%M")
                    age_h = (now - t).total_seconds() / 3600
                except (ValueError, TypeError):
                    age_h = 1.0
                freshness = max(0, 1 - age_h / decay_h)
                sentiment = 1 if event.get("sentiment") == "positive" else -1
                impact = event.get("impact", 0.3)
                news_bonus += sentiment * impact * freshness * 0.10
        final += max(-0.10, min(0.10, news_bonus))

    # ── 情绪面策略调节 (A/B 池分配) ──────────────────────────
    # 由轮动速度决定 A/B 池比例，但不改个股分，只改 pool_size
    # 这个逻辑在 stock_selector 的 _select_stocks_real 里实现

    return round(final, 4)


def get_pool_allocation(signals: dict) -> dict:
    """根据情绪面数据决定 A/B 池名额分配。

    Returns:
        {"pool_a": int, "pool_b": int, "risk_multiplier": float}
    """
    sent_data = signals.get("sentiment", {}).get("data", {})
    market = sent_data.get("market", {}) if sent_data else {}

    rotation = market.get("rotation_speed", 0.5)
    breadth = market.get("market_breadth", 0.5)

    # v4.0: rotation 边界处理
    # rotation=1.0 可能来自两方面：(1) compute_rotation_speed 返回的极快轮动信号
    # (2) 数据不足时的兜底。后者由 SentimentCollector 在 None→0.5 处统一处理。
    # 此处 rotation∈[0,1] 已是有效值，不做二次兜底。
    rotation = max(0.0, min(1.0, rotation))

    if rotation < 0.3:
        # 轮动慢：领头羊稳固，追涨为主
        a_size = 35
        b_size = 15
        risk_mult = 1.0
    elif rotation < 0.6:
        # 轮动中：攻守平衡
        a_size = 25
        b_size = 25
        risk_mult = 0.9
    elif rotation < 0.85:
        # 轮动快：谨慎追涨，多低吸
        a_size = 15
        b_size = 35
        risk_mult = 0.7
    else:
        # 极快轮动：只防守
        a_size = 5
        b_size = 45
        risk_mult = 0.5

    # 广度修正：广度极低时进一步保守
    if breadth < 0.16:
        risk_mult *= 0.8
        a_size = max(0, a_size - 5)
        b_size = min(50, b_size + 5)

    return {
        "pool_a": a_size,
        "pool_b": b_size,
        "risk_multiplier": round(risk_mult, 2),
    }
