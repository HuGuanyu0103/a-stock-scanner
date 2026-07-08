---
name: ui
description: "UI optimization self-iterative loop — extract structured visual data, review with ui-ux-pro-max, fix, verify, repeat until quality threshold or timeout"
---

# /ui — UI 优化自迭代工作流

对产品界面进行闭环自迭代优化。由于 AI 无法「看」截图，本流程使用 Playwright 提取**结构化视觉数据**替代截图做分析。

## 触发条件

当用户说 `/ui` 或「优化界面」「改进 UI」「界面美化」时启动。

## 工作流

### Phase 0: 准备
1. 启动 Flask app：`python3 board_flow_dashboard/app.py`（后台运行）
2. 确认 ui-ux-pro-max skill 可用：`.claude/skills/ui-ux-pro-max/scripts/search.py`
3. 确认 Playwright Python 可用：`from playwright.sync_api import sync_playwright`

### Phase 1: 基线采集
用 Playwright 提取结构化视觉快照（非截图图片）：

```python
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page(viewport={'width': 1440, 'height': 900})

    # 收集 JS 错误
    errors = []
    page.on('console', lambda msg: errors.append(f'[{msg.type}] {msg.text}') if msg.type == 'error' else None)

    page.goto('http://127.0.0.1:8080', wait_until='networkidle', timeout=15000)
    page.wait_for_timeout(3000)

    # ── 结构化快照（每一轮必须采集的字段）────

    # 1. 元素存在性 & 可见性
    page.evaluate("""() => {
        const results = {};
        const selectors = [
            '.left-panel', '.right-panel', '#sectorChart', '#rankList',
            '#tabFlow', '#tabPicks', '#chatMessages', '#chatSuggestions',
            '.stale-banner', '.chart-legend', '.rank-item',
            '.sector-type-btn', '.pool-toggle', '.picks-table'
        ];
        selectors.forEach(sel => {
            const el = document.querySelector(sel);
            results[sel] = el ? { visible: el.offsetParent !== null, w: el.offsetWidth, h: el.offsetHeight } : null;
        });
        return results;
    }""")

    # 2. 计算后的样式（颜色/字体/间距）
    page.evaluate("""() => {
        const els = document.querySelectorAll('.chart-card, .rank-card, .left-panel, .right-panel, body, .chat-msg');
        const results = [];
        els.forEach(el => {
            const cs = getComputedStyle(el);
            results.push({
                selector: el.className || el.tagName,
                bg: cs.backgroundColor,
                color: cs.color,
                font: cs.fontFamily.split(',')[0],
                fontSize: cs.fontSize,
                padding: cs.padding,
                borderRadius: cs.borderRadius,
                boxShadow: cs.boxShadow,
                gap: cs.gap,
            });
        });
        return results;
    }""")

    # 3. 布局度量
    page.evaluate("""() => ({
        viewport: { w: window.innerWidth, h: window.innerHeight },
        leftPanel: document.querySelector('.left-panel')?.getBoundingClientRect(),
        rightPanel: document.querySelector('.right-panel')?.getBoundingClientRect(),
        chart: document.querySelector('#sectorChart')?.getBoundingClientRect(),
    })""")

    # 4. 交互验证
    # - 切换 tab → 检查元素可见性变化
    # - Hover → 检查 CSS 变化
    # - 点击 → 检查 DOM 变化

    # 5. 截图仍保存（给用户看，非 AI 分析用）
    page.screenshot(path='/tmp/ui_round_0.png', full_page=False)
```

### Phase 2: 设计审查（每轮）

1. **读结构化快照** → 列出所有异常值
2. **调用 ui-ux-pro-max**：
   ```bash
   python3 .claude/skills/ui-ux-pro-max/scripts/search.py --domain color --design-system "project style"
   python3 .claude/skills/ui-ux-pro-max/scripts/search.py --domain ux --max-results 5 "pattern"
   python3 .claude/skills/ui-ux-pro-max/scripts/search.py --domain chart --max-results 5 "chart type"
   ```
3. **对照检查清单**：

   | # | 检查项 | 方法 |
   |---|--------|------|
   | 1 | 无 emoji 作图标 | `page.evaluate` 扫描按钮/标题 textContent |
   | 2 | 避免纯白背景 `#fff/rgb(255,255,255)` | 计算样式 bg 字段 |
   | 3 | Hover 有 150-300ms 过渡 | 检查 CSS transition 值 |
   | 4 | 文字对比度 ≥ 4.5:1 | 从 fg/bg 色值计算相对亮度 |
   | 5 | 可点击元素有 cursor:pointer | 扫描所有 button/a 的 cursor 值 |
   | 6 | 无 JS 错误 | console 日志 |
   | 7 | prefers-reduced-motion | CSS 中查 `@media (prefers-reduced-motion)` |
   | 8 | 响应式断点 | 切换 375/768/1024/1440 宽度各检查一次 |
   | 9 | 左右分栏比例 ~60:40 | 布局度量 leftPanel/rightPanel 宽度比 |
   | 10 | ECharts 渲染 canvas | `document.querySelector('#sectorChart canvas')` |

4. **生成修改**：用 Edit/Write 工具修改 `board_flow_dashboard/static/index.html`
5. **验证**：刷新页面，重新采集结构化快照，逐项对比通过/失败

### Phase 3: 终止条件
- 连续 2 轮所有检查项通过
- 累计 5 轮
- 累计耗时 30 分钟

### Phase 4: 收尾
1. 停 Flask app
2. `git add` + `git commit -m "vX.Y: /ui loop 优化 XXX"` + `git push origin main`
3. 报告：通过项数 / 改动行数 / 截图路径

## 设计原则（来自 ui-ux-pro-max）

- **风格**：Minimalism & Swiss Style — 简洁、功能性、高对比度、网格布局
- **色彩**：Apple — `#f5f5f7` 底、`#fafbfc` 卡片、`#0071e3` 强调、红涨绿跌
- **字体**：`-apple-system, 'PingFang SC'` 系统字体栈
- **间距**：宽松留白、8px 倍数、12px 大圆角
- **动效**：150-200ms 过渡、尊重 reduced-motion

## 项目上下文

- 目标文件：`board_flow_dashboard/static/index.html`
- 应用入口：`board_flow_dashboard/app.py`（Flask :8080）
- 产品定位：A 股 ToC 智能看板 — 左看板（Tab 切换）+ 右 AI 对话
- 用户偏好：Apple 品质感、简洁、克制、留白
