#!/usr/bin/env python3
"""
消息面监控 (System C) — 独立线程运行

两档机制：
  第一档（规则引擎，每 60s）：标题关键词 → 快速分类，无 LLM
  第二档（LLM 增强，每 30min）：DeepSeek-V3 批量深度分析
  第三档（LLM 突发，事件驱动）：标题含"突发/重磅" → DeepSeek-R1 推理

输出：写入 SignalStore
"""

import json
import logging
import threading
import time
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Optional

import requests as req
import urllib3
urllib3.disable_warnings()

logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).parent / "data"
NEWS_CHECK_INTERVAL = 60       # 规则引擎检查间隔（秒）
LLM_BATCH_INTERVAL = 600       # LLM 批量分析间隔（秒），v4.0 从 1800 缩短

# ── v4.0: 消息事件级别与衰减周期 ───────────────────────────
EVENT_LEVEL_CONFIG = {
    "major_policy": {"decay_hours": 8, "impact_base": 0.8, "label": "重大政策"},
    "industry_news": {"decay_hours": 2, "impact_base": 0.5, "label": "行业新闻"},
    "stock_announcement": {"decay_hours": 0.5, "impact_base": 0.3, "label": "个股公告"},
}

# 重大政策关键词
MAJOR_POLICY_KW = [
    "降准", "降息", "政治局", "国务院", "央行", "证监会", "发改委",
    "财政", "货币", "LPR", "MLF", "逆回购", "国常会", "深改委",
    "中央经济", "两会", "五年规划", "专项债", "特别国债",
]

# 个股层面关键词
STOCK_LEVEL_KW = [
    "业绩预告", "减持", "增持", "回购", "问询函", "警示函",
    "立案", "处罚", "ST", "退市", "重组", "停牌", "复牌",
]

# 共享 Session
_http = req.Session()
_http.trust_env = False
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Referer": "https://www.cls.cn/",
}

# ── 关键词词典 ────────────────────────────────────────────────

POSITIVE_KW = [
    "利好", "涨停", "突破", "签约", "中标", "回购", "增持", "业绩预增",
    "扭亏", "扩产", "涨价", "新产品", "获批", "合作", "定增", "重组",
    "政策支持", "补贴", "超预期", "订单", "量产", "突破性",
]

NEGATIVE_KW = [
    "利空", "跌停", "减持", "调查", "处罚", "退市", "亏损", "预亏",
    "爆雷", "违约", "诉讼", "冻结", "业绩下滑", "裁员", "停产",
    "降价", "竞争加剧", "监管", "问询函", "警示函",
]

SECTOR_KW_MAP = {
    "芯片": ["半导体", "AI芯片", "存储芯片"],
    "AI": ["AI芯片", "AI语料", "ChatGPT概念", "多模态AI", "算力"],
    "光伏": ["光伏设备", "绿色电力"],
    "锂电": ["锂电池", "锂矿概念", "固态电池", "麒麟电池"],
    "新能源车": ["新能源车", "华为汽车"],
    "军工": ["军工", "商业航天", "低空经济"],
    "医药": ["创新药", "医药商业", "医美概念"],
    "消费电子": ["消费电子", "电子竞技", "裸眼3D"],
    "机器人": ["人形机器人", "机器人执行器"],
    "通信": ["光通信模块", "通信设备", "5G", "6G"],
    "金融": ["证券", "银行", "保险"],
    "资源": ["稀缺资源", "黄金概念", "稀土永磁", "小金属概念", "有色金属"],
    "白酒": ["白酒", "食品饮料"],
    "数据": ["数据中心", "数据确权", "Web3.0"],
    "游戏": ["网络游戏", "短剧互动游戏", "电子竞技"],
}


def _classify_event_level(title: str) -> tuple:
    """根据标题关键词判断事件级别。

    Returns:
        (event_level, decay_hours, impact_base)
    """
    for kw in MAJOR_POLICY_KW:
        if kw in title:
            level = "major_policy"
            return level, EVENT_LEVEL_CONFIG[level]["decay_hours"], \
                   EVENT_LEVEL_CONFIG[level]["impact_base"]
    for kw in STOCK_LEVEL_KW:
        if kw in title:
            level = "stock_announcement"
            return level, EVENT_LEVEL_CONFIG[level]["decay_hours"], \
                   EVENT_LEVEL_CONFIG[level]["impact_base"]
    # 默认：行业新闻
    level = "industry_news"
    return level, EVENT_LEVEL_CONFIG[level]["decay_hours"], \
           EVENT_LEVEL_CONFIG[level]["impact_base"]


def _classify_rule_based(title: str) -> Optional[dict]:
    """规则引擎快速分类。返回 None 表示无法判断。"""
    sentiment = None
    for kw in POSITIVE_KW:
        if kw in title:
            sentiment = "positive"
            impact = 0.5 if kw in ("利好", "涨停", "突破", "超预期", "政策支持") else 0.3
            break
    for kw in NEGATIVE_KW:
        if kw in title:
            sentiment = "negative"
            impact = 0.5 if kw in ("利空", "减持", "调查", "爆雷") else 0.3
            break

    if sentiment is None:
        return None

    # 突发判定
    is_urgent = any(kw in title for kw in ("突发", "重磅", "紧急"))

    # v4.0: 事件级别判定
    event_level, decay_hours, impact_base = _classify_event_level(title)

    # 板块映射
    sectors = []
    for sec_kw, sec_names in SECTOR_KW_MAP.items():
        if sec_kw in title:
            sectors.extend(sec_names)

    return {
        "sentiment": sentiment,
        "impact": 0.8 if is_urgent else max(impact, impact_base),
        "sectors": list(set(sectors))[:3],
        "is_urgent": is_urgent,
        "event_level": event_level,
        "decay_hours": decay_hours,
    }


# ── LLM 分析（DeepSeek）───────────────────────────────────────

def _llm_analyze_news(titles: list[str]) -> list[dict]:
    """用 DeepSeek-V3 批量分析新闻标题。

    Args:
        titles: 最近收集的新闻标题列表

    Returns:
        [{"title": str, "sentiment": "positive"|"negative"|"neutral",
          "impact": 0~1, "sectors": [...], "summary": str}, ...]
    """
    try:
        from openai import OpenAI
    except ImportError:
        logger.info("openai 未安装，跳过 LLM 分析")
        return []

    api_key = _get_deepseek_key()
    if not api_key:
        return []

    client = OpenAI(api_key=api_key, base_url="https://api.deepseek.com")

    prompt = f"""分析以下 A 股相关新闻标题。对每条返回 JSON：

- sentiment: "positive"(利好) / "negative"(利空) / "neutral"(中性)
- impact: 影响力 0~1，0.1=一般 0.5=重要 1.0=重大
- sectors: 影响的板块名称列表（从给出的标题中推断）
- summary: 一句话摘要（10字以内）

标题列表：
{chr(10).join(f"{i+1}. {t}" for i, t in enumerate(titles))}

返回格式：
[{{"index": 1, "sentiment": "positive", "impact": 0.5, "sectors": ["半导体"], "summary": "..."}}, ...]"""

    try:
        resp = client.chat.completions.create(
            model="deepseek-chat",
            messages=[
                {"role": "system", "content": "你是A股新闻分析助手。只返回JSON数组，不要其他内容。"},
                {"role": "user", "content": prompt},
            ],
            response_format={"type": "json_object"},
            temperature=0.1,
            max_tokens=2000,
        )
        raw = resp.choices[0].message.content
        data = json.loads(raw)
        if isinstance(data, dict) and "results" in data:
            data = data["results"]
        if not isinstance(data, list):
            return []
        # 回填标题
        for item in data:
            idx = item.get("index", 0) - 1
            if 0 <= idx < len(titles):
                item["title"] = titles[idx]
        return data
    except Exception as e:
        logger.warning("LLM 分析失败: %s", e)
        return []


def _llm_analyze_urgent(title: str) -> Optional[dict]:
    """用 DeepSeek-R1 深度分析突发新闻。"""
    try:
        from openai import OpenAI
    except ImportError:
        return None

    api_key = _get_deepseek_key()
    if not api_key:
        return None

    client = OpenAI(api_key=api_key, base_url="https://api.deepseek.com")

    try:
        resp = client.chat.completions.create(
            model="deepseek-reasoner",
            messages=[
                {"role": "system", "content": "你是A股短线分析专家。分析突发新闻对A股相关板块的短线影响（2-3天）。返回JSON。"},
                {"role": "user", "content": f"突发新闻：{title}\n\n分析：sentiment(positive/negative/neutral), impact(0-1), sectors(影响的板块), reasoning(推理过程50字以内)"},
            ],
            response_format={"type": "json_object"},
            temperature=0.1,
            max_tokens=1000,
        )
        raw = resp.choices[0].message.content
        return json.loads(raw)
    except Exception as e:
        logger.warning("LLM 突发分析失败: %s", e)
        return None


def _get_deepseek_key() -> Optional[str]:
    """从环境变量或配置文件读取 DeepSeek API Key。"""
    import os
    key = os.environ.get("DEEPSEEK_API_KEY")
    if key:
        return key
    # 尝试从配置文件读
    config_file = DATA_DIR / "deepseek_key.txt"
    if config_file.exists():
        return config_file.read_text().strip()
    return None


# ── 新闻爬取 ──────────────────────────────────────────────────

def _fetch_cls_headlines() -> list[str]:
    """爬取财联社电报标题。"""
    try:
        url = "https://www.cls.cn/api/sw?app=CailianpressWeb&os=web&sv=8.4.6"
        data = {"type": "telegram", "page": 1, "rn": 20}
        resp = _http.post(url, json=data, timeout=8, verify=False, headers=HEADERS)
        items = resp.json().get("data", {}).get("roll_data", [])
        titles = []
        for item in items:
            title = (item.get("title") or "").strip()
            brief = (item.get("brief") or "").strip()
            text = title or brief
            if text and len(text) > 3:
                titles.append(text)
        return titles[:15]
    except Exception as e:
        logger.debug("新闻爬取失败: %s", e)
        return []


# ── 监控主线程 ────────────────────────────────────────────────

class NewsMonitor:
    """消息面监控器 — 独立线程运行。"""

    def __init__(self):
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._titles_buffer: list[str] = []
        self._last_llm_time = 0.0
        self._events: list[dict] = []

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True, name="news")
        self._thread.start()
        logger.info("NewsMonitor 启动")

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=2)

    def _scan_headlines(self):
        """抓取标题 → 规则引擎分类 → 暂存。"""
        titles = _fetch_cls_headlines()
        if not titles:
            return

        new_events = []
        for title in titles:
            # 去重
            if title in self._titles_buffer:
                continue
            self._titles_buffer.append(title)

            # 规则引擎
            result = _classify_rule_based(title)
            if result:
                # v4.0: 按级别计算过期时间
                decay_h = result.get("decay_hours", 2)
                expires_dt = datetime.now() + timedelta(hours=decay_h)
                new_events.append({
                    "id": f"evt_{hash(title) & 0xFFFFFF:06x}",
                    "time": datetime.now().strftime("%H:%M"),
                    "title": title,
                    "sentiment": result["sentiment"],
                    "impact": result["impact"],
                    "sectors": result["sectors"],
                    "stocks": [],
                    "source": "rule_engine",
                    "event_level": result.get("event_level", "industry_news"),
                    "decay_hours": result.get("decay_hours", 2),
                    "expires_at": expires_dt.strftime("%H:%M"),
                })

                # 突发 → LLM 深度分析
                if result["is_urgent"]:
                    llm_result = _llm_analyze_urgent(title)
                    if llm_result:
                        new_events[-1].update({
                            "sentiment": llm_result.get("sentiment", result["sentiment"]),
                            "impact": llm_result.get("impact", result["impact"]),
                            "sectors": llm_result.get("sectors", result["sectors"]),
                            "source": "deepseek_r1",
                        })

        if new_events:
            # 合并已有事件，去重，保留最近 2 小时的
            cutoff = datetime.now().replace(hour=datetime.now().hour - 2)
            self._events = [
                e for e in self._events + new_events
                if _parse_event_time(e["time"]) >= cutoff
            ]
            # 按时间和影响力排序
            self._events.sort(key=lambda e: (e["impact"], e["time"]), reverse=True)
            self._events = self._events[:20]  # 保留前 20 条

            self._publish()

    def _llm_batch_analyze(self):
        """批量 LLM 分析积累的标题。"""
        if not self._titles_buffer:
            return

        llm_results = _llm_analyze_news(self._titles_buffer[-20:])
        if not llm_results:
            return

        for item in llm_results:
            self._events.append({
                "id": f"llm_{hash(item.get('title','')) & 0xFFFFFF:06x}",
                "time": datetime.now().strftime("%H:%M"),
                "title": item.get("title", ""),
                "sentiment": item.get("sentiment", "neutral"),
                "impact": item.get("impact", 0.3),
                "sectors": item.get("sectors", []),
                "stocks": [],
                "source": "deepseek_v3",
                "expires_at": (datetime.now().replace(hour=datetime.now().hour + 2)).strftime("%H:%M"),
            })

        self._titles_buffer = self._titles_buffer[-50:]  # 只保留最近 50 条
        self._publish()

    def _publish(self):
        """写入 SignalStore。"""
        from signals import get_signal_store
        get_signal_store().update("news", {"events": self._events})

    def _is_market_time(self) -> bool:
        """检查当前是否在 A 股交易时段（含盘前 15 分钟）。"""
        now = datetime.now()
        if now.weekday() >= 5:
            return False  # 周末
        t = now.hour * 60 + now.minute
        return 555 <= t <= 915  # 9:15-15:15（含盘前盘后缓冲）

    def _run(self):
        while self._running:
            # 非交易时段不轮询，等一段时间再检查
            if not self._is_market_time():
                time.sleep(60)
                continue

            try:
                self._scan_headlines()

                # LLM 批量分析（每 30 分钟）
                now = time.time()
                if now - self._last_llm_time >= LLM_BATCH_INTERVAL:
                    self._llm_batch_analyze()
                    self._last_llm_time = now

            except Exception as e:
                logger.warning("NewsMonitor 异常: %s", e)

            for _ in range(NEWS_CHECK_INTERVAL):
                if not self._running:
                    break
                time.sleep(1)


def _parse_event_time(time_str: str) -> datetime:
    try:
        parts = time_str.split(":")
        now = datetime.now()
        return now.replace(hour=int(parts[0]), minute=int(parts[1]))
    except (ValueError, IndexError):
        return datetime.now()


# ── CLI ────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")
    monitor = NewsMonitor()
    # 单次执行测试
    monitor._scan_headlines()
    print(json.dumps(monitor._events, ensure_ascii=False, indent=2)[:2000])
