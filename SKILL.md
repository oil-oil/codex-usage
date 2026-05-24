---
name: codex-usage
description: 生成中文 Codex Token 使用报告，数据来自本机 Codex SQLite state 数据库。适用于查看 Codex Token 消耗、state_5.sqlite、threads.tokens_used、月度趋势、来源拆分、模型统计、高消耗会话、导出 JSON 数据，以及把 Agent 自定义 Token 分析脚本渲染成 html-doc 风格 HTML 报告。
---

# Codex Usage

## 这个 Skill 做什么

从 Codex `state_*.sqlite` 数据库生成中文 Token 使用报告。报告面向普通用户阅读，默认使用 `html-doc` 风格，包含月度指标卡、真实 SVG 图表、月份选择器、曲线 hover 指标、可筛选表格、可排序表格和可复制 SQLite 查询。也支持让 Agent 编写一个只包含分析逻辑的 Python 脚本，再由 Skill 自动渲染成 HTML。所有展示数字统一使用 `万`、`亿` 等中文单位。

## 什么时候使用

用户提到下面任意需求时使用这个 Skill：

- 想查看 Codex 过去消耗了多少 Token。
- 想按月份、来源、模型、提供方查看 Token 分布。
- 想找出最高消耗的 Codex 会话。
- 想把 Codex SQLite 使用数据做成中文 HTML 报告。
- 想让 Agent 读取 Token 数据后补充分析，并自动渲染成 HTML。
- 想给其他用户一个可复用的 Token 使用报告生成工具。

## Agent 执行流程

1. 确认数据库路径。优先使用用户传入的 `--db /path/to/state_5.sqlite`；如果用户没有传入，脚本会依次尝试 `$CODEX_USAGE_DB`、`$CODEX_SQLITE_HOME/state_5.sqlite`、`config.toml` 里的 `sqlite_home`、`$CODEX_HOME/state_5.sqlite`、`~/.codex/state_5.sqlite`。
2. 执行 `scripts/generate_codex_usage_report.py`。默认渲染器是 `html-doc`，会同时生成 `.html` 和 `.md`。
3. 把 HTML 报告路径告诉用户。报告是自包含文件，可以直接用浏览器打开。
4. 如果用户需要原始汇总数据，加上 `--json-out` 输出 JSON。

## 自定义分析工作流

当用户希望 Agent 补充自己的 Token 分析时，使用两步流程：先生成数据 JSON，再让 Agent 写一个 `analyze(report)` 脚本，最后交给 `render_token_analysis.py` 渲染。Agent 只写分析逻辑，不写 HTML。

生成数据：

```bash
python3 /path/to/codex-usage/scripts/generate_codex_usage_report.py \
  --db "$HOME/.codex/state_5.sqlite" \
  --renderer data \
  --json-out ./codex-usage-report.json
```

生成分析脚本模板：

```bash
python3 /path/to/codex-usage/scripts/render_token_analysis.py \
  --init-script ./token-analysis.py
```

分析脚本只需要返回结构化数据：

```python
def analyze(report):
    return {
        "title": "2026 年 5 月 Codex Token 分析",
        "summary": "高峰日期和主要入口决定本月消耗。",
        "metrics": [{"label": "本月 Token", "value": "36.55亿", "note": "331 个会话"}],
        "findings": [
            {
                "priority": "P1",
                "title": "高峰日期贡献明显",
                "evidence": "5 月 6 日达到 4.43亿",
                "recommendation": "优先复盘高峰日期的长上下文任务。",
            }
        ],
        "tables": [],
    }
```

渲染分析 HTML：

```bash
python3 /path/to/codex-usage/scripts/render_token_analysis.py \
  --report-json ./codex-usage-report.json \
  --analysis-script ./token-analysis.py \
  --out ./codex-token-analysis.html
```

`analyze(report)` 支持返回 `title`、`summary`、`tags`、`metrics`、`findings`、`sections`、`tables`、`actions` 和 `blocks`。其中 `tables` 会渲染成可搜索、可排序的 html-doc 表格；`blocks` 可以传入原生 html-doc 组件 JSON。

## 最短命令

```bash
python3 /path/to/codex-usage/scripts/generate_codex_usage_report.py \
  --db "$HOME/.codex/state_5.sqlite" \
  --out ./codex-usage-report.html
```

给其他用户使用时，让对方的 Agent 传入已发现的数据库路径：

```bash
python3 /path/to/codex-usage/scripts/generate_codex_usage_report.py \
  --db "/Users/alex/.codex/state_5.sqlite" \
  --out "/Users/alex/Desktop/codex-usage-report.html" \
  --json-out "/Users/alex/Desktop/codex-usage-report.json"
```

## 常用参数

- `--db PATH`: SQLite 数据库路径，通常指向 `state_5.sqlite`。
- `--codex-home PATH`: Codex home 目录，只在没有传入 `--db` 时使用；脚本也会读取该目录下 `config.toml` 里的 `sqlite_home`。
- `--out PATH`: HTML 报告输出路径。
- `--json-out PATH`: 可选 JSON 汇总输出路径。
- `--snapshot PATH`: SQLite 快照输出路径。
- `--no-snapshot`: 直接查询源数据库。
- `--since YYYY-MM-DD`: 只包含本机日期不早于该日期的会话。
- `--until YYYY-MM-DD`: 只包含本机日期不晚于该日期的会话。
- `--top N`: 高消耗会话数量，默认 `25`。
- `--title TEXT`: 报告标题。
- `--renderer html-doc|standalone|md|data`: 渲染方式，默认 `html-doc`；`data` 只输出 JSON。
- `--html-doc-dir PATH`: `html-doc` skill 目录。
- `--md-out PATH`: `html-doc` Markdown 输出路径。
- `scripts/render_token_analysis.py --init-script PATH`: 生成自定义分析脚本模板。
- `scripts/render_token_analysis.py --report-json PATH --analysis-script PATH --out PATH`: 把自定义分析渲染成 HTML。

## 报告内容

生成的报告包含这些用户可见内容：

- 总览：累计 Token、会话数、平均值、最大会话、未归档占比、子 Agent 占比。
- 月度仪表盘：初始展示本机当前月份；顶部指标卡按所选月份联动更新。
- 月份选择器：使用下拉选择月份，不平铺所有月份按钮。
- 真实图表：曲线图展示所选月份的每一天，hover 时展示日期、Token、会话数、均值和本月占比。
- 月份联动：切换月份后，来源环形饼图、模型环形饼图和峰值日期柱形图同步更新。
- 明细表格：月度明细、默认月份每日明细、来源拆分、模型/提供方、高消耗会话。
- 交互能力：搜索、筛选、排序、复制常用 SQLite 查询。

报告正文要像给用户阅读的分析页面，标题和说明使用“Codex Token 消耗报告”“每日 Token 趋势”“主要入口”“高消耗会话”这类自然表达。不要把初始化月份、切换能力、主账本、快照口径、字段来源这类实现说明写进正文；只有当用户主动追问数据口径时，再在回复里解释。

## 数据口径

- 主要统计字段是 `threads.tokens_used`。
- 必需字段是 `threads.created_at` 和 `threads.tokens_used`。
- 官方源码里 `threads.tokens_used` 来自线程级 `total_token_usage.total_tokens` 或 `token_usage.total_tokens`，脚本按会话汇总这个字段。
- 官方源码使用的状态库文件名是 `state_5.sqlite`，SQLite 状态目录可通过 `sqlite_home` 或 `CODEX_SQLITE_HOME` 调整。
- 如果存在 `model`、`model_provider`、`source`、`title`、`archived`、`rollout_path`、`cwd` 等字段，报告会展示更多维度。
- 来源字段会转成用户可读名称，例如 `vscode` 显示为“Codex 桌面端”、`exec` 显示为“自动执行”、`cli` 显示为“命令行”。
- 默认会在 HTML 输出文件旁创建 SQLite 快照，让报告数字和生成时刻保持一致。
- `assets/report-template.html` 只是 `--renderer standalone` 的兜底模板；常规报告优先使用 `html-doc`。
