# A 股短线选股系统

针对 **2-14 天持股周期**设计的 A 股选股与策略研究工具。不做自动交易，专注 **选股信号发现 + 策略回测验证**。

## 架构

```
Layer 1 ─ 数据层 (adata + sentiment) ──→ 股票代码缓存 / 实时行情 / K线 / 资金流向 / 概念 / 涨停池 / 龙虎榜 / 情绪指标
     ↓
Layer 2 ─ 扫描层          ──→ 9 种技术形态 + 4 种情绪信号 + 19 个量化因子 + 选股引擎
     ↓
Layer 3 ─ 回测层          ──→ 策略回测（持股周期/止盈止损/评分过滤）
     ↓
Layer 4 ─ 分析层          ──→ 个股深度分析 + 选股报告（Markdown）
```

### 技术形态（Layer 2）

| 信号 | 描述 | 强度 |
|------|------|------|
| 放量突破 | 涨幅>3% 且 成交量>5日均量×1.5 | ★★★★ |
| MA5金叉MA10 | 短期均线黄金交叉 | ★★★ |
| 均线多头排列 | MA5 > MA10 > MA20, 趋势向上 | ★★★ |
| MACD金叉 | DIF 上穿 DEA，零轴上方更强 | ★★★★ |
| KDJ超卖金叉 | K<30 区域上穿 D 线 | ★★★★ |
| RSI上穿50 | 由弱转强 | ★★★ |
| 连续3日放量 | 成交量连续递增 | ★★★ |
| 涨停回踩10日线 | 强势股回调支撑 | ★★★★ |
| 平台突破 | 突破 20 日最高点 | ★★★★ |

### 情绪面信号

| 信号 | 描述 | 强度 |
|------|------|------|
| 连板梯队 | 连续涨停识别，最高连板=龙头信号 | ★★★★★ |
| 炸板回封 | 长下影线+放量微涨，主力回封 | ★★★ |
| 弱转强反包 | 前日大跌→今日放量反包 | ★★★★ |
| 情绪周期 | 主升浪/超跌反弹/加速冲顶判断 | ★★★★ |

### 量化因子（Qlib 风格）

内置 19 个因子：动量(5)、波动(4)、量价(4)、形态(3)、资金面(1) + 因子组合加权评分

## 快速开始

### 安装依赖

```bash
pip3 install -r requirements.txt
```

### 每日选股

```bash
# 全市场扫描（价格3-100元，非ST，输出前30只）
python3 run_daily.py

# 只看前 20 只
python3 run_daily.py --top 20

# 包含概念信息（耗时稍长）
python3 run_daily.py --with-concepts

# 包含资金流向
python3 run_daily.py --with-flow

# 生成 Markdown 报告（含情绪指标和因子评分）
python3 run_daily.py --report

# 单只股票快速分析（含技术+情绪+因子评分）
python3 run_daily.py --quick 000001
python3 run_daily.py --quick 600519

# 市场简报（涨跌分布/涨停数）
python3 run_daily.py --brief

# 市场情绪概览（涨停池/连板梯队/涨跌比/综合评分）
python3 run_daily.py --sentiment

# 禁用新功能
python3 run_daily.py --no-sentiment    # 只保留传统技术形态信号
python3 run_daily.py --no-factors      # 禁用量化因子评分
```

### 策略回测

```bash
# 默认回测（持股7天，2025年至今）
python3 run_backtest.py

# 持股5天
python3 run_backtest.py --holding 5

# 持股14天
python3 run_backtest.py --holding 14

# 指定回测区间
python3 run_backtest.py --start 2025-01-01 --end 2025-06-01
```

### 修改选股参数

编辑 `config.yaml`:

```yaml
screen:
  min_price: 3.0        # 最低股价
  max_price: 100.0       # 最高股价
  min_turnover: 1.0      # 最低换手率
  max_market_cap: 500    # 最高市值（亿）
  top_n: 30              # 输出前 N 只
  min_score: 2           # 最低信号评分

backtest:
  holding_days: 7        # 持股天数
  stop_loss: -7.0        # 止损 %
  take_profit: 20.0      # 止盈 %

sentiment:
  enable_board_tier: true    # 连板梯队分析
  enable_reopen: true        # 炸板回封识别
  enable_weak2strong: true   # 弱转强识别

factors:
  enable: true               # 因子评分
  weights:                   # 因子权重
```

## 文件结构

```
a股/
├── config.yaml              # 配置文件
├── requirements.txt         # Python 依赖
├── run_daily.py             # 每日选股入口
├── run_backtest.py          # 回测入口
├── README.md
├── layer1_data/
│   ├── fetcher.py           # 数据层 — 封装 adata
│   └── sentiment.py         # 情绪数据 — 涨停池/龙虎榜/情绪指标（对标 akshare）
├── layer2_scan/
│   ├── patterns.py          # 9 种短线技术形态识别
│   ├── sentiment_signals.py # 情绪面信号 — 连板梯队/弱转强/炸板回封（对标 WalkerLau/stock）
│   ├── alpha_factors.py     # 量化因子引擎 — 19 因子 + 组合评分（对标 Qlib）
│   └── screener.py          # 选股引擎（初筛 → 扫描 → 综合评分）
├── layer3_backtest/
│   └── backtest.py          # 回测引擎
├── layer4_analysis/
│   └── analyzer.py          # 个股分析 + 市场简报 + 报告生成
├── data/                    # 缓存目录
└── reports/                 # 选股报告输出
```

## 数据来源

底层使用 [adata](https://github.com/1nchaos/adata) 库：

| 数据 | 来源 | 说明 |
|------|------|------|
| 实时行情 | 新浪/腾讯 | `list_market_current()` |
| K线数据 | 东方财富 | `get_market()` |
| 资金流向 | 东方财富 | `all_capital_flow_east()` |
| 概念信息 | 东方财富 | `get_concept_east()` |
| 股票代码 | 本地缓存 | `adata/stock/cache/code.csv` |

## 许可

MIT
