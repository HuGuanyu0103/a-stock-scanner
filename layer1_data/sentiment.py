"""
情绪数据层 — 涨停池、连板统计、情绪指标、龙虎榜

对标 akshare 的涨停股池/龙虎榜/情绪接口，直接用 adata + requests 实现。
所有函数返回 pandas DataFrame，失败时返回空 DataFrame。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

import pandas as pd

logger = logging.getLogger(__name__)


class SentimentData:
    """市场情绪与短线情绪数据"""

    def __init__(self, fetcher=None):
        from layer1_data import DataFetcher
        self.fetcher = fetcher or DataFetcher()

    # ── 涨停池 ─────────────────────────────────────────────────

    def limit_up_pool(self, date_str: str = "") -> pd.DataFrame:
        """
        识别当日或指定日涨停股票池。
        从全市场行情中筛选涨幅 >= 9.8% 且非 ST 的股票。
        """
        date_str = date_str or datetime.now().strftime("%Y-%m-%d")
        try:
            all_stocks = self.fetcher.all_stocks()
            if all_stocks is None or all_stocks.empty:
                return pd.DataFrame()
            codes = all_stocks["stock_code"].tolist()

            fragments = []
            for i in range(0, len(codes), 100):
                chunk = codes[i:i + 100]
                df = self.fetcher.current_market(code_list=chunk)
                if df is not None and not df.empty:
                    fragments.append(df)

            if not fragments:
                return pd.DataFrame()

            market = pd.concat(fragments, ignore_index=True)
            pct_col = "change_pct" if "change_pct" in market.columns else "pct_chg"
            market[pct_col] = pd.to_numeric(market[pct_col], errors="coerce")

            if "short_name" in market.columns:
                market = market[~market["short_name"].str.contains("ST|退", na=False)]
            limit_ups = market[market[pct_col] >= 9.8].copy()

            if limit_ups.empty:
                return pd.DataFrame()

            limit_ups["consecutive_boards"] = 0
            limit_ups["limit_pct"] = limit_ups[pct_col]

            codes_to_check = limit_ups["stock_code"].tolist()
            kline_map = self.fetcher.batch_kline(codes_to_check, days=30, k_type=1)
            consecutive = {}
            for code in codes_to_check:
                kl = kline_map.get(code)
                if kl is None or kl.empty:
                    continue
                consecutive[code] = self._count_consecutive_boards(kl, date_str)

            limit_ups["consecutive_boards"] = limit_ups["stock_code"].map(consecutive).fillna(0).astype(int)

            cols = ["stock_code", "short_name", "price", "change_pct",
                    "consecutive_boards", "volume", "amount"]
            avail = [c for c in cols if c in limit_ups.columns]
            limit_ups = limit_ups[avail].sort_values("consecutive_boards", ascending=False)
            limit_ups["trade_date"] = date_str
            return limit_ups.reset_index(drop=True)

        except Exception as e:
            logger.warning("涨停池识别失败: %s", e)
            return pd.DataFrame()

    def _count_consecutive_boards(self, kline: pd.DataFrame, date_str: str) -> int:
        """统计截至 date_str 的连续涨停天数"""
        pct_col = "change_pct" if "change_pct" in kline.columns else "pct_chg"
        kl = kline[kline["trade_date"] <= date_str].sort_values("trade_date", ascending=False)
        count = 0
        for _, row in kl.iterrows():
            if row.get(pct_col, 0) >= 9.8:
                count += 1
            else:
                break
        return count

    # ── 连板高度与梯队 ─────────────────────────────────────────

    def board_tiers(self, date_str: str = "") -> dict:
        """
        连板梯队统计
        Returns dict: highest_board, total_limit_up, tier_distribution
        """
        pool = self.limit_up_pool(date_str)
        if pool.empty:
            return {"highest_board": 0, "total_limit_up": 0, "tier_distribution": {}}

        dist = pool["consecutive_boards"].value_counts().sort_index().to_dict()
        tiers = {}
        for board_count in sorted(dist.keys()):
            if board_count <= 4:
                tiers[f"tier_{board_count}"] = dist[board_count]
            else:
                tiers["tier_high"] = tiers.get("tier_high", 0) + dist[board_count]

        return {
            "highest_board": int(pool["consecutive_boards"].max()),
            "total_limit_up": len(pool),
            "tier_distribution": tiers,
            "date": date_str or datetime.now().strftime("%Y-%m-%d"),
        }

    # ── 昨日涨停今日表现 ────────────────────────────────────────

    def yesterday_limitup_performance(self) -> dict:
        """昨日涨停股票今日的表现"""
        today = datetime.now().strftime("%Y-%m-%d")
        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")

        yest_pool = self.limit_up_pool(yesterday)
        if yest_pool.empty:
            return {"avg_change_pct": 0, "up_ratio": 0,
                    "continue_board_ratio": 0, "count": 0, "date": today}

        yest_codes = yest_pool["stock_code"].tolist()
        today_market = self.fetcher.current_market(code_list=yest_codes)
        if today_market is None or today_market.empty:
            return {"avg_change_pct": 0, "up_ratio": 0,
                    "continue_board_ratio": 0, "count": len(yest_codes), "date": today}

        pct_col = "change_pct" if "change_pct" in today_market.columns else "pct_chg"
        today_market[pct_col] = pd.to_numeric(today_market[pct_col], errors="coerce")

        merged = yest_pool.merge(today_market[["stock_code", pct_col]], on="stock_code", how="left")
        pct_today = f"{pct_col}_y" if f"{pct_col}_y" in merged.columns else pct_col

        avg_pct = merged[pct_today].mean()
        up_ratio = (merged[pct_today] > 0).sum() / len(merged) * 100 if len(merged) > 0 else 0
        continue_ratio = (merged[pct_today] >= 9.8).sum() / len(merged) * 100 if len(merged) > 0 else 0

        return {
            "avg_change_pct": round(float(avg_pct), 2),
            "up_ratio": round(float(up_ratio), 1),
            "continue_board_ratio": round(float(continue_ratio), 1),
            "count": len(merged),
            "date": today,
        }

    # ── 涨跌家数 ───────────────────────────────────────────────

    def advance_decline_stats(self) -> dict:
        """全市场涨跌家数统计"""
        try:
            from layer4_analysis import StockAnalyzer
            analyzer = StockAnalyzer(self.fetcher)
            brief = analyzer.market_brief()
            if "error" in brief:
                return {"advance": 0, "decline": 0, "total": 0, "advance_ratio": 0, "limit_up": 0}
            total = brief.get("up_count", 0) + brief.get("down_count", 0) + brief.get("flat_count", 0)
            adv_ratio = brief["up_count"] / total * 100 if total > 0 else 0
            return {
                "advance": brief["up_count"],
                "decline": brief["down_count"],
                "flat": brief["flat_count"],
                "total": total,
                "advance_ratio": round(adv_ratio, 1),
                "limit_up": brief["limit_up_count"],
                "date": datetime.now().strftime("%Y-%m-%d"),
            }
        except Exception as e:
            logger.warning("涨跌家数统计失败: %s", e)
            return {"error": str(e)}

    # ── 龙虎榜 ───────────────────────────────────────────

    def dragon_tiger_list(self, trade_date: str = "") -> pd.DataFrame:
        """
        龙虎榜数据（East Money API 接口）
        """
        trade_date = trade_date or datetime.now().strftime("%Y-%m-%d")
        try:
            import requests as req
            url = (
                "https://push2.eastmoney.com/api/qt/clist/get"
                "?pn=1&pz=100&po=1&np=1&ut=bd1d9ddb04089700cf9c27f6f7426281"
                "&fltt=2&invt=2&fid=f3&fs=m:0+t:6+f:!50,m:0+t:80+f:!50"
                "&fields=f12,f14,f3,f62,f184,f66,f69,f72,f75,f78,f81,f84,f87,f204,f205,f124"
            )
            r = req.get(url, timeout=10, headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
                "Referer": "https://data.eastmoney.com/",
            })
            data = r.json()
            items = data.get("data", {}).get("diff", [])
            if not items:
                return pd.DataFrame()

            rows = []
            for item in items:
                rows.append({
                    "stock_code": str(item.get("f12", "")).zfill(6),
                    "short_name": item.get("f14", ""),
                    "change_pct": item.get("f3", 0),
                    "total_buy": item.get("f62", 0),
                    "total_sell": item.get("f184", 0),
                    "net_buy_amount": item.get("f204", 0) or item.get("f66", 0),
                    "reason": item.get("f124", ""),
                })

            df = pd.DataFrame(rows)
            df["trade_date"] = trade_date
            return df.sort_values("net_buy_amount", ascending=False).reset_index(drop=True)
        except Exception as e:
            logger.warning("龙虎榜获取失败: %s", e)
            return pd.DataFrame()

    # ── 综合情绪评分 ───────────────────────────────────────────

    def sentiment_score(self) -> dict:
        """
        综合市场情绪评分 (0-100)
        因子: 涨跌比(30%) 涨停家数(20%) 连板高度(20%) 昨日涨停表现(15%) 龙虎榜净买入率(15%)
        """
        adv_dec = self.advance_decline_stats()
        boards = self.board_tiers()
        yest_perf = self.yesterday_limitup_performance()

        adv_ratio = adv_dec.get("advance_ratio", 0)
        score_adv = min(30, adv_ratio / 100 * 30)

        total_ups = boards.get("total_limit_up", 0)
        score_ups = min(20, total_ups / 80 * 20)

        highest = boards.get("highest_board", 0)
        score_height = min(20, highest / 7 * 20)

        yest_avg = yest_perf.get("avg_change_pct", 0)
        score_yest = min(15, (yest_avg + 5) / 10 * 15) if yest_avg > -5 else 0

        lhb = self.dragon_tiger_list()
        if not lhb.empty:
            positive_ratio = (lhb["net_buy_amount"] > 0).sum() / len(lhb) * 100
            score_lhb = min(15, positive_ratio / 100 * 15)
        else:
            score_lhb = 7.5

        total = round(score_adv + score_ups + score_height + score_yest + score_lhb, 1)

        if total >= 75:
            label = "强势"
        elif total >= 50:
            label = "偏强"
        elif total >= 35:
            label = "中性"
        else:
            label = "弱势"

        return {
            "score": total,
            "label": label,
            "components": {
                "advance_decline": round(score_adv, 1),
                "limit_up_count": round(score_ups, 1),
                "board_height": round(score_height, 1),
                "yesterday_performance": round(score_yest, 1),
                "dragon_tiger": round(score_lhb, 1),
            },
            "details": {
                "advance_ratio": adv_ratio,
                "total_limit_up": total_ups,
                "highest_board": highest,
                "yest_avg_change": yest_avg,
            },
            "date": datetime.now().strftime("%Y-%m-%d"),
        }
