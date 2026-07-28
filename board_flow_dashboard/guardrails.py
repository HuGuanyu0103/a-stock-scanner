#!/usr/bin/env python3
"""
输出合规护栏（Guardrails）—— 金融 AI 的信任与合规红线层

与 validate_output（质量层：查是否有数字/占位符/模糊词）职责不同：本模块是**合规层**，
把「AI 荐股工具在中国的监管红线」编码为可执行检查，作用于所有面向用户的自然语言输出
与辩论结论。核心不是让 AI 更准，而是让它**不越界、可信任、可交付**。

三道防线：
  1. 输入侧 prompt 注入清洗（sanitize_user_input）：用户输入拼进 prompt 前先消毒，
     剥离"忽略以上指令/你现在是…/system:"等越权片段，降低注入面。
  2. 输出侧合规扫描（check_output）：拦截违规承诺（保证收益/包涨/涨停/内幕/全仓梭哈），
     命中即判定不合规并给出可用于「重写」的问题清单。
  3. 交付侧免责注入（enforce）：合规输出统一追加标准风险提示/免责声明，
     并对轻微违规做**软性改写**（把"必涨/保证"等措辞降级为条件表述）。

设计原则：可解释（每次拦截都能说清命中哪条红线）、可配置（红线词表集中）、
零依赖（纯字符串规则，不额外调用 LLM，零成本零延迟）。
"""

from __future__ import annotations

import re
from typing import Optional

# ── 合规红线词表（命中即违规，硬拦截）────────────────────────────
# 违规承诺类：中国证券投顾强监管，AI 工具严禁承诺收益/涨停/包赚
FORBIDDEN_PROMISE = [
    "保证收益", "保证盈利", "稳赚", "稳赢", "包赚", "必赚", "必涨", "肯定涨",
    "一定涨", "包涨", "涨停可期", "明天涨停", "锁定涨停", "必然涨停",
    "无风险", "零风险", "稳赚不赔", "只赚不赔", "翻倍", "financial freedom",
]
# 违规诱导类：怂恿满仓/借贷/梭哈
FORBIDDEN_INCITE = [
    "全仓", "满仓干", "梭哈", "融资加杠杆", "借钱买", "抵押", "all in", "allin",
]
# 违规信息源类：内幕/操纵
FORBIDDEN_ILLEGAL = ["内幕消息", "内幕信息", "坐庄", "操纵股价", "老鼠仓", "拉高出货"]

# 软性改写映射：轻度过度措辞 → 合规的条件表述（不改语义只降绝对性）
SOFTEN_MAP = {
    "必涨": "有上涨机会（不确定）",
    "肯定上涨": "存在上涨可能",
    "一定能涨": "存在上涨可能",
    "绝对安全": "相对稳健（仍有风险）",
    "闭眼买": "可关注（需自行判断）",
}

# 标准免责声明（交付侧统一追加）
DISCLAIMER = (
    "\n\n———\n⚠️ 风险提示：以上为基于量化数据的分析参考，不构成投资建议。"
    "股市有风险，入市需谨慎；请结合自身风险承受能力独立决策，盈亏自负。"
)

# prompt 注入常见越权片段（输入侧清洗）
_INJECTION_PATTERNS = [
    r"(?i)ignore\s+(all\s+)?(previous|above|prior)\s+instructions?",
    r"(?i)disregard\s+(all\s+)?(previous|above)\s+",
    r"忽略(以上|之前|前面)(所有)?指令",
    r"忘记(你|之前)的?(设定|指令|角色)",
    r"(?i)you\s+are\s+now\s+",
    r"你现在是[一]?[个名]",
    r"(?i)system\s*[:：]",
    r"(?i)<\s*/?\s*system\s*>",
    r"(?i)new\s+(instructions?|rules?)\s*[:：]",
]


def sanitize_user_input(text: str, max_len: int = 2000) -> str:
    """输入侧清洗：剥离 prompt 注入片段 + 截断超长，用于拼进 prompt 前。

    注意：只做「降注入面」的保守清洗，不改变正常用户提问语义。
    """
    if not text:
        return ""
    cleaned = text
    for pat in _INJECTION_PATTERNS:
        cleaned = re.sub(pat, "［已过滤］", cleaned)
    cleaned = cleaned.strip()
    if len(cleaned) > max_len:
        cleaned = cleaned[:max_len] + "…"
    return cleaned


def check_output(text: str) -> dict:
    """输出侧合规扫描。返回 {compliant: bool, violations: [...], categories: [...]}。

    命中任一硬红线即 compliant=False，violations 可直接喂给「合规重写」流程。
    """
    if not text:
        return {"compliant": True, "violations": [], "categories": []}
    violations, categories = [], set()
    for w in FORBIDDEN_PROMISE:
        if w in text:
            violations.append(f"承诺收益/涨停类违规措辞：『{w}』")
            categories.add("promise")
    for w in FORBIDDEN_INCITE:
        if w in text:
            violations.append(f"诱导重仓/杠杆类违规：『{w}』")
            categories.add("incite")
    for w in FORBIDDEN_ILLEGAL:
        if w in text:
            violations.append(f"违规信息源/操纵类：『{w}』")
            categories.add("illegal")
    return {"compliant": len(violations) == 0,
            "violations": violations, "categories": sorted(categories)}


def soften(text: str) -> tuple[str, list]:
    """对轻度过度措辞做软性改写（降绝对性），返回 (新文本, 改写记录)。"""
    changes = []
    out = text or ""
    for bad, good in SOFTEN_MAP.items():
        if bad in out:
            out = out.replace(bad, good)
            changes.append(f"『{bad}』→『{good}』")
    return out, changes


def enforce(text: str, add_disclaimer: bool = True) -> dict:
    """交付侧总闸：软改写 → 合规扫描 → （合规则）追加免责声明。

    返回 {text, compliant, violations, softened, disclaimer_added}。
      - 若命中硬红线（compliant=False）：不追加免责声明，交由上层触发合规重写或拦截，
        避免"违规内容 + 免责声明"这种自欺组合。
      - 若仅轻度措辞：已软改写，视为合规，追加免责声明后交付。
    """
    softened_text, changes = soften(text or "")
    scan = check_output(softened_text)
    result = {
        "text": softened_text,
        "compliant": scan["compliant"],
        "violations": scan["violations"],
        "categories": scan["categories"],
        "softened": changes,
        "disclaimer_added": False,
    }
    if scan["compliant"] and add_disclaimer and DISCLAIMER.strip() not in softened_text:
        result["text"] = softened_text + DISCLAIMER
        result["disclaimer_added"] = True
    return result


def rewrite_hint(violations: list) -> str:
    """把 violations 组织成给 LLM 的合规重写指令（供自我纠错复用）。"""
    if not violations:
        return ""
    return ("你上一条回答触犯了金融合规红线：" + "；".join(violations) +
            "。请重写：绝对不得承诺收益/涨停/包赚，不得诱导满仓/借贷/杠杆，"
            "不得提及内幕或操纵；改用『有机会/需注意风险/可关注但自行决策』等合规、"
            "中性、可执行的表述，保留有价值的分析结论。")
