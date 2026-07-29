#!/usr/bin/env python3
"""
信号可信度反馈调节器（Weight Tuner）— 让规则引擎「自进化」

规则引擎的因子权重与信号阈值原本是专家拍脑袋定死的、不会随实盘表现变化。
本模块接上决策飞轮（decision_store）的信号级胜率数据，把「历史上哪些信号真的
赚钱」反向作用到选股评分：表现好的信号给正向乘数、表现差的降权。这是从
「静态硬编码规则」走向「数据反馈驱动的自适应规则」的关键一环。

设计原则（金融场景，稳健第一）：
  1. 可解释：每个乘数都能追溯到「某信号 N 笔、胜率 X%、均收益 Y%、收缩系数」，非黑盒。
  2. 有边界：乘数封顶在 [MIN_MULT, MAX_MULT]，防止过拟合/暴走。
  3. 可回滚：一个开关全局关闭；关闭后评分与调节前完全一致。
  4. 置信度收缩（v2）：不再用「样本过硬门槛就全信」，而是按样本量 n 平滑给信任度
     （收缩因子 n/(n+K)）——小样本自动往中性(乘数=1.0)拉，样本充分才逼近满额调节。
     这从统计上解决了「20 笔小样本胜率噪声被照单全收」的脆弱性。
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
MIN_SAMPLES = 5           # 参与调节的最低样本（v2：从硬门槛降为软门槛，5 笔起即可参与，
                          #   由「置信度收缩」按样本量自动决定实际调节力度，而非一刀切）
SHRINK_K = 20             # v2 置信度收缩常数：有效力度 = n/(n+K)。n=K 时信任 50%，
                          #   n≫K 才接近满额。小样本自动往中性(乘数=1.0)收缩，防噪声暴走
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


def _shrink_factor(n: int) -> float:
    """v2 置信度收缩因子 n/(n+K)：样本越少越接近 0（往中性拉），越多越接近 1。

    这是把旧的「样本过 20 硬门槛就全信」升级为「按样本量平滑给信任度」的核心。
    """
    n = max(0, int(n or 0))
    return n / (n + SHRINK_K) if (n + SHRINK_K) > 0 else 0.0


def _compute_multiplier(win_rate: float, avg_ret: float, n: int = 0) -> float:
    """把单个信号的胜率与均收益映射为 [MIN_MULT, MAX_MULT] 的乘数（v2：含置信度收缩）。

    胜率高于基准、均收益为正 → 乘数 > 1；反之 < 1。
    v2 关键改进：原始偏离先乘以置信度收缩因子 n/(n+K) 再叠加——样本少时调节量
    自动往中性(1.0)收缩，避免「20 笔小样本胜率噪声」被当成真实信号照单全收；
    样本充分时才逼近满额调节。兼顾可解释（仍可追溯胜率/均收益）与统计稳健。
    """
    raw_delta = ((win_rate - WINRATE_BASELINE) * WINRATE_SENSITIVITY
                 + (avg_ret - RETURN_BASELINE) * RETURN_SENSITIVITY)
    delta = raw_delta * _shrink_factor(n)   # v2：按样本置信度收缩
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
        m = _compute_multiplier(win_rate, avg_ret, n)   # v2：传入 n 做置信度收缩
        mults[combo] = m
        detail[combo] = {"n": n, "win_rate": win_rate, "avg_ret": avg_ret,
                         "shrink": round(_shrink_factor(n), 3), "mult": m}
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
        "shrink_k": SHRINK_K,
        "bounds": [MIN_MULT, MAX_MULT],
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)) if ts else None,
        "signals": [
            {"signal_combo": k, **v} for k, v in items
        ],
    }


# ── L4: Champion-Challenger 调权对照验证（样本外/时间切分）───────
# 问题（旧实现的致命缺陷）：用同一批已结算样本既算乘数又评估收益，是"用拟合
# 数据评估拟合结果"——高收益信号本就被 _compute_multiplier 赋更高乘数，challenger
# 数学上必然 ≥ champion，edge 无统计意义（自证循环）。
# 正解（本实现）：时间切分做真·样本外(out-of-sample)验证——
#   1) 按结算时间排序，前 TRAIN_FRAC 作训练集，用它算各信号乘数（= challenger 策略）；
#   2) 在留出的后半段（测试集，训练时不可见）上比：
#        champion   = 等权平均收益（不调权基线）
#        challenger = 用"训练集学到的乘数"加权测试集收益
#   3) challenger 在**没见过的**测试集上仍稳定跑赢，才说明调权真的有泛化价值。
# 这才是反事实评估，能拿去被量化/ML 背景的人推敲而不露怯。

PROMOTE_MIN_SAMPLES = 30   # 晋升所需最小(测试集)样本量
PROMOTE_MIN_EDGE = 0.3     # challenger 需领先 champion 的最小收益差(pp)才建议晋升
TRAIN_FRAC = 0.6           # 时间切分：前 60% 已结算样本作训练(算乘数)，后 40% 作样本外测试
MIN_TEST_PER_COMBO = 1     # 测试集里某信号至少几笔才纳入对照


def _multipliers_from_trades(trades: list) -> dict:
    """仅用给定(训练集)交易明细计算每个信号组合的乘数——与生产同一套映射，
    但只喂训练集数据，保证测试集对乘数不可见。"""
    agg: dict = {}
    for t in trades:
        combo = t.get("signal_combo", "")
        if not combo:
            continue
        a = agg.setdefault(combo, {"n": 0, "wins": 0, "sum_ret": 0.0})
        a["n"] += 1
        a["sum_ret"] += (t.get("return_pct") or 0.0)
        if (t.get("return_pct") or 0.0) > 0:
            a["wins"] += 1
    mults = {}
    for combo, a in agg.items():
        if a["n"] < MIN_SAMPLES:
            continue  # 训练样本不足的信号不调权（乘数=1，由 .get 默认兜底）
        win_rate = a["wins"] / a["n"] * 100.0
        avg_ret = a["sum_ret"] / a["n"]
        mults[combo] = _compute_multiplier(win_rate, avg_ret, a["n"])  # v2：同样带置信度收缩
    return mults


def evaluate_challenger(store) -> dict:
    """样本外 Champion-Challenger 对照：前段学乘数，后段(未见过)验证收益。

    返回 {champion_avg, challenger_avg, edge, n_samples, recommend_promote, reason,
          method, train_size, test_size}。回答"这套调权在没见过的数据上是否真更好"。
    """
    if store is None:
        return {"error": "无 store"}
    try:
        trades = store.get_settled_trades()  # 已按 exit_date 升序、含 signal_combo
    except Exception as e:
        return {"error": f"取样失败: {e}"}

    n = len(trades)
    if n < PROMOTE_MIN_SAMPLES:
        return {"n_samples": n, "recommend_promote": False,
                "reason": f"总样本不足({n}<{PROMOTE_MIN_SAMPLES})，无法做可信的样本外对照",
                "method": "out_of_sample_time_split",
                "promote_min_samples": PROMOTE_MIN_SAMPLES, "promote_min_edge": PROMOTE_MIN_EDGE}

    # 时间切分：前 TRAIN_FRAC 训练、后段测试（训练时不可见）
    split = max(1, int(n * TRAIN_FRAC))
    train, test = trades[:split], trades[split:]
    if len(test) < 1:
        return {"n_samples": n, "recommend_promote": False,
                "reason": "测试集为空，样本时间跨度不足", "method": "out_of_sample_time_split"}

    # 只用训练集学乘数（challenger 策略），测试集对此不可见
    mults = _multipliers_from_trades(train)

    champ_sum = champ_cnt = 0.0
    chall_num = chall_den = 0.0
    for t in test:
        combo = t.get("signal_combo", "")
        ret = t.get("return_pct") or 0.0
        champ_sum += ret          # champion：等权
        champ_cnt += 1
        m = mults.get(combo, 1.0)  # challenger：用训练集乘数加权
        chall_num += ret * m
        chall_den += m

    champion_avg = round(champ_sum / champ_cnt, 3) if champ_cnt else 0
    challenger_avg = round(chall_num / chall_den, 3) if chall_den else 0
    edge = round(challenger_avg - champion_avg, 3)

    if len(test) < PROMOTE_MIN_SAMPLES:
        recommend, reason = False, f"测试集样本不足({len(test)}<{PROMOTE_MIN_SAMPLES})，样本外结论不够可信"
    elif edge >= PROMOTE_MIN_EDGE:
        recommend, reason = True, f"样本外测试集上 challenger 领先 champion {edge}pp，调权有泛化价值，建议晋升"
    else:
        recommend, reason = False, f"样本外未稳定领先(edge={edge}pp<{PROMOTE_MIN_EDGE})，保持 champion 不晋升"

    return {
        "method": "out_of_sample_time_split",   # 明示口径：样本外时间切分
        "champion_avg": champion_avg,           # 测试集等权平均收益(pp)
        "challenger_avg": challenger_avg,        # 测试集按训练集乘数加权收益(pp)
        "edge": edge,
        "n_samples": n,
        "train_size": len(train),
        "test_size": len(test),
        "learned_multipliers": len(mults),      # 训练集学到多少个信号乘数
        "recommend_promote": recommend,
        "reason": reason,
        "promote_min_samples": PROMOTE_MIN_SAMPLES,
        "promote_min_edge": PROMOTE_MIN_EDGE,
    }
