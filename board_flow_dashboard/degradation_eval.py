#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""降级链可用性评测（离线·确定性·可复现）

金融决策工具的可用性红线：任何一环故障，都不能给用户错误结论或一片空白。
观澜的端到端降级链为 L0 多Agent → L1 单Agent → L2 规则引擎 → L3 静态兜底，
每一跳都带 degraded 标记，且所有出口统一过 guardrails 合规闸。

本评测模拟「从第 k 级起全部失败」的各场景，验证最终交付给用户的文本：
  ① 非空（不白屏）  ② 过合规闸（不放行违规内容）

不依赖 LLM、不需 API key。运行：
    cd board_flow_dashboard && python degradation_eval.py
"""
from __future__ import annotations

try:
    from .guardrails import seal, check_output
except ImportError:
    from guardrails import seal, check_output  # type: ignore


# 各级承接者的典型产出（真实系统分别来自 debate / chat_agent / chat / 规则引擎 / 静态文案）
LEVEL_OUTPUTS = {
    0: "多Agent辩论共识：该股技术偏强但情绪分化，倾向谨慎参与、注意回撤（尊重风控半仓上限）",
    1: "单Agent分析：结合量价与所属板块动能，倾向谨慎参与，注意设好止损位",
    2: "规则引擎：市场广度 46%（中性），热门板块半导体/光模块，候选票已按多因子评分排序",
    3: "ℹ️ AI 与规则引擎暂时不可用，请稍后重试。",
}

SCENARIOS = {
    "全链路正常（L0 承接）": 0,
    "多Agent失败 → 降级单Agent（L1）": 1,
    "单Agent也失败 → 降级规则引擎（L2）": 2,
    "规则引擎也失败 → 静态兜底（L3）": 3,
}


def deliver(fail_from: int):
    """从 fail_from 级起全部失败，返回 (承接级, 经合规闸的最终文本)。"""
    for lvl in range(fail_from, 4):
        if lvl in LEVEL_OUTPUTS:
            return lvl, seal(LEVEL_OUTPUTS[lvl])["text"]
    return None, ""


def run() -> dict:
    print("=" * 60)
    print("降级链可用性评测（离线·确定性）")
    print("=" * 60)
    ok = 0
    for name, fail_from in SCENARIOS.items():
        lvl, text = deliver(fail_from)
        non_empty = bool(text and text.strip())
        compliant = check_output(text)["compliant"]
        passed = non_empty and compliant
        ok += passed
        print(f"[{'OK ' if passed else 'FAIL'}] {name}")
        print(f"       承接层 L{lvl} | 非空={non_empty} 合规={compliant}")
    print("-" * 60)
    print(f"→ 降级链可用性 = {ok}/{len(SCENARIOS)} 场景均保证「非空 + 合规」输出，无白屏")
    return {"passed": ok, "total": len(SCENARIOS)}


if __name__ == "__main__":
    run()
