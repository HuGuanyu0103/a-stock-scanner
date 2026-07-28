#!/usr/bin/env python3
"""
Multi-Agent 辩论系统

六个 Agent 角色（5 分析师 + 1 主席），基于现有三系统架构 + 决策飞轮 + 风控：
  TechAnalyst      观象 — 只看技术+资金面信号
  SentimentAnalyst 观势 — 只看情绪面信号
  NewsAnalyst      观闻 — 只看消息面信号
  HistoryAnalyst   观史 — 只看决策飞轮历史战绩（信号级真实胜率）
  RiskOfficer      观危 — 独立风控审视（板块集中度/大盘环境/纪律红线）
  Moderator        观澜 — 按可审计权重聚合五方意见，输出共识/分歧报告

辩论流程（真辩论，三阶段）：
  1. 五个分析师并发独立发表初始意见（只看自己领域的数据，互不可见）
  2. 反驳轮（真多轮）：每个分析师看到他人观点 + 上一轮别人对自己的反驳后，
     进行反驳/补充/让步/坚持；立场稳定（无人再反驳）即提前收敛
  3. Moderator 基于「初始意见 + 多轮辩论 + 历史胜率 + 数值权重」收敛出共识/分歧报告
     （rounds=0 可退化为旧的「并行发言→聚合」，向后兼容）

用法:
  from debate import DebateOrchestrator
  do = DebateOrchestrator()
  report = do.run_debate(candidates, signals, hot_sectors, breadth,
                         rounds=1, loop_context="<飞轮历史胜率>")
"""

from __future__ import annotations

import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════
# 分析师 System Prompts
# ═══════════════════════════════════════════════════════════════

TECH_SYSTEM_PROMPT = """你是 A 股技术+资金面分析师「观象」。你只看 K 线形态、均线系统、资金流向。

## 你的性格
你天性乐观进取——在信号不矛盾的情况下，你倾向于优先寻找交易机会而非风险。你的信条是「强势股自有其逻辑」。「底部放量突破」是你的最爱。

## 你的领域
- 技术形态信号（MACD金叉、均线多头排列、放量突破、平台突破等）
- 资金面信号（主力净流入/流出、量比、换手率、北向资金）
- 量价关系（放量上涨 vs 缩量上涨，哪个更可信）

## 你不看
- 市场情绪（交给情绪分析师）
- 新闻事件（交给消息分析师）

## 你的任务
根据提供的候选池数据，发表你的技术+资金面意见：
1. 候选池中哪些股票技术面最强？为什么？
2. 资金面是否印证技术面？（背离 = 风险信号）
3. 你最推荐的 2 只：技术面 + 资金面共振的

## 输出格式
严格 JSON：
{
  "analyst": "观象",
  "domain": "技术+资金面",
  "overall_view": "一句话总判断",
  "strength_picks": [
    {"code": "股票代码", "name": "名称", "reason": "技术面理由"},
    ...
  ],
  "divergence_alerts": [
    {"code": "股票代码", "name": "名称", "issue": "技术面与资金面的矛盾"},
    ...
  ],
  "bottom_line": "最终结论：哪些可以追，哪些要回避"
}
"""

SENTIMENT_SYSTEM_PROMPT = """你是 A 股情绪面分析师「观势」。你只看市场情绪、赚钱效应、资金轮动。

## 你的性格
你天性谨慎保守——你的首要职责不是寻找机会，而是识别风险。你觉得每一次追涨都可能是接盘。你的信条是「宁可错过，不可做错」。「市场情绪过热」是你最警惕的信号。

## 你的领域
- 涨停家数、连板高度、炸板率
- 涨跌比（上涨家数 / 下跌家数）
- 板块轮动速度（资金在不同板块间切换的频率）
- 情绪指数（综合评分 0-100）

## 你不看
- K 线技术形态（交给技术分析师）
- 新闻标题（交给消息分析师）

## 你的任务
根据提供的情绪数据，发表你的市场情绪判断：
1. 当前市场是「贪婪」「正常」「恐惧」哪个区间？
2. 赚钱效应如何？（涨停溢价、连板成功率）
3. 是否适合追涨？还是应该防守？

## 输出格式
严格 JSON：
{
  "analyst": "观势",
  "domain": "情绪面",
  "mood": "贪婪 | 正常 | 恐惧",
  "mood_score": 65,
  "breadth_assessment": "市场广度判断",
  "rotation_risk": "轮动过快/正常/慢速",
  "advice": "进攻 | 防守 | 观望",
  "bottom_line": "基于情绪面的最终建议"
}
"""

NEWS_SYSTEM_PROMPT = """你是 A 股消息面分析师「观闻」。你只看新闻事件对板块和个股的影响。

## 你的性格
你冷静理性——你不带偏见地评估每一条消息的实质影响。你不对市场方向做预测，只客观判断消息的利好/利空程度和时效性。你的信条是「事实是什么，就说什么」。

## 你的领域
- 政策新闻（国务院、证监会、发改委等官方发布）
- 行业新闻（产业政策、技术突破、供需变化）
- 个股公告（业绩预告、减持、回购、重组）
- 事件的影响力和时效性（今天的热点明天可能就凉了）

## 你不看
- K 线数据（交给技术分析师）
- 涨跌家数（交给情绪分析师）

## 你的任务
根据提供的新闻事件数据，发表你的消息面判断：
1. 今天有没有重大催化剂？（有 = 哪些板块受益）
2. 有没有利空需要回避？
3. 消息面的整体评级

## 输出格式
严格 JSON：
{
  "analyst": "观闻",
  "domain": "消息面",
  "overall_rating": "利好 | 中性 | 利空",
  "key_catalysts": [
    {"topic": "事件描述", "impact": "高|中|低", "sectors": ["板块1"]},
    ...
  ],
  "risk_events": ["需要警惕的消息"],
  "bottom_line": "消息面对今日操作的影响"
}
"""

HISTORY_SYSTEM_PROMPT = """你是 A 股决策委员会的复盘分析师「观史」。你只看历史战绩数据，从"过去哪些信号真赚钱"的角度提供经验校验。

## 你的性格
你是团队的记忆与纪律守护者——不被当下的热闹迷惑，只信数据沉淀。你的信条是「历史不会简单重复，但会押韵」。你专治其他分析师的「这次不一样」幻觉。

## 你的领域
- 各信号组合的历史胜率与平均收益（来自决策飞轮的真实结算数据）
- 识别当前候选票所属信号在历史上的表现档位（高胜率/低胜率/样本不足）
- 提醒团队：历史上表现差的信号，即便当下形态好看也要打折

## 你不看
- 实时技术/情绪/消息（交给对应分析师），你只做历史校验

## 你的任务
根据提供的历史信号胜率数据，对当前讨论做经验校验：
1. 当前候选池里的信号，历史胜率如何？哪些是历史验证过的强信号？
2. 有没有"当下热门但历史胜率低"的信号需要警示？
3. 样本不足的信号要标注（历史不可用，需谨慎）

## 输出格式
严格 JSON：
{
  "analyst": "观史",
  "domain": "历史复盘",
  "verified_signals": [{"signal": "信号名", "win_rate": "历史胜率", "note": "可信/存疑/样本不足"}],
  "warnings": ["历史胜率低但当下热门的信号提醒"],
  "bottom_line": "基于历史战绩，本次决策应加权哪些信号、警惕哪些"
}
"""

RISK_SYSTEM_PROMPT = """你是 A 股决策委员会的风控官「观危」。你不参与"买什么"的讨论，只做独立的风险审视。

## 你的性格
你是团队的刹车片——别人越兴奋你越冷静。你的职责不是找机会，而是确保不出致命错误。你的信条是「活下来比赚得多更重要」。

## 你的领域
- 仓位与集中度风险（候选是否过度集中在单一板块/主题）
- 回撤风险（个股/板块的止损纪律，最大单笔潜在亏损）
- 系统性风险（大盘环境是否支持进攻，普跌/极端行情下该防守）
- 交易纪律（止盈止损点位是否清晰、是否有情绪化追高嫌疑）

## 你不看
- 具体选哪只票的技术/情绪细节（交给对应分析师）

## 你的任务
对当前候选池和大盘环境做独立风控审视：
1. 板块集中度是否过高？（同一板块占比过大 = 系统性风险）
2. 当前大盘环境（市场广度）是否支持进攻仓位？
3. 有没有明显的追高/情绪化风险信号？
4. 给出仓位红线建议（满仓/半仓/轻仓/空仓观望）

## 输出格式
严格 JSON：
{
  "analyst": "观危",
  "domain": "风险控制",
  "concentration_risk": "板块集中度评估",
  "market_risk": "大盘环境下的仓位风险",
  "position_ceiling": "满仓 | 半仓 | 轻仓 | 空仓观望",
  "risk_alerts": ["具体风险提示"],
  "bottom_line": "风控角度的最终建议：仓位红线与必须回避的风险"
}
"""

MODERATOR_SYSTEM_PROMPT = """你是 A 股决策委员会的主席「观澜」。你有五位分析师，他们已各自发表意见并经过多轮辩论：
- 观象（技术+资金面）、观势（情绪面）、观闻（消息面）、观史（历史战绩复盘）、观危（风控官）。

## 你的任务
1. 阅读五位分析师的意见，以及他们多轮辩论中的反驳、让步与修正后观点
2. 判断一致程度：
   - 「强烈共识」：多数分析师指向同一方向且风控无红线否决
   - 「部分共识」：主要方向一致，个别分析师保留意见
   - 「分歧」：各执一词，或风控与进攻派尖锐对立
3. 综合判断后，给出最终决策建议

## 重要原则
- 你不是简单投票，而是**按可审计的角色权重**加权。用户会给你一份客观计算的角色话语权（基于市场广度），你必须按该权重加权，并在 key_reasoning 里说明为何本次某角色权重更高。
  - 广度高（普涨）时技术面权重更高；广度低（分化/退潮）时风控与情绪面权重更高。
- **观史的历史胜率是硬证据**：若某信号/板块历史胜率低，即使当下技术面漂亮也要降低置信度；反之历史验证过的信号可加分。
- **观危的风控红线不可逾越**：final_decision 的 position_advice 不得超过观危给出的 position_ceiling（仓位上限）。若观危要求回避某标的，不得放进 agreed_picks。
- 如果有分歧，明确指出「矛盾在哪里」和「条件建议」。
  - 例如：「技术面看多但情绪面偏冷、历史胜率一般，建议等情绪回暖再跟」
- 如果多数看空或风控否决，诚实地说「今天不适合操作」

## 输出格式
严格 JSON：
{
  "analyst": "观澜",
  "role": "决策委员会主席",
  "consensus_level": "强烈共识 | 部分共识 | 分歧",
  "consensus_detail": "为什么是这个共识级别",
  "final_decision": {
    "posture": "进攻 | 防守 | 观望",
    "confidence": 5,
    "key_reasoning": "按角色权重综合五方意见+多轮辩论+历史胜率后的核心判断，需说明权重如何影响结论",
    "agreed_picks": ["多方认可且未被风控否决的股票"],
    "conditional_picks": [{"code": "代码", "name": "名称", "condition": "满足什么条件可以买"}],
    "avoid_list": ["应该回避的股票/板块"],
    "position_advice": "满仓 | 半仓 | 轻仓 | 观望"
  },
  "analyst_alignment": {
    "tech": "看多 | 中性 | 看空",
    "sentiment": "看多 | 中性 | 看空",
    "news": "看多 | 中性 | 看空",
    "history": "看多 | 中性 | 看空",
    "risk": "放行 | 警示 | 否决"
  },
  "bottom_line": "最终一句话建议"
}
"""


# ═══════════════════════════════════════════════════════════════
# 反驳轮 System Prompt（真辩论的核心：分析师之间互相看见、互相质疑）
# ═══════════════════════════════════════════════════════════════

REBUT_SYSTEM_PROMPT = """你是 A 股决策委员会中的分析师「{analyst}」（{domain}）。

这是一场**真辩论**：你已发表了初始意见，现在你看到了另外两位分析师的观点。
请基于你的专业立场（{domain}）对他们的观点做出回应——这是辩论的反驳环节。

## 你要做的
1. **反驳**：另外两位哪些判断你从专业角度不认同？为什么？（用你领域的证据）
2. **补充**：他们忽略了哪些你领域的关键信息？
3. **让步**：他们哪些观点有道理、让你修正了初始看法？（诚实的分析师会承认对方的合理之处）
4. **坚持**：你依然坚持的核心判断是什么？

## 重要
- 保持你的性格与专业视角，但不要为了反对而反对——辩论的目的是逼近真相，不是赢。
- 如果对方在你领域之外的判断你无法评价，就明说「这不在我的领域」。

## 输出格式
严格 JSON：
{{
  "analyst": "{analyst}",
  "rebuttals": [
    {{"target": "观象|观势|观闻|观史|观危", "point": "你反驳的对方观点", "argument": "你的专业依据"}}
  ],
  "supplements": ["你补充的关键信息"],
  "concessions": ["你被说服/修正的点，没有则空数组"],
  "revised_view": "辩论后你修正/强化后的核心判断（一句话）"
}}
"""


# ═══════════════════════════════════════════════════════════════
# 数据提取器 — 为每个分析师准备专属上下文
# ═══════════════════════════════════════════════════════════════

class DataExtractor:
    """从候选池、三系统信号、决策飞轮历史与风控维度提取各分析师的专属数据。"""

    @staticmethod
    def for_tech(candidates: list[dict], top_n: int = 8) -> str:
        """为技术分析师提取数据。"""
        # 按盘中评分排序，取 Top N
        ranked = sorted(candidates, key=lambda x: x.get("score", 0), reverse=True)[:top_n]

        rows = []
        for s in ranked:
            ds = s.get("daily_combined_score")
            daily_str = f"{ds:.1f}" if ds is not None else "?"
            rows.append({
                "代码": s.get("code", ""),
                "名称": s.get("name", ""),
                "板块": s.get("sector", ""),
                "池": s.get("pool", ""),
                "盘中评分": s.get("score", 0),
                "日评分": daily_str,
                "涨跌幅": f"{s.get('pct_chg', 0):+.1f}%",
                "信号": s.get("signal", "?"),
                "量比": s.get("volume_ratio", 0),
                "换手率": s.get("turnover_rate", 0),
                "主力净占比": f"{s.get('net_main_ratio', 0):+.1f}%",
                "日内位置": f"{s.get('intraday_position', 0):.2f}",
                "短期动量": s.get("short_momentum", 0),
            })

        return json.dumps({
            "title": "技术+资金面数据（按评分排序的 Top 股票）",
            "stocks": rows,
        }, ensure_ascii=False, indent=2)

    @staticmethod
    def for_sentiment(signals: dict, breadth: float) -> str:
        """为情绪分析师提取数据。"""
        sent_data = signals.get("sentiment", {}).get("data", {})
        market = sent_data.get("market", {}) if sent_data else {}

        return json.dumps({
            "title": "情绪面数据",
            "市场广度": f"{int(breadth * 100)}%板块上涨",
            "情绪指数": market.get("sentiment_index", "?"),
            "炸板率": f"{market.get('po_ban_rate', 0) * 100:.0f}%" if market.get("po_ban_rate") else "?",
            "轮动速度": market.get("rotation_speed", "?"),
            "连板率": market.get("lianban_rate", "?"),
        }, ensure_ascii=False, indent=2)

    @staticmethod
    def for_news(signals: dict) -> str:
        """为消息分析师提取数据。"""
        news_data = signals.get("news", {}).get("data", {})
        events = news_data.get("events", [])[:10] if news_data else []

        return json.dumps({
            "title": "消息面数据",
            "events": [{
                "标题": e.get("title", "")[:40],
                "情绪": e.get("sentiment", ""),
                "影响": e.get("impact", 0),
                "板块": e.get("sectors", []),
                "时间": e.get("time", ""),
            } for e in events],
            "总事件数": len(events),
        }, ensure_ascii=False, indent=2)

    @staticmethod
    def for_history(loop_context: str, candidates: list[dict]) -> str:
        """M2: 为观史分析师提取历史战绩数据（决策飞轮信号胜率）。"""
        cand_signals = sorted({c.get("signal", "") for c in candidates if c.get("signal")})
        return json.dumps({
            "title": "历史战绩数据（来自决策飞轮真实结算）",
            "历史信号胜率": loop_context or "（暂无历史结算数据，飞轮尚在积累）",
            "本次候选池涉及的信号": cand_signals,
        }, ensure_ascii=False, indent=2)

    @staticmethod
    def for_risk(candidates: list[dict], breadth: float, hot_sectors: list[str]) -> str:
        """M3: 为风控官提取风险审视数据（板块集中度/大盘环境）。"""
        from collections import Counter
        sectors = Counter(c.get("sector", "未知") for c in candidates[:25])
        top_conc = sectors.most_common(5)
        total = sum(sectors.values()) or 1
        return json.dumps({
            "title": "风控审视数据",
            "市场广度": f"{int(breadth * 100)}% 板块上涨",
            "大盘环境": ("普跌/防守" if breadth < 0.3 else ("分化" if breadth < 0.5 else "偏多/可进攻")),
            "候选池板块集中度": [{"板块": s, "占比": f"{n/total*100:.0f}%"} for s, n in top_conc],
            "热板块": hot_sectors[:8],
            "候选池规模": len(candidates),
        }, ensure_ascii=False, indent=2)


# ═══════════════════════════════════════════════════════════════
# 辩论编排器
# ═══════════════════════════════════════════════════════════════

class DebateOrchestrator:
    """Multi-Agent 辩论编排器。

    使用方式:
        do = DebateOrchestrator()
        report = do.run_debate(candidates, signals, hot_sectors, breadth)
        # report 包含五个分析师意见（观象/观势/观闻/观史/观危）+ Moderator 综合判断
    """

    def __init__(self, model: str = "deepseek-chat"):
        self._model = model
        self._client = None
        self._api_available = None

    def _init_client(self) -> bool:
        if self._api_available is not None:
            return self._api_available

        try:
            from openai import OpenAI
        except ImportError:
            self._api_available = False
            return False

        import os
        from pathlib import Path
        key = os.environ.get("DEEPSEEK_API_KEY")
        if not key:
            kf = Path(__file__).parent / "data" / "deepseek_key.txt"
            if kf.exists():
                key = kf.read_text().strip()
        if not key:
            self._api_available = False
            return False

        # P1-3: 显式 timeout，避免多次串行 LLM 调用时端点长时间挂起
        self._client = OpenAI(api_key=key, base_url="https://api.deepseek.com",
                              timeout=30.0)
        self._api_available = True
        return True

    def run_debate(
        self,
        candidates: list[dict],
        signals: dict,
        hot_sectors: list[str],
        breadth: float = 0.5,
        rounds: int = 1,
        loop_context: str = "",
    ) -> Optional[dict]:
        """运行完整辩论流程（5 角色并发 + 真多轮收敛 + 历史校验 + 可审计加权）。

        Args:
            rounds: 反驳轮数。0 = 并行发言→聚合；≥1 = 真多轮辩论：每轮分析师看到
                    「别人对自己的反驳」后再调整，循环至立场稳定或达 rounds 上限。
            loop_context: 决策飞轮历史信号胜率文本（M2），注入观史角色与主席。

        Returns:
            {analysts, rebuttals(每轮), moderator, consensus_level, rounds, weights, timestamp}
        """
        if not self._init_client():
            return None

        # ── 5 个分析师并发发表初始意见（M4 并发）──────────────
        # 角色: 观象(技术资金) 观势(情绪) 观闻(消息) 观史(历史战绩) 观危(风控)
        agent_specs = {
            "tech": (TECH_SYSTEM_PROMPT, DataExtractor.for_tech(candidates)),
            "sentiment": (SENTIMENT_SYSTEM_PROMPT, DataExtractor.for_sentiment(signals, breadth)),
            "news": (NEWS_SYSTEM_PROMPT, DataExtractor.for_news(signals)),
            "history": (HISTORY_SYSTEM_PROMPT, DataExtractor.for_history(loop_context, candidates)),
            "risk": (RISK_SYSTEM_PROMPT, DataExtractor.for_risk(candidates, breadth, hot_sectors)),
        }
        metas = {
            "tech": ("观象", "技术+资金面"), "sentiment": ("观势", "情绪面"),
            "news": ("观闻", "消息面"), "history": ("观史", "历史复盘"), "risk": ("观危", "风险控制"),
        }

        def _one_analyst(item):
            role, (prompt, data) = item
            return role, self._ask_analyst(role, prompt, data)

        with ThreadPoolExecutor(max_workers=5) as ex:
            opinions = dict(ex.map(_one_analyst, agent_specs.items()))

        if not any(opinions.values()):
            return None

        # ── 真多轮反驳收敛（M3）：每轮让 analyst 看到别人对自己的反驳再调整 ──
        all_rounds = []          # 每轮的 rebuttals dict
        cur_opinions = dict(opinions)
        prev_rebut = {}          # 上一轮别人的反驳（供本轮 analyst 看到"别人怎么说我"）
        for rd in range(max(rounds, 0)):
            def _one_rebut(role):
                name, domain = metas[role]
                if not cur_opinions.get(role):
                    return role, None
                others = {metas[r][0]: op for r, op in cur_opinions.items() if r != role and op}
                # M3 多轮关键：把"上一轮别人对我的反驳"也带进来，让本轮能回应
                against_me = None
                if prev_rebut:
                    against_me = [
                        {"from": metas[r][0], "point": rb}
                        for r, reb in prev_rebut.items() if r != role and reb
                        for rb in (reb.get("rebuttals") or []) if rb.get("target") == name
                    ]
                return role, self._rebut(name, domain, cur_opinions[role], others, against_me)

            with ThreadPoolExecutor(max_workers=5) as ex:
                rebut = dict(ex.map(_one_rebut, list(metas.keys())))
            rebut = {k: v for k, v in rebut.items() if v}
            all_rounds.append(rebut)
            # 收敛判定：本轮所有 analyst 都无实质反驳（rebuttals 为空）→ 立场稳定，提前结束
            active = sum(1 for v in rebut.values() if v.get("rebuttals"))
            prev_rebut = rebut
            if active == 0:
                break

        # ── 可审计加权（M3）：用市场广度做数值权重约束，喂给主席 ──
        weights = self._compute_role_weights(breadth, signals)

        # ── 主席收敛（含全部角色意见 + 多轮反驳 + 历史 + 数值权重）──
        moderator_opinion = self._moderate(
            cur_opinions, DataExtractor.for_tech(candidates, top_n=15),
            hot_sectors, all_rounds, weights, loop_context,
        )

        return {
            "analysts": {r: (op or {"error": "分析师不可用"}) for r, op in cur_opinions.items()},
            "rebuttals": all_rounds[0] if all_rounds else {},  # 兼容旧字段（首轮）
            "rebuttal_rounds": all_rounds,                     # 全部轮次
            "moderator": moderator_opinion or {"error": "主席不可用"},
            "consensus_level": (moderator_opinion or {}).get("consensus_level", "未知"),
            "rounds": len(all_rounds),
            "weights": weights,
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }

    @staticmethod
    def _compute_role_weights(breadth: float, signals: dict) -> dict:
        """M3 可审计加权：用市场广度等客观信号算出各角色的数值权重（可复现，非LLM自由发挥）。

        趋势市(广度高)→技术权重高；震荡/普跌(广度低)→情绪+风控权重高；
        历史与风控给固定底权重，保证经验与风险始终有话语权。
        """
        b = max(0.0, min(1.0, breadth))
        # 基础权重
        w = {"tech": 0.25, "sentiment": 0.20, "news": 0.15, "history": 0.20, "risk": 0.20}
        if b >= 0.5:            # 偏多/进攻市：技术面话语权上调
            w["tech"] += 0.10; w["risk"] -= 0.05; w["sentiment"] -= 0.05
        elif b < 0.3:          # 普跌/防守市：情绪+风控话语权上调
            w["risk"] += 0.10; w["sentiment"] += 0.05; w["tech"] -= 0.15
        # 归一化
        s = sum(w.values())
        return {k: round(v / s, 3) for k, v in w.items()}

    def _rebut(self, name: str, domain: str, own: dict, others: dict, against_me=None) -> Optional[dict]:
        """反驳轮：分析师看到其他人观点后做出专业回应。

        against_me（M3 多轮）：上一轮别人对"我"的反驳列表，让本轮能针对性回应/坚持/让步，
        实现真正的多轮逼近而非单向一次性喷。
        """
        own_str = json.dumps(own, ensure_ascii=False, indent=2)
        others_str = "\n\n".join(
            f"=== {n} 的观点 ===\n{json.dumps(op, ensure_ascii=False, indent=2)}"
            for n, op in others.items()
        )
        against_block = ""
        if against_me:
            against_block = (
                "\n\n=== 上一轮其他分析师对你的反驳（请针对性回应：坚持或让步）===\n"
                + json.dumps(against_me, ensure_ascii=False, indent=2)
            )
        try:
            resp = self._client.chat.completions.create(
                model=self._model,
                messages=[
                    {"role": "system", "content": REBUT_SYSTEM_PROMPT.format(analyst=name, domain=domain)},
                    {"role": "user", "content": (
                        f"你的当前意见：\n{own_str}\n\n"
                        f"其他分析师的观点：\n{others_str}"
                        f"{against_block}\n\n"
                        "请做出你的辩论回应，严格按 JSON 输出。"
                    )},
                ],
                response_format={"type": "json_object"},
                temperature=0.4,
                max_tokens=1500,
            )
            return self._parse_json(resp.choices[0].message.content)
        except Exception as e:
            logger.warning("反驳轮 %s 调用失败: %s", name, e)
            return None

    def _ask_analyst(self, role: str, system_prompt: str, data: str) -> Optional[dict]:
        """询问一位分析师。"""
        try:
            resp = self._client.chat.completions.create(
                model=self._model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": (
                        f"请根据以下数据发表你的专业意见。\n\n{data}\n\n"
                        "严格按照 JSON 格式输出，不要添加其他内容。"
                    )},
                ],
                response_format={"type": "json_object"},
                temperature=0.3,
                max_tokens=1500,
            )
            raw = resp.choices[0].message.content
            return self._parse_json(raw)
        except Exception as e:
            logger.warning("分析师 %s 调用失败: %s", role, e)
            return None

    def _moderate(
        self,
        opinions: dict,
        full_data: str,
        hot_sectors: list[str],
        all_rounds: list = None,
        weights: dict = None,
        loop_context: str = "",
    ) -> Optional[dict]:
        """Moderator 综合五角色意见 + 多轮反驳 + 历史 + 数值权重。"""
        name_map = {"tech": "观象·技术资金", "sentiment": "观势·情绪", "news": "观闻·消息",
                    "history": "观史·历史复盘", "risk": "观危·风控"}
        context = ""
        for role, label in name_map.items():
            op = opinions.get(role)
            context += f"=== {label} 意见 ===\n{json.dumps(op, ensure_ascii=False, indent=2) if op else '无数据'}\n\n"

        # 多轮反驳全部纳入主席视野（M3）
        if all_rounds:
            for i, rd in enumerate(all_rounds, 1):
                if rd:
                    context += (f"=== 第{i}轮辩论反驳 ===\n"
                                f"{json.dumps(rd, ensure_ascii=False, indent=2)}\n\n")
            context += ("注意：请重点参考多轮辩论中的反驳、让步与修正后观点——"
                        "经过多轮仍无人反驳的共识、被对方说服的让步，比初始意见更可信。\n\n")

        # 可审计数值权重（M3）：明确告诉主席各角色话语权，且要求解释为何采纳
        if weights:
            context += (f"=== 角色话语权（基于市场广度客观计算，非主观）===\n"
                        f"{json.dumps(weights, ensure_ascii=False)}\n"
                        "请按此权重加权各角色意见，并在 key_reasoning 里说明为何本次某角色权重更高。\n\n")

        # 历史战绩（M2）：让主席知道哪些信号历史上真赚钱
        if loop_context:
            context += f"=== 历史信号胜率（决策飞轮）===\n{loop_context}\n\n"

        context += (f"=== 完整候选池 ===\n{full_data}\n\n"
                    f"=== 当前热板块 ===\n{hot_sectors[:8]}")

        try:
            resp = self._client.chat.completions.create(
                model=self._model,
                messages=[
                    {"role": "system", "content": MODERATOR_SYSTEM_PROMPT},
                    {"role": "user", "content": (
                        "请综合五位分析师(技术/情绪/消息/历史/风控)的意见、多轮辩论反驳、"
                        "角色数值权重、历史胜率和候选池，给出最终决策建议。"
                        "风控官(观危)的仓位红线必须被尊重，不得超越其 position_ceiling。"
                        f"\n\n{context}\n\n严格按照 JSON 格式输出。"
                    )},
                ],
                response_format={"type": "json_object"},
                temperature=0.4,
                max_tokens=2500,
            )
            raw = resp.choices[0].message.content
            return self._parse_json(raw)
        except Exception as e:
            logger.warning("Moderator 调用失败: %s", e)
            return None

    @staticmethod
    def _parse_json(raw: str) -> Optional[dict]:
        try:
            m = re.search(r'\{.*\}', raw, re.DOTALL)
            return json.loads(m.group() if m else raw)
        except (json.JSONDecodeError, AttributeError):
            return None


# ── 全局单例 ──────────────────────────────────────────────────

_orchestrator: Optional[DebateOrchestrator] = None


def get_orchestrator() -> DebateOrchestrator:
    global _orchestrator
    if _orchestrator is None:
        _orchestrator = DebateOrchestrator()
    return _orchestrator
