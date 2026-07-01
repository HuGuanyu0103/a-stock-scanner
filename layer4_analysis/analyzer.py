"""
分析层 — 个股综合分析 + 板块热度 + 市场简报

整合技术信号、资金流向、概念板块、市场情绪，
生成可读的个股分析和市场简报。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Optional

import pandas as pd

from layer1_data import DataFetcher
from layer2_scan.patterns import TechnicalPatterns
from layer2_scan.sentiment_signals import SentimentSignals
from layer2_scan.alpha_factors import AlphaFactors

logger = logging.getLogger(__name__)


class StockAnalyzer:
    """个股/市场分析器"""

    def __init__(self, fetcher: Optional[DataFetcher] = None):
        self.fetcher = fetcher or DataFetcher()

    def analyze_stock(self, stock_code: str, lookback_days: int = 120) -> dict:
        """
        深度分析单只股票

        Returns
        -------
        dict with keys: code, price, signals, concepts, support, resistance, summary
        """
        end = datetime.now().strftime("%Y-%m-%d")
        start = (datetime.now() - timedelta(days=lookback_days)).strftime("%Y-%m-%d")

        kline = self.fetcher.kline(stock_code, start_date=start, end_date=end)
        if kline is None or kline.empty:
            return {"code": stock_code, "error": "无法获取 K 线数据"}

        tech_signals = TechnicalPatterns.scan(kline)
        tech_score = TechnicalPatterns.total_score(tech_signals)

        sent_signals = SentimentSignals.scan(kline)
        sent_score = SentimentSignals.total_score(sent_signals)

        all_signals = tech_signals + sent_signals
        score = tech_score + sent_score
        signal_names = TechnicalPatterns.signal_names(tech_signals) +                        SentimentSignals.signal_names(sent_signals)
        max_level = TechnicalPatterns.max_signal_level(tech_signals)

        # 因子评分
        factor_score = 0.0
        factor_details = {}
        try:
            af = AlphaFactors(kline)
            factor_details = af.compute_all()
            factor_score = af.composite_score()
        except Exception:
            pass

        combined_score = round(tech_score * 0.5 + sent_score * 0.2 + factor_score * 0.3, 1)

        current = kline.iloc[-1]
        close = current.get("close", 0)
        pct = current.get("change_pct", 0)

        close_s = kline["close"]
        ma5 = close_s.rolling(5).mean().iloc[-1] if len(kline) >= 5 else 0
        ma10 = close_s.rolling(10).mean().iloc[-1] if len(kline) >= 10 else 0
        ma20 = close_s.rolling(20).mean().iloc[-1] if len(kline) >= 20 else 0

        recent_low = kline["low"].tail(20).min()
        recent_high = kline["high"].tail(20).max()
        concepts = self.fetcher.stock_concepts(stock_code)

        conclusion_parts = []
        if all_signals:
            conclusion_parts.append(f"识别到 {len(all_signals)} 个信号，技术{tech_score}+情绪{sent_score}，因子{factor_score:.1f}")
        if concepts:
            conclusion_parts.append(f"所属概念: {', '.join(concepts[:5])}")
        if ma5 > ma10 > ma20 > 0:
            conclusion_parts.append("均线多头排列，趋势向上")
        elif ma5 < ma10 < ma20 and all(m > 0 for m in (ma5, ma10, ma20)):
            conclusion_parts.append("均线空头排列，趋势偏弱")

        return {
            "code": stock_code,
            "price": close,
            "change_pct": pct,
            "ma5": ma5, "ma10": ma10, "ma20": ma20,
            "support": recent_low,
            "resistance": recent_high,
            "concepts": concepts[:5] if concepts else [],
            "signal_count": len(all_signals),
            "signal_score": score,
            "tech_score": tech_score,
            "sentiment_score": sent_score,
            "factor_score": round(factor_score, 1),
            "combined_score": combined_score,
            "max_signal_level": max_level,
            "signal_names": signal_names,
            "signals": all_signals,
            "factor_details": factor_details,
            "summary": "; ".join(conclusion_parts) if conclusion_parts else "无明显信号",
        }

    def market_brief(self) -> dict:
        """
        市场简报 — 使用全市场行情分批获取

        Returns
        -------
        dict with keys: trade_date, up_count, down_count, ...
        """
        all_stocks = self.fetcher.all_stocks()
        if all_stocks is None or all_stocks.empty:
            return {"error": "无法获取股票列表"}

        all_codes = all_stocks["stock_code"].tolist()
        fragments = []
        batch_size = 100

        for i in range(0, len(all_codes), batch_size):
            chunk = all_codes[i:i + batch_size]
            try:
                df = self.fetcher.current_market(code_list=chunk)
                if df is not None and not df.empty:
                    fragments.append(df)
            except Exception:
                continue
            if len(fragments) >= 5:  # 取 500 只足够估算市场情况
                break

        if not fragments:
            return {"error": "无法获取行情数据"}

        market = pd.concat(fragments, ignore_index=True)
        pct_col = "change_pct"
        # 转换 change_pct 为数值类型
        market[pct_col] = pd.to_numeric(market[pct_col], errors="coerce")
        up = int((market[pct_col] > 0).sum())
        down = int((market[pct_col] < 0).sum())
        flat = int((market[pct_col] == 0).sum())

        # 估算涨停（非ST，涨幅>=9.8%）
        st_filter = market["short_name"].str.contains("ST", na=False) if "short_name" in market.columns else pd.Series([False]*len(market))
        non_st = market[~st_filter]
        limit_up = int((non_st[pct_col] >= 9.8).sum()) if len(non_st) > 0 else 0

        return {
            "trade_date": datetime.now().strftime("%Y-%m-%d"),
            "up_count": up,
            "down_count": down,
            "flat_count": flat,
            "limit_up_count": limit_up,
            "total_count": len(market),
        }

    def generate_report(self, screener_result: pd.DataFrame, output_path: str = "") -> str:
        """生成选股报告（Markdown）"""
        if screener_result is None or screener_result.empty:
            return "## 选股报告\n\n今日无符合条件的股票。"

        date_str = datetime.now().strftime("%Y-%m-%d %H:%M")
        # 尝试获取市场情绪数据
        sentiment_context = ""
        try:
            from layer1_data import SentimentData
            sd = SentimentData()
            ss = sd.sentiment_score()
            sentiment_context = f"\n\n## 市场情绪\n\n综合评分: **{ss['score']}** | 状态: {ss['label']} | " \
                f"涨停{ss['details']['total_limit_up']}家 | 最高{ss['details']['highest_board']}板 | " \
                f"上涨比{ss['details']['advance_ratio']}%\n"
            # 连板梯队
            bt = sd.board_tiers()
            if bt.get("tier_distribution"):
                tiers = " | ".join(
                    f"{k.replace('tier_','').replace('high','高位')}板:{v}只"
                    for k, v in sorted(bt['tier_distribution'].items())
                )
                sentiment_context += f"连板梯队: {tiers}\n"
        except Exception:
            pass

        lines = [
            f"# 选股报告 — {date_str}",
            "",
            f"共扫描到 **{len(screener_result)}** 只符合条件的股票",
            "",
            sentiment_context,
            "",
            "| 序号 | 代码 | 名称 | 价格 | 涨幅% | 综合 | 形态 | 情绪 | 因子 | 信号说明 |",
            "|------|------|------|------|-------|------|------|------|------|----------|",
        ]

        for i, (_, row) in enumerate(screener_result.iterrows(), 1):
            code = row.get("stock_code", "")
            name = str(row.get("short_name", ""))[:6]
            price = row.get("price", 0)
            pct = row.get("change_pct", 0)
            combined = row.get("combined_score", row.get("signal_score", 0))
            ts = row.get("signal_score", 0)
            ss = row.get("sentiment_score", "0")
            fs = row.get("factor_score", "0")
            names = str(row.get("signal_names", ""))[:40]
            concepts = row.get("concepts", "")

            notes = names
            if concepts:
                notes += f" [{concepts[:20]}]"

            lines.append(
                f"| {i} | {code} | {name} | {price:.2f} | {pct:+.1f} | "
                f"{combined:.1f} | {ts} | {ss} | {fs} | {notes} |"
            )

        report = "\n".join(lines)
        if output_path:
            with open(output_path, "w", encoding="utf-8") as f:
                f.write(report)
            logger.info("报告已保存至 %s", output_path)
        return report
