#!/usr/bin/env python3
"""
规则引擎参数中心（Engine Config）

把原先散落、写死在 stock_selector.py 里的因子权重、信号判定阈值、风控常量、
信号操作指引集中到一处，成为规则引擎参数的「唯一真源」。

设计目标：
  1. 参数外置：调参不再需要改选股主逻辑源码，改这里（或 JSON 覆盖）即可。
  2. 零行为变化：所有默认值与原 stock_selector.py 逐一对齐，纯搬运，不改数值。
  3. 可覆盖：支持 data/engine_config.json 覆盖任意默认值（便于实验/回测/线上热调），
     文件不存在时用内置默认值。
  4. 可回滚：JSON 只覆盖显式声明的键，删掉 JSON 即恢复默认。
  5. 向后兼容：stock_selector.py 仍暴露同名模块级变量（WEIGHTS/SIGNAL_GUIDE/...），
     position_watcher 等外部引用无需改动。

优先级：JSON 覆盖 > 内置默认。
"""

from __future__ import annotations

import copy
import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).parent / "data"
CONFIG_JSON = DATA_DIR / "engine_config.json"


# ═══════════════════════════════════════════════════════════════
# 内置默认参数（与原 stock_selector.py 逐一对齐，数值不变）
# ═══════════════════════════════════════════════════════════════

DEFAULTS = {
    # ── 多因子权重（v3.5：量价为主 60%，资金确认为辅 16%，日评融合 25%）──
    "weights": {
        "volume_ratio": 0.20,       # 量比 — 超短线量是王
        "price_deviation": 0.16,    # 智能偏离 — 相对板块强弱
        "net_main_ratio": 0.16,     # 主力净占比 — 确认信号（含流置信动态降权）
        "intraday_position": 0.12,  # 日内相对位置 — 入场时机
        "open_return": 0.10,        # 开盘涨幅 — 开盘定多空
        "turnover_rate": 0.10,      # 换手率 — 流动性
        "amp_ratio": 0.08,          # 振幅 — 盈利空间
        "short_momentum": 0.06,     # 短期动量（近5日涨幅）— 趋势延续性
        "breakout_dist": 0.04,      # 突破距离（距20日高点）— 空间判断
    },

    # ── 候选池/板块采样参数 ──
    "pool": {
        "hot_sector_count": 5,          # 资金流维度：每种板块类型取前 N 个
        "hot_sector_count_pct": 3,      # 价格动量维度：每种板块额外取前 N 个
        "stocks_per_sector": 50,        # 每个板块取前 N 只成分股
        "candidate_pool_size": 50,      # 候选池总容量（= A + B）
        "daily_score_weight": 0.25,     # 日评分在最终排名中的权重
        "max_per_sector": 8,            # A 池同一板块最多入选数
        "max_per_sector_b": 8,          # B 池同一板块最多入选数
        "max_stocks_per_mega_sector": 10,  # 同一大赛道总上限
        "ratio_a": 25,                  # A 池名额
        "ratio_b": 25,                  # B 池名额
        "pullback_bonus_multiplier": 1.10,  # B 池得分放大系数
    },

    # ── 大盘环境阈值 ──
    "market": {
        "bear_market_pool_size": 10,    # 普跌日候选池缩减（已废弃，保留兼容）
        "bear_market_threshold": 0.16,  # 上涨板块占比低于此值视为普跌
        "extreme_bear_threshold": 0.08, # 极端熊市阈值，A 池彻底禁用
        "bear_pullback_a": 25,          # 普跌时 A 池名额
        "bear_pullback_b": 25,          # 普跌时 B 池名额
    },

    # ── 三级系统性风控熔断 ──
    "risk": {
        "sector_meltdown_flow": -20,    # 板块主力净流出超此值(亿)触发熔断
        "sector_meltdown_pct": -3.0,    # 板块跌幅超此值(%)触发熔断
        "sector_meltdown_penalty": 0.5, # 熔断板块成分股得分乘以此系数
        "flash_crash_pct": -5.0,        # 个股盘中急跌超此值(%)直接剔除
        "flash_crash_open_gap": -4.0,   # 开盘后跌幅超此值(%)判定急跌
        # v4.6 板块退潮柔性降权（治追高，填补硬熔断之前的中间地带）
        "sector_fade_penalty": 0.85,    # 板块退潮成分股得分乘以此系数
        "sector_fade_trends": ["加速流出", "温和流出"],  # 判定为退潮的 trend 取值
    },

    # ── 流动性硬过滤 ──
    "liquidity": {
        "min_market_cap_a": 30,   # A 池最小流通市值（亿元）
        "min_market_cap_b": 20,   # B 池最小流通市值（亿元）
        "min_turnover_rate": 0.5, # 最低换手率（%）
    },

    # ── A 池信号判定阈值（对应 _infer_signal 的 if/elif 分支）──
    # 每条规则是 (信号名, {因子: (算子, 阈值)...})，按列表顺序优先匹配。
    # 算子：gt=>, lt=<, ge=>=, le=<=。第一个全部满足的规则即命中。
    "signal_rules_a": [
        ["放量上攻", {"net_main_ratio": ["gt", 15], "volume_ratio": ["gt", 1.5], "pct_chg": ["gt", 3], "intraday_position": ["gt", 0.6]}],
        ["放量突破", {"volume_ratio": ["gt", 2], "pct_chg": ["gt", 2], "open_return": ["gt", 1]}],
        ["补涨潜力", {"net_main_ratio": ["gt", 10], "price_deviation": ["lt", -1], "intraday_position": ["lt", 0.5]}],
        ["资金驱动", {"net_main_ratio": ["gt", 8], "intraday_position": ["gt", 0.7]}],
        ["量价齐升", {"volume_ratio": ["gt", 1.5], "net_main_ratio": ["gt", 5]}],
        ["弱势回避", {"price_deviation": ["lt", -2], "net_main_ratio": ["lt", 0]}],
        ["滞涨关注", {"price_deviation": ["lt", -2], "net_main_ratio": ["ge", 0]}],
        ["温和吸筹", {"net_main_ratio": ["gt", 3]}],
        ["高位风险", {"intraday_position": ["gt", 0.85], "net_main_ratio": ["lt", 0]}],
    ],
    "signal_default_a": "盘中观察",

    # ── B 池信号判定阈值（对应 _infer_signal_pullback）──
    # 匹配失败时回退到 A 池信号规则。
    "signal_rules_b": [
        ["主线分歧低吸", {"net_main_ratio": ["gt", 10], "price_deviation": ["lt", 0], "intraday_position": ["lt", 0.5], "pct_chg": ["lt", 2]}],
        ["板块洗盘承接", {"net_main_ratio": ["gt", 5], "volume_ratio": ["gt", 1.2], "price_deviation": ["gt", -3], "pct_chg": ["lt", 3]}],
        ["缩量止跌企稳", {"volume_ratio": ["lt", 1.2], "intraday_position": ["gt", 0.3], "pct_chg": ["gt", -3], "open_return": ["lt", 1]}],
    ],

    # ── 信号操作指引（止盈止损/持有天数）──
    "signal_guide": {
        "放量上攻":   {"holding": 2,     "take_profit": "4-6%", "stop_loss": "-3%"},
        "放量突破":   {"holding": 2,     "take_profit": "4-6%", "stop_loss": "-3%"},
        "补涨潜力":   {"holding": "3-4", "take_profit": "6-8%", "stop_loss": "-4%"},
        "资金驱动":   {"holding": 2,     "take_profit": "5%",   "stop_loss": "-3.5%"},
        "量价齐升":   {"holding": "2-3", "take_profit": "5%",   "stop_loss": "-3.5%"},
        "温和吸筹":   {"holding": "3-4", "take_profit": "6-8%", "stop_loss": "-4%"},
        "滞涨关注":   {"holding": 3,     "take_profit": "4%",   "stop_loss": "-3%"},
        "弱势回避":   {"holding": 0,     "take_profit": "-",    "stop_loss": "-"},
        "高位风险":   {"holding": 0,     "take_profit": "-",    "stop_loss": "-"},
        "盘中观察":   {"holding": "2-3", "take_profit": "4%",   "stop_loss": "-3%"},
        "主线分歧低吸": {"holding": "3-4", "take_profit": "7-9%", "stop_loss": "-4.5%"},
        "板块洗盘承接": {"holding": 3,     "take_profit": "6%",   "stop_loss": "-3.5%"},
        "缩量止跌企稳": {"holding": "3-4", "take_profit": "6-8%", "stop_loss": "-4%"},
    },
}


# ═══════════════════════════════════════════════════════════════
# 加载与合并
# ═══════════════════════════════════════════════════════════════

def _deep_merge(base: dict, override: dict) -> dict:
    """把 override 深度合并进 base 的副本（只覆盖显式声明的键）。"""
    result = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(result.get(k), dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = v
    return result


def _load_config() -> dict:
    """加载配置：JSON 覆盖（若存在）> 内置默认。"""
    if CONFIG_JSON.exists():
        try:
            override = json.loads(CONFIG_JSON.read_text(encoding="utf-8"))
            merged = _deep_merge(DEFAULTS, override)
            logger.info("规则引擎参数：已加载 JSON 覆盖 %s", CONFIG_JSON.name)
            return merged
        except Exception as e:
            logger.warning("规则引擎 JSON 覆盖加载失败(%s)，回退默认值", e)
    return copy.deepcopy(DEFAULTS)


_CONFIG = _load_config()


def get_config() -> dict:
    """返回当前生效的完整配置（已合并 JSON 覆盖）。"""
    return _CONFIG


def reload_config() -> dict:
    """重新从磁盘加载配置（用于热更新/测试）。"""
    global _CONFIG
    _CONFIG = _load_config()
    return _CONFIG


# ── 信号规则求值（把 (算子,阈值) 声明转成判定）──────────────────
_OPS = {
    "gt": lambda a, b: a > b,
    "lt": lambda a, b: a < b,
    "ge": lambda a, b: a >= b,
    "le": lambda a, b: a <= b,
}


def match_signal(stock: dict, rules: list, default: str = "") -> str:
    """按规则表顺序匹配信号：第一条所有条件都满足的规则即命中。

    stock 字段缺失或为假值(0/None)按缺省处理，与原 `s.get(k) or 0`
    （intraday_position 为 `or 0.5`）逐一对齐——注意用 `or` 而非 `is None`：
    真实值 0.0 也会被替换为缺省，这是与旧 if/elif 链保持零行为变化的关键。
    """
    for name, conds in rules:
        ok = True
        for factor, (op, thr) in conds.items():
            default_val = 0.5 if factor == "intraday_position" else 0
            val = stock.get(factor) or default_val
            if not _OPS[op](val, thr):
                ok = False
                break
        if ok:
            return name
    return default
