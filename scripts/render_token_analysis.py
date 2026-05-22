#!/usr/bin/env python3
"""Render a custom Codex token analysis script as an html-doc report."""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

from generate_codex_usage_report import (
    clip_text,
    fmt_pct,
    fmt_tokens,
    format_day_label,
    format_month_label,
    frontmatter_text,
    json_block,
    render_html_doc,
    resolve_html_doc_dir,
)


SCRIPT_TEMPLATE = '''"""Custom Codex token analysis.

The renderer passes the full usage JSON into analyze(report). Return plain
Python data; render_token_analysis.py will create the html-doc HTML.
"""


def _current_month_view(report):
    month = report.get("default_month") or ""
    views = report.get("month_views") or []
    return next((item for item in views if item.get("month") == month), views[-1] if views else {})


def analyze(report):
    view = _current_month_view(report)
    month = view.get("month") or report.get("default_month") or "当前月份"
    days = view.get("days") or []
    active_days = [day for day in days if int(day.get("tokens") or 0) > 0]
    top_days = sorted(active_days, key=lambda item: int(item.get("tokens") or 0), reverse=True)[:5]
    sources = (view.get("sources") or [])[:5]
    top_sessions = (view.get("top_sessions") or [])[:5]

    return {
        "title": f"{month} Token 使用分析",
        "summary": "这份分析聚焦当前月份的消耗峰值、入口集中度和高消耗会话。",
        "tags": ["月度分析", "Token", "Codex"],
        "metrics": [
            {"label": "本月 Token", "value": view.get("tokens_display", "0"), "note": f"{view.get('threads', 0)} 个会话"},
            {"label": "单会话均值", "value": view.get("avg_display", "0"), "note": "按本月会话平均"},
            {"label": "最大会话", "value": view.get("max_tokens_display", "0"), "note": "单个会话峰值"},
            {"label": "子 Agent 占比", "value": f"{view.get('subagent_share', 0):.2f}%", "note": view.get("subagent_tokens_display", "0")},
        ],
        "findings": [
            {
                "priority": "P1",
                "title": "高峰日期决定本月主要波动",
                "evidence": "、".join(f"{day.get('day')} {day.get('tokens_display')}" for day in top_days[:3]) or "暂无明显峰值",
                "recommendation": "优先复盘高峰日期里的长上下文任务和重复调用。",
            },
            {
                "priority": "P2",
                "title": "入口分布可以用于定位主要消耗场景",
                "evidence": "、".join(f"{item.get('source')} {item.get('share_display')}" for item in sources[:3]) or "暂无来源数据",
                "recommendation": "把主要入口和任务类型对应起来，判断是否需要拆分上下文或压缩工具输出。",
            },
        ],
        "tables": [
            {
                "id": "top-days",
                "title": "高消耗日期",
                "columns": [
                    {"key": "day", "label": "日期", "minWidth": "120px"},
                    {"key": "tokens", "label": "Token", "minWidth": "120px"},
                    {"key": "threads", "label": "会话数", "width": "90px"},
                    {"key": "share", "label": "占比", "width": "100px"},
                ],
                "rows": [
                    {
                        "day": day.get("day"),
                        "tokens": day.get("tokens_display"),
                        "threads": day.get("threads", 0),
                        "share": day.get("share_display"),
                    }
                    for day in top_days
                ],
            },
            {
                "id": "top-sessions",
                "title": "高消耗会话",
                "columns": [
                    {"key": "rank", "label": "#", "width": "56px"},
                    {"key": "tokens", "label": "Token", "minWidth": "120px"},
                    {"key": "source", "label": "来源", "width": "100px"},
                    {"key": "title", "label": "标题", "minWidth": "320px"},
                ],
                "rows": [
                    {
                        "rank": item.get("rank"),
                        "tokens": item.get("tokens_display"),
                        "source": item.get("source"),
                        "title": item.get("title"),
                    }
                    for item in top_sessions
                ],
            },
        ],
    }
'''


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="把 Agent 编写的 Token 分析脚本渲染成 html-doc HTML。",
    )
    parser.add_argument("--report-json", help="generate_codex_usage_report.py 输出的 JSON 文件。")
    parser.add_argument("--analysis-script", help="包含 analyze(report) 的 Python 分析脚本。")
    parser.add_argument("--out", default="codex-token-analysis.html", help="HTML 输出路径。")
    parser.add_argument("--md-out", help="可选 Markdown 输出路径，默认使用 <out>.md。")
    parser.add_argument("--html-doc-dir", help="html-doc skill 目录。")
    parser.add_argument("--title", help="覆盖分析报告标题。")
    parser.add_argument("--init-script", help="生成一个分析脚本模板，然后退出。")
    return parser.parse_args()


def write_template(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(SCRIPT_TEMPLATE, encoding="utf-8")
    print(f"分析脚本模板: {path}")


def load_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"JSON 解析失败：{path}\n{exc}") from exc


def load_analysis(path: Path):
    spec = importlib.util.spec_from_file_location("codex_usage_custom_analysis", path)
    if spec is None or spec.loader is None:
        raise SystemExit(f"无法加载分析脚本：{path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        raise SystemExit(f"分析脚本执行失败：{path}\n{exc}") from exc
    analyze = getattr(module, "analyze", None) or getattr(module, "build_analysis", None)
    if not callable(analyze):
        raise SystemExit("分析脚本必须定义 analyze(report) 或 build_analysis(report)。")
    return analyze


def as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def text(value: Any) -> str:
    return " ".join(str(value or "").split())


def default_month_view(report: dict[str, Any]) -> dict[str, Any]:
    month = report.get("default_month") or ""
    views = report.get("month_views") or []
    return next((item for item in views if item.get("month") == month), views[-1] if views else {})


def normalize_metric(item: Any) -> dict[str, Any]:
    if isinstance(item, dict):
        return {
            "label": text(item.get("label") or item.get("name")),
            "value": text(item.get("value")),
            "note": text(item.get("note") or item.get("description")),
            "spark": item.get("spark") if isinstance(item.get("spark"), list) else None,
        }
    return {"label": "指标", "value": text(item)}


def finding_tone(priority: str) -> str:
    normalized = priority.upper()
    if normalized in {"P0", "P1", "高", "HIGH"}:
        return "danger"
    if normalized in {"P2", "中", "MEDIUM"}:
        return "warning"
    return "info"


def finding_rows(findings: list[Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for index, item in enumerate(findings, start=1):
        if isinstance(item, str):
            rows.append(
                {
                    "priority": {"badge": "观察", "tone": "info"},
                    "finding": item,
                    "evidence": "",
                    "recommendation": "",
                }
            )
            continue
        if not isinstance(item, dict):
            continue
        priority = text(item.get("priority") or item.get("level") or f"#{index}")
        rows.append(
            {
                "priority": {"badge": priority, "tone": finding_tone(priority)},
                "finding": text(item.get("title") or item.get("finding") or item.get("name")),
                "evidence": clip_text(item.get("evidence") or item.get("why") or "", 120),
                "recommendation": clip_text(item.get("recommendation") or item.get("action") or item.get("next") or "", 120),
                "details": text(item.get("detail") or item.get("details") or item.get("impact")),
            }
        )
    return rows


def section_to_matrix(section: dict[str, Any], index: int) -> dict[str, Any]:
    title = text(section.get("title") or section.get("heading") or f"分析补充 {index}")
    rows: list[dict[str, Any]] = []
    for item_index, item in enumerate(as_list(section.get("items") or section.get("points")), start=1):
        if isinstance(item, dict):
            rows.append(
                {
                    "item": text(item.get("title") or item.get("name") or f"要点 {item_index}"),
                    "value": text(item.get("value") or item.get("detail") or item.get("body")),
                    "status": item.get("status") or item.get("tone") or "",
                }
            )
        else:
            rows.append({"item": f"要点 {item_index}", "value": text(item), "status": ""})
    if not rows and section.get("body"):
        rows.append({"item": "说明", "value": text(section.get("body")), "status": ""})
    return {
        "id": section.get("id") or f"analysis-section-{index}",
        "span": int(section.get("span") or 12),
        "title": title,
        "body": text(section.get("body")) if rows and section.get("body") and len(rows) > 1 else "",
        "columns": [
            {"key": "item", "label": "项目", "minWidth": "160px"},
            {"key": "value", "label": "内容", "minWidth": "260px"},
            {"key": "status", "label": "标记", "width": "110px"},
        ],
        "rows": rows,
    }


def normalize_table(table: dict[str, Any], index: int) -> dict[str, Any]:
    block = dict(table)
    block.setdefault("id", f"analysis-table-{index}")
    block.setdefault("span", 12)
    block.setdefault("search", True)
    block.setdefault("sortable", True)
    block.setdefault("columns", [])
    block.setdefault("rows", [])
    if not block.get("title"):
        block["title"] = f"分析表格 {index}"
    return block


def analysis_markdown(report: dict[str, Any], analysis: dict[str, Any], title_override: str | None = None) -> str:
    view = default_month_view(report)
    month_label = format_month_label(view.get("month") or report.get("default_month") or "")
    title = title_override or analysis.get("title") or f"{month_label} Token 使用分析"
    summary = analysis.get("summary") or analysis.get("body") or f"这里汇总 {month_label}的 Token 消耗特征和可执行建议。"
    tags = [text(item) for item in as_list(analysis.get("tags")) if text(item)]

    blocks: list[str] = []
    blocks.append(
        "---\n"
        f"title: {frontmatter_text(title)}\n"
        "subtitle: Codex Token 自定义分析\n"
        "lang: zh-CN\n"
        "glossary:\n"
        "  Token: 模型处理文本时使用的计量单位\n"
        "  Thread: Codex 里的一次对话记录\n"
        "---"
    )

    actions = as_list(analysis.get("actions"))
    if actions:
        blocks.append(json_block("actions", actions))

    blocks.append(
        json_block(
            "hero",
            {
                "id": "hero",
                "span": 12,
                "kicker": "Token Analysis",
                "title": title,
                "body": summary,
                "tags": tags or ["月度分析", "Token", "Codex"],
            },
        )
    )

    metrics = [normalize_metric(item) for item in as_list(analysis.get("metrics"))]
    if not metrics:
        metrics = [
            {"label": "本月 Token", "value": view.get("tokens_display") or fmt_tokens(view.get("tokens") or 0), "note": f"{view.get('threads', 0)} 个会话"},
            {"label": "单会话均值", "value": view.get("avg_display") or fmt_tokens(view.get("avg_tokens") or 0), "note": "按会话平均"},
            {"label": "最大会话", "value": view.get("max_tokens_display") or fmt_tokens(view.get("max_tokens") or 0), "note": "单个会话峰值"},
            {"label": "子 Agent 占比", "value": fmt_pct(float(view.get("subagent_share") or 0)), "note": view.get("subagent_tokens_display") or "0"},
        ]
    blocks.append(json_block("metrics", {"span": 12, "items": metrics[:6]}))

    findings = finding_rows(as_list(analysis.get("findings") or analysis.get("insights")))
    if findings:
        blocks.append(
            json_block(
                "matrix",
                {
                    "id": "analysis-findings",
                    "span": 12,
                    "title": analysis.get("findings_title") or "关键发现和建议",
                    "search": True,
                    "sortable": True,
                    "columns": [
                        {"key": "priority", "label": "级别", "width": "92px"},
                        {"key": "finding", "label": "发现", "minWidth": "220px"},
                        {"key": "evidence", "label": "依据", "minWidth": "260px"},
                        {"key": "recommendation", "label": "建议", "minWidth": "260px"},
                    ],
                    "rows": findings,
                },
            )
        )

    for index, section in enumerate(as_list(analysis.get("sections")), start=1):
        if isinstance(section, dict):
            blocks.append(json_block("matrix", section_to_matrix(section, index)))

    for index, table in enumerate(as_list(analysis.get("tables")), start=1):
        if isinstance(table, dict):
            blocks.append(json_block("matrix", normalize_table(table, index)))

    for block in as_list(analysis.get("blocks") or analysis.get("html_doc_blocks")):
        if isinstance(block, dict):
            block_type = block.pop("type", None) or block.pop("component", None)
            if block_type:
                blocks.append(json_block(str(block_type), block))

    if len(blocks) <= 3:
        top_day = (view.get("top_day") or {}).get("day")
        blocks.append(
            json_block(
                "matrix",
                {
                    "id": "analysis-context",
                    "span": 12,
                    "title": f"{month_label}基础上下文",
                    "columns": [
                        {"key": "item", "label": "项目", "minWidth": "160px"},
                        {"key": "value", "label": "数值", "minWidth": "180px"},
                    ],
                    "rows": [
                        {"item": "高峰日期", "value": format_day_label(top_day) if top_day else "暂无"},
                        {"item": "未归档占比", "value": fmt_pct(float(view.get("active_share") or 0))},
                    ],
                },
            )
        )

    return "\n\n".join(blocks) + "\n"


def main() -> int:
    args = parse_args()
    if args.init_script:
        write_template(Path(args.init_script).expanduser().resolve())
        return 0
    if not args.report_json or not args.analysis_script:
        raise SystemExit("需要同时传入 --report-json 和 --analysis-script，或使用 --init-script 生成模板。")

    report_path = Path(args.report_json).expanduser().resolve()
    script_path = Path(args.analysis_script).expanduser().resolve()
    out_path = Path(args.out).expanduser().resolve()
    md_path = Path(args.md_out).expanduser().resolve() if args.md_out else out_path.with_suffix(".md")

    report = load_json(report_path)
    analyze = load_analysis(script_path)
    try:
        analysis = analyze(report)
    except Exception as exc:
        raise SystemExit(f"analyze(report) 执行失败：\n{exc}") from exc
    if not isinstance(analysis, dict):
        raise SystemExit("analyze(report) 必须返回 dict。")

    markdown = analysis_markdown(report, analysis, args.title)
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text(markdown, encoding="utf-8")
    html_doc_dir = resolve_html_doc_dir(args.html_doc_dir)
    render_html_doc(md_path, out_path, html_doc_dir)

    print(f"分析 HTML: {out_path}")
    print(f"分析 Markdown: {md_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
