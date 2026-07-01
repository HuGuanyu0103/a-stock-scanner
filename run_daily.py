#!/usr/bin/env python3
"""
每日选股扫描入口

使用方式（新增 --push 推送结果到微信）:
  python run_daily.py                          # 全量扫描
  python run_daily.py --top 20                 # 只看前 20
  python run_daily.py --quick 000001           # 单只股票快速分析
  python run_daily.py --report                 # 生成 Markdown 报告
  python run_daily.py --brief                  # 市场简报
  python run_daily.py --with-flow              # 含资金流向
  python run_daily.py --with-concepts          # 含概念信息
  python run_daily.py --push                   # 选股+推送到微信
  python run_daily.py --brief --push           # 简报+推送到微信
  python run_daily.py --batch 30               # 每批30只股票行情
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime

import yaml

from layer1_data import DataFetcher
from layer2_scan import StockScreener
from layer4_analysis import StockAnalyzer


def setup_logging(level: str = "INFO"):
    fmt = "%(asctime)s [%(levelname)s] %(message)s"
    logging.basicConfig(level=getattr(logging, level.upper(), logging.INFO),
                        format=fmt, datefmt="%H:%M:%S")
    # 避免大量第三方库日志干扰
    for name in ("urllib3", "requests", "urllib3.connectionpool", "adata"):
        logging.getLogger(name).setLevel(logging.WARNING)


def load_config(path: str = "config.yaml") -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except FileNotFoundError:
        return {}


def cmd_screen(args, config: dict):
    cfg = config.get("screen", {})
    fetcher = DataFetcher()
    screener = StockScreener(fetcher=fetcher)

    kwargs = {
        "min_price": cfg.get("min_price", 3.0),
        "max_price": cfg.get("max_price", 100.0),
        "min_volume": cfg.get("min_volume", 0),
        "exclude_st": cfg.get("exclude_st", True),
        "exclude_chinext": cfg.get("exclude_chinext", True),
        "top_n": args.top or cfg.get("top_n", 30),
        "min_score": cfg.get("min_score", 2),
        "batch_size": args.batch or cfg.get("batch_size", 50),
    }

    if args.with_concepts:
        result = screener.screen_with_concepts(**kwargs)
    elif args.with_flow:
        result = screener.screen_with_capital_flow(**kwargs)
    else:
        result = screener.screen(**kwargs)

    if result.empty:
        print("\n没有符合条件的股票")
        return

    # 表头
    time_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    print(f"\n{'='*100}")
    print(f"  选股结果 — {time_str}  |  共 {len(result)} 只")
    print(f"{'='*100}")
    print(f" {'综合':>5} {'形态':>4} {'情绪':>4} {'因子':>5} {'代码':>8} {'名称':<7} {'价格':>8} {'涨幅%':>6} {'信号':<42}")
    print("-" * 100)

    for _, row in result.iterrows():
        name = str(row.get("short_name", ""))[:6]
        sig = str(row.get("signal_names", ""))[:38]
        pct = row.get("change_pct", 0)
        price = row.get("price", 0)
        combined = row.get("combined_score", row.get("signal_score", 0))
        ts = row.get("signal_score", 0)
        ss = row.get("sentiment_score", 0)
        fs = row.get("factor_score", 0)
        print(f" {combined:>5.1f} {ts:>4} {ss:>4} {fs:>5.1f}  "
              f"{row['stock_code']:>8}  "
              f"{name:<7}  {price:>7.2f}  {pct:>+5.1f}  {sig}")

    # 报告
    if args.report:
        report_dir = config.get("report", {}).get("output_dir", "reports")
        os.makedirs(report_dir, exist_ok=True)
        date_str = datetime.now().strftime("%Y%m%d_%H%M")
        path = f"{report_dir}/选股报告_{date_str}.md"
        analyzer = StockAnalyzer(fetcher=fetcher)
        analyzer.generate_report(result, output_path=path)
        print(f"\n报告已保存: {path}")


def cmd_quick(args, config: dict):
    fetcher = DataFetcher()
    screener = StockScreener(fetcher=fetcher)
    analyzer = StockAnalyzer(fetcher=fetcher)
    code = args.quick.upper()

    print(f"\n{'='*60}")
    print(f"  📊 分析: {code}")
    print(f"{'='*60}")

    result = screener.quick_scan(code)
    if "error" in result:
        print(f"  ❌ {result['error']}")
        return

    sigs = result.get("all_signals", result.get("signals", []))
    names = result.get("signal_names", [])
    ts = result.get("tech_score", 0)
    ss = result.get("sentiment_score", 0)
    fs = result.get("factor_score", 0)
    cs = result.get("combined_score", 0)
    print(f"  综合评分: {cs}  |  技术:{ts}  情绪:{ss}  因子:{fs}  |  信号数: {len(sigs)}")
    if names:
        print(f"  信号: {' | '.join(names)}")
        for s in sigs:
            level = s.get("level", 0)
            stars = "★" * min(level, 5)
            print(f"    - [{stars}] {s['name']}: {s['desc']}")

    deep = analyzer.analyze_stock(code)
    if "summary" in deep:
        print(f"\n  综合: {deep['summary']}")

    # 显示因子详情
    fd = result.get("factor_details", {})
    if fd:
        top_factors = sorted(fd.items(), key=lambda x: abs(x[1]), reverse=True)[:5]
        print("\n  核心因子:")
        for k, v in top_factors:
            print(f"    {k}: {v:+.2f}")
    if deep.get("support") and deep.get("resistance"):
        print(f"  支撑: {deep['support']:.2f}  |  阻力: {deep['resistance']:.2f}")



def cmd_sentiment(args, config: dict):
    """市场情绪概览"""
    try:
        from layer1_data import SentimentData
        sd = SentimentData()
        score = sd.sentiment_score()
        print(f"\n{'='*50}")
        print(f"  📊 市场情绪 — {score['date']}")
        print(f"{'='*50}")
        print(f"  综合评分: {score['score']}  |  状态: {score['label']}")
        print(f"\n  分项得分:")
        comp = score['components']
        print(f"    涨跌比: {comp['advance_decline']:.1f}/30")
        print(f"    涨停数: {comp['limit_up_count']:.1f}/20")
        print(f"    连板高度: {comp['board_height']:.1f}/20")
        print(f"    昨日涨停表现: {comp['yesterday_performance']:.1f}/15")
        print(f"    龙虎榜: {comp['dragon_tiger']:.1f}/15")
        det = score['details']
        print(f"\n  详情: 上涨比{det['advance_ratio']}% | 涨停{det['total_limit_up']}家 | "
              f"最高{det['highest_board']}板 | 昨涨均{det['yest_avg_change']}%")
        # 连板梯队
        bt = sd.board_tiers()
        if bt.get("tier_distribution"):
            print(f"\n  连板梯队:")
            for k, v in sorted(bt['tier_distribution'].items()):
                board_num = k.replace("tier_", "")
                label = "高位" if board_num == "high" else board_num + "板"
                print(f"    {label}: {v}只")
    except Exception as e:
        print(f"  情绪数据获取失败: {e}")

def cmd_brief(args, config: dict):
    fetcher = DataFetcher()
    analyzer = StockAnalyzer(fetcher=fetcher)
    brief = analyzer.market_brief()
    if "error" in brief:
        print(f"  ❌ {brief['error']}")
        return
    print(f"\n{'='*40}")
    print(f"  📈 市场简报 — {brief['trade_date']}")
    print(f"{'='*40}")
    print(f"  上涨: {brief['up_count']} 只")
    print(f"  下跌: {brief['down_count']} 只")
    print(f"  涨停: {brief['limit_up_count']} 只")
    print(f"  总数: {brief['total_count']} 只")




def cmd_push_scan(args, config: dict):
    """全量选股并推送结果到微信"""
    from push import send, get_send_key

    key = get_send_key(config)
    if not key:
        print("❌ 未配置 Server酱 SendKey，请先在 config.yaml 中设置 serverchan.send_key")
        print("   注册地址: https://sct.ftqq.com/")
        return

    # 先跑选股
    cmd_screen(args, config)

    print("\n📤 推送结果到微信...")
    try:
        from push import send
        title = f"📈 A股选股结果 — {__import__('datetime').datetime.now().strftime('%m-%d %H:%M')}"
        content = f"选股完成，共 {args.top or 30} 只\n\n在终端查看完整结果"
        r = send(title, content, config=config)
        if r.get("code") == 0:
            print("  ✅ 结果已推送到微信")
        else:
            print(f"  ⚠️ 推送失败: {r.get('message')}")
    except Exception as e:
        print(f"  ⚠️ 推送出错: {e}")


def main():
    parser = argparse.ArgumentParser(
        description="A 股短线选股系统",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""示例:
  python run_daily.py                    全量选股
  python run_daily.py --top 20           只看前 20
  python run_daily.py --report           选股+生成报告
  python run_daily.py --quick 000001     单只分析
  python run_daily.py --brief            市场简报
  python run_daily.py --with-concepts    含概念
  python run_daily.py --with-flow        含资金流向
  python run_daily.py --batch 30         每批30只股票
  python run_daily.py --sentiment        市场情绪概览（涨停/连板/涨跌比）
  python run_daily.py --no-sentiment     禁用情绪面信号
  python run_daily.py --no-factors       禁用量化因子评分
        """,
    )
    parser.add_argument("--top", type=int, default=0, help="输出前 N 只")
    parser.add_argument("--quick", type=str, default="", help="单只股票快速分析")
    parser.add_argument("--report", action="store_true", help="生成 Markdown 报告")
    parser.add_argument("--brief", action="store_true", help="市场简报")
    parser.add_argument("--sentiment", action="store_true", help="市场情绪概览")
    parser.add_argument("--with-concepts", action="store_true", help="含概念信息")
    parser.add_argument("--with-flow", action="store_true", help="含资金流向")
    parser.add_argument("--batch", type=int, default=0, help="每批获取行情的股票数")
    parser.add_argument("--no-sentiment", action="store_true", help="禁用情绪面信号")
    parser.add_argument("--no-factors", action="store_true", help="禁用量化因子评分")
    parser.add_argument("--config", type=str, default="config.yaml")
    parser.add_argument("--push", action="store_true", help="推送结果到微信（需配置 Server酱）")
    parser.add_argument("--log-level", type=str, default="")

    args = parser.parse_args()
    config = load_config(args.config)
    log_level = args.log_level or config.get("logging", {}).get("level", "INFO")
    setup_logging(log_level)

    if args.quick:
        cmd_quick(args, config)
    elif args.push and args.brief:
        cmd_brief(args, config)          # --push 在 cmd_brief 内部处理
    elif args.push:
        cmd_push_scan(args, config)
    elif args.sentiment:
        cmd_sentiment(args, config)
    elif args.brief:
        cmd_brief(args, config)
    else:
        cmd_screen(args, config)


if __name__ == "__main__":
    main()
