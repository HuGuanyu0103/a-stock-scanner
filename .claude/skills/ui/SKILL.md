---
name: ui
description: "UI optimization self-iterative loop — screenshot, analyze with ui-ux-pro-max, fix, re-screenshot, repeat until quality threshold or timeout"
---

# /ui — UI 优化自迭代工作流

对产品界面进行闭环自迭代优化：截图 → 设计审查 → 修改 → 再截图 → 循环，直到满意或超时。

## 触发条件

当用户说 `/ui` 或「优化界面」「改进 UI」「界面美化」时启动。

## 工作流

### Phase 0: 准备
1. 启动 Flask app（`python3 board_flow_dashboard/app.py` 后台运行）
2. 确认 ui-ux-pro-max skill 可用（`.claude/skills/ui-ux-pro-max/scripts/search.py`）
3. 用 Python Playwright（`playwright` 包）操作浏览器

### Phase 1: 基线截图
```python
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page(viewport={'width': 1440, 'height': 900})
    page.goto('http://127.0.0.1:8080', wait_until='networkidle', timeout=15000)
    page.wait_for_timeout(3000)
    page.screenshot(path='/tmp/ui_round_0.png', full_page=False)
    browser.close()
```

### Phase 2: 设计审查（每轮）
每个 Round 执行以下步骤：

1. **收集视觉数据**：用 Playwright 检查布局尺寸、颜色值、元素可见性、JS 错误
2. **调用 ui-ux-pro-max**：
   ```bash
   python3 .claude/skills/ui-ux-pro-max/scripts/search.py --domain color --design-system "project style keywords"
   python3 .claude/skills/ui-ux-pro-max/scripts/search.py --domain ux --max-results 5 "interaction pattern"
   python3 .claude/skills/ui-ux-pro-max/scripts/search.py --domain chart --max-results 5 "chart type"
   ```
3. **对照检查清单**：
   - [ ] 无 emoji 作图标（用 SVG 或纯文本）
   - [ ] 避免纯白背景（#ffffff → #f8fafc/fafbfc）
   - [ ] Hover 状态有平滑过渡（150-300ms）
   - [ ] 文字对比度 ≥ 4.5:1（浅色模式）
   - [ ] 可点击元素有 cursor:pointer
   - [ ] 响应式：375px / 768px / 1024px / 1440px
   - [ ] prefers-reduced-motion 支持
   - [ ] 聚焦态可见（键盘导航）
4. **生成修改**：用 Edit/Write 工具修改 `board_flow_dashboard/static/index.html`
5. **验证**：刷新页面截图，检查 JS 错误、布局完整性、交互功能

### Phase 3: 终止条件
满足任一即停止：
- **连续 2 轮自评合格**（所有检查清单项通过）
- **累计耗时超过 30 分钟**
- **累计 5 轮**

### Phase 4: 收尾
1. 停 Flask app
2. `git add` + `git commit` + `git push origin main`
3. 报告改动摘要 + 截图路径

## 设计原则（来自 ui-ux-pro-max）

- **风格**：Minimalism & Swiss Style — 简洁、功能性、高对比度、网格布局
- **色彩**：Apple 风格 — 浅灰底 `#f5f5f7`、卡片 `#fafbfc`、强调 `#0071e3`、红涨绿跌
- **字体**：系统字体栈 `-apple-system, 'PingFang SC'` — 中文字体首选
- **间距**：宽松留白、8px 网格、大圆角（12px）
- **动效**：150-200ms 过渡、无弹跳、尊重 reduced-motion

## 项目上下文

- 目标文件：`board_flow_dashboard/static/index.html`
- 应用入口：`board_flow_dashboard/app.py`（Flask :8080）
- 产品定位：A 股 ToC 智能看板 — 左看板（Tab 切换）+ 右 AI 对话
- 用户偏好：Apple 品质感、简洁、克制、留白

## 示例对话

```
用户: /ui
Claude:
  Phase 0: 启动 app → Playwright 截图
  Round 1: 布局检查 → ui-ux-pro-max 审查 → 发现 emoji + 纯白背景 → 修复
  Round 2: 再截图 → 验证通过 → 连续 2 轮合格 → 停止
  Phase 4: git push → 报告
```
