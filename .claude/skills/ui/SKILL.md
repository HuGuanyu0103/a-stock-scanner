---
name: ui
description: "UI optimization self-iterative loop — screenshot via Playwright, visual review via vision model API, fix, repeat until quality threshold or timeout"
---

# /ui — UI 优化自迭代工作流

Playwright 截图 → 视觉模型分析 → 修改 → 再截图验证 → 循环至合格。

## 触发条件

用户说 `/ui` 或「优化界面」「改进 UI」「界面美化」。

## 工作流

### Phase 0: 准备
1. 启动 Flask：`python3 board_flow_dashboard/app.py &`
2. 确认 Playwright Python 可用
3. 确认视觉模型 API Key 可用（见下方配置）

### Phase 1: 每轮执行

```
┌──────────┐    ┌──────────────┐    ┌──────────────┐
│ Playwright│ →  │ 视觉模型 API │ →  │ Edit/Write   │
│ 截图保存  │    │ 分析+建议    │    │ 修改代码      │
└──────────┘    └──────────────┘    └──────────────┘
       ↑                                    │
       └────────── 下一轮 ──────────────────┘
```

#### 1a. 截图
```python
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page(viewport={'width': 1440, 'height': 900})
    page.goto('http://127.0.0.1:8080', wait_until='networkidle', timeout=15000)
    page.wait_for_timeout(3000)
    page.screenshot(path='/tmp/ui_round_N.png', full_page=True)
    browser.close()
```

#### 1b. 视觉分析
将截图发给视觉模型，要求结构化反馈：
```
你是一位资深 UI/UX 设计师。请审查这个网页界面截图，从以下维度给反馈：

1. 布局（平衡、留白、信息密度）
2. 色彩（和谐度、对比度、是否避免纯白背景）
3. 字体（可读性、层级、大小是否合适）
4. 交互（按钮/链接是否有辨识度、hover 状态）
5. 问题（对齐错误、溢出、截断、拥挤）

对每个问题，给出具体的 CSS 修改建议。只报告实际看到的问题，不要猜测。
```

#### 1c. 应用修改
根据视觉模型的反馈，用 Edit/Write 修改 `board_flow_dashboard/static/index.html`。

#### 1d. 验证
重复 1a → 1b，确认上次的问题已修复。对比两轮截图的变化。

### Phase 2: 终止条件
- 连续 2 轮视觉模型反馈「无明显问题」
- 累计 5 轮
- 累计 30 分钟

### Phase 3: 收尾
1. 停 Flask
2. `git add` + `git commit` + `git push origin main`
3. 报告每轮改动 + 最终截图

## 视觉模型 API 配置

使用 DeepSeek Anthropic 兼容端点（已验证可用）：

```python
import base64, json, requests

# 读取 API Key
key_path = 'board_flow_dashboard/data/deepseek_key.txt'
with open(key_path) as f:
    api_key = f.read().strip()

# 读取截图
with open('/tmp/ui_round_N.png', 'rb') as f:
    img_b64 = base64.b64encode(f.read()).decode()

# 调用视觉模型
resp = requests.post('https://api.deepseek.com/anthropic/v1/messages', headers={
    'x-api-key': api_key,
    'anthropic-version': '2023-06-01',
    'Content-Type': 'application/json'
}, json={
    'model': 'deepseek-chat',
    'max_tokens': 1024,
    'messages': [{
        'role': 'user',
        'content': [
            {'type': 'image', 'source': {'type': 'base64', 'media_type': 'image/png', 'data': img_b64}},
            {'type': 'text', 'text': '''你是一位资深 UI/UX 设计师。请审查这个网页界面截图，从以下维度给结构化反馈：

1. 布局：分栏比例、留白、信息密度是否合适
2. 色彩：是否和谐、对比度是否足够、是否避免纯白背景
3. 字体：可读性、层级、大小
4. 具体问题：对齐错误、溢出、截断、间距不统一
5. 与 Apple 设计风格的差距

每个问题给出具体的 CSS 修改建议（选择器 + 属性 + 值）。
只报告实际看到的，不要猜测。用中文回答。'''}
        ]
    }]
}, timeout=30)

result = resp.json()
analysis = ''.join(c['text'] for c in result.get('content', []) if c['type'] == 'text')
print(analysis)
```

## 项目上下文
- 目标文件：`board_flow_dashboard/static/index.html`
- 应用入口：`board_flow_dashboard/app.py`（Flask :8080）
- 产品定位：A 股 ToC 智能看板 — 左看板 + 右 AI 对话
- 设计目标：Apple 品质感、简洁、克制
