#!/usr/bin/env python3
"""
LLM JSON 解析工具 —— 从模型输出里稳健地抽出第一个完整 JSON 对象。

为什么需要它：LLM 即便被要求「只输出 JSON」，仍可能夹带前后解释文字、
```json 代码围栏、或多个对象。旧实现用贪婪正则 `\\{.*\\}` 从第一个 `{`
匹配到最后一个 `}`，遇到「对象 + 后续解释里还有花括号」或多对象时会把中间
无关内容一起吞进去，导致 json.loads 失败、整条结果被丢弃。

这里用括号配平扫描（尊重字符串与转义）定位第一个**完整**对象，显著更稳。
"""

from __future__ import annotations

import json
import logging
from typing import Optional

logger = logging.getLogger(__name__)


def _strip_code_fence(text: str) -> str:
    """去掉 ```json ... ``` / ``` ... ``` 围栏，只保留内部内容。"""
    t = text.strip()
    if t.startswith("```"):
        # 去掉首行围栏（可能是 ```json）
        nl = t.find("\n")
        if nl != -1:
            t = t[nl + 1:]
        if t.rstrip().endswith("```"):
            t = t.rstrip()[:-3]
    return t


def _first_balanced_object(text: str) -> Optional[str]:
    """扫描出第一个括号配平的 {...} 子串；字符串内的花括号不计数。"""
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None  # 未配平（被截断）


def parse_json_object(raw: str) -> Optional[dict]:
    """从 LLM 原始输出中解析出第一个 JSON 对象；失败返回 None。

    尝试顺序：整体直接解析 → 去围栏后括号配平抽取 → 原样兜底。
    只接受 dict 结果（辩论/选股意见都是对象）。
    """
    if not raw or not raw.strip():
        return None
    # 1) 最理想：整体就是合法 JSON
    try:
        obj = json.loads(raw)
        return obj if isinstance(obj, dict) else None
    except (json.JSONDecodeError, ValueError):
        pass
    # 2) 去代码围栏后再括号配平抽取
    candidate = _first_balanced_object(_strip_code_fence(raw)) or _first_balanced_object(raw)
    if candidate:
        try:
            obj = json.loads(candidate)
            return obj if isinstance(obj, dict) else None
        except (json.JSONDecodeError, ValueError):
            logger.debug("括号配平抽取后仍解析失败")
    return None
