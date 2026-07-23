#!/usr/bin/env python3
"""
信号可信度反馈调节器（Weight Tuner）— 让规则引擎「自进化」

规则引擎的因子权重与信号阈值原本是专家拍脑袋定死的、不会随实盘表现变化。
本模块接上决策飞轮（decision_store）的信号级胜率数据，把「历史上哪些信号真的
赚钱」反向作用到选股评分：表现好的信号给正向乘数、表现差的降权。这是从
「静态硬编码规则」走向「数据反馈驱动的自适应规则」的关键一环。

设计原则（金融场景，稳健第一）：
  1. 可解释：每个乘数都能追溯到「某信号 N 笔、胜率 X%、均收益 Y%」，非黑盒。
  2. 有边界：样本不足不调；乘数封顶在 [MIN_MULT, MAX_MULT]，防止过拟合/暴走。
  3. 可回滚：一个开关全局关闭；关闭后评分与调节前完全一致。
  4. 平滑：用胜率与盈亏综合打分，线性映射到乘数，避免阶跃式剧烈变动。
  5. 冷启动安全：无数据时所有乘数=1.0（等于不调节），不影响新系统运行。

数据来源：decision_store.get_signal_win_rates() → [{signal_combo,total_trades,win_rate,avg_ret}]
作用点：stock_selector._score_and_rank 中，个股拿到 signal 后按 pool-signal 组合取乘数微调 score。
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Optional

logger = logging.getLogger(__name__)

# ── 调节边界参数（保守设定，宁可少调也不激进）──────────────────
ENABLED = True            # 全局开关：False 时所有乘数=1.0（等于关闭反馈闭环）
MIN_SAMPLES = 5           # 信号组合累计结算 < 此值不参与调节（样本不足不可信）
MIN_MULT = 0.85           # 乘数下限（表现最差的信号最多降权到 0.85）
MAX_MULT = 1.15           # 乘数上限（表现最好的信号最多加权到 1.15）
CACHE_TTL = 1800.0        # 乘数缓存 30 分钟，避免每次选股都查库

# 综合评分基准线：胜率 50% + 均收益 0% 视为「中性」，乘数=1.0
WINRATE_BASELINE = 50.0   # %
RETURN_BASELINE = 0.0     # %
# 评分→乘数的敏感度：胜率每偏离基准 1%、均收益每偏离 1%，各自的影响权重
WINRATE_SENSITIVITY = 0.006   # 胜率偏离 ±25% → ±0.15（封顶）
RETURN_SENSITIVITY = 0.02     # 均收益偏离 ±7.5% → ±0.15（封顶）


_cache: dict = {"mults": {}, "ts": 0.0, "detail": {}}
_lock = threading.Lock()


def _compute_multiplier(win_rate: float, avg_ret: float) -> float:
    """把单个信号的胜率与均收益映射为 [MIN_MULT, MAX_MULT] 的乘数。

    胜率高于基准、均收益为正 → 乘数 > 1；反之 < 1。两者线性叠加后裁剪到边界。
    """
    delta = ((win_rate - WINRATE_BASELINE) * WINRATE_SENSITIVITY
             + (avg_ret - RETURN_BASELINE) * RETURN_SENSITIVITY)
    mult = 1.0 + delta
    return round(max(MIN_MULT, min(MAX_MULT, mult)), 4)


def _refresh(store) -> None:
    """从决策飞轮拉取信号胜率，重算乘数缓存。失败静默保持旧缓存。"""
    try:
        rows = store.get_signal_win_rates()
    except Exception as e:
        logger.warning("weight_tuner 拉取胜率失败: %s", e)
        return
    mults, detail = {}, {}
    for r in rows or []:
        combo = r.get("signal_combo", "")
        n = r.get("total_trades", 0) or 0
        if not combo or n < MIN_SAMPLES:
            continue
        win_rate = r.get("win_rate", 0) or 0
        avg_ret = r.get("avg_ret", 0) or 0
        m = _compute_multiplier(win_rate, avg_ret)
        mults[combo] = m
        detail[combo] = {"n": n, "win_rate": win_rate, "avg_ret": avg_ret, "mult": m}
    with _lock:
        _cache["mults"] = mults
        _cache["detail"] = detail
        _cache["ts"] = time.time()
    if mults:
        logger.info("weight_tuner 刷新: %d 个信号有反馈乘数", len(mults))


def get_multipliers(store=None, force: bool = False) -> dict:
    """返回 {signal_combo: multiplier}。带 TTL 缓存；store 为 None 时不刷新只读缓存。"""
    if not ENABLED:
        return {}
    now = time.time()
    with _lock:
        fresh = (now - _cache["ts"]) < CACHE_TTL and _cache["mults"] is not None
    if store is not None and (force or not fresh):
        _refresh(store)
    with _lock:
        return dict(_cache["mults"])


def get_signal_multiplier(pool: str, signal: str, store=None) -> float:
    """取某 (池, 信号) 组合的评分乘数；无反馈数据或关闭时返回 1.0（不调节）。"""
    if not ENABLED:
        return 1.0
    mults = get_multipliers(store)
    combo = f"{pool}-{signal}"
    return mults.get(combo, 1.0)


def get_tuning_report(store=None) -> dict:
    """返回可解释的调节明细，供前端/API 展示「引擎因何自调」。"""
    get_multipliers(store)  # 触发按需刷新
    with _lock:
        detail = dict(_cache["detail"])
        ts = _cache["ts"]
    items = sorted(detail.items(), key=lambda kv: kv[1]["mult"], reverse=True)
    return {
        "enabled": ENABLED,
        "min_samples": MIN_SAMPLES,
        "bounds": [MIN_MULT, MAX_MULT],
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)) if ts else None,
        "signals": [
            {"signal_combo": k, **v} for k, v in items
        ],
    }
