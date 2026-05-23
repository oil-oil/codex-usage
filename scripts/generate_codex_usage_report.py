#!/usr/bin/env python3
"""生成自包含的 Codex token 使用 HTML 报告。"""

from __future__ import annotations

import argparse
import base64
import calendar
import html
import json
import math
import os
import sqlite3
import subprocess
import sys
import tempfile
from collections import defaultdict
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPT_DIR.parent
DEFAULT_TEMPLATE = SKILL_DIR / "assets" / "report-template.html"
STATE_DB_FILENAME = "state_5.sqlite"

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python < 3.11 fallback.
    tomllib = None  # type: ignore[assignment]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="从 state_*.sqlite 生成可交互的 Codex token 使用报告。",
    )
    parser.add_argument("--db", help="Codex state SQLite 数据库路径。")
    parser.add_argument(
        "--codex-home",
        help="Codex home 目录。未传入 --db 时用于定位 state_5.sqlite。",
    )
    parser.add_argument(
        "--out",
        default="codex-usage-report.html",
        help="HTML 输出路径，默认 ./codex-usage-report.html。",
    )
    parser.add_argument("--json-out", help="可选 JSON 汇总输出路径。")
    parser.add_argument(
        "--snapshot",
        help="可选 SQLite 快照路径，默认 <out>.snapshot.sqlite。",
    )
    parser.add_argument(
        "--no-snapshot",
        action="store_true",
        help="直接查询源数据库，不先创建快照。",
    )
    parser.add_argument(
        "--since",
        help="只包含本机日期不早于该日期的会话，格式 YYYY-MM-DD。",
    )
    parser.add_argument(
        "--until",
        help="只包含本机日期不晚于该日期的会话，格式 YYYY-MM-DD。",
    )
    parser.add_argument(
        "--top",
        type=int,
        default=25,
        help="纳入报告的高消耗会话数量，默认 25。",
    )
    parser.add_argument(
        "--title",
        default="Codex Token 使用报告",
        help="报告标题。",
    )
    parser.add_argument(
        "--template",
        default=str(DEFAULT_TEMPLATE),
        help="standalone 渲染模式使用的 HTML 模板路径。",
    )
    parser.add_argument(
        "--renderer",
        choices=["html-doc", "standalone", "md", "data"],
        default="html-doc",
        help="报告渲染方式，默认使用 html-doc；data 只输出 JSON。",
    )
    parser.add_argument(
        "--html-doc-dir",
        help="html-doc skill 目录。默认尝试 $HTML_DOC_SKILL_DIR、~/.agents/skills/html-doc、~/.codex/skills/html-doc。",
    )
    parser.add_argument(
        "--md-out",
        help="可选 html-doc Markdown 输出路径。默认使用 <out>.md。",
    )
    return parser.parse_args()


def resolve_db_path(args: argparse.Namespace) -> Path:
    candidates: list[Path] = []
    if args.db:
        candidates.append(Path(args.db).expanduser())
    env_db = os.environ.get("CODEX_USAGE_DB")
    if env_db:
        candidates.append(Path(env_db).expanduser())
    sqlite_home = os.environ.get("CODEX_SQLITE_HOME")
    if sqlite_home:
        candidates.append(Path(sqlite_home).expanduser() / STATE_DB_FILENAME)
    codex_home = args.codex_home or os.environ.get("CODEX_HOME")
    if codex_home:
        codex_home_path = Path(codex_home).expanduser()
        config_sqlite_home = sqlite_home_from_config(codex_home_path / "config.toml")
        if config_sqlite_home:
            candidates.append(config_sqlite_home / STATE_DB_FILENAME)
        candidates.append(codex_home_path / STATE_DB_FILENAME)
    default_codex_home = Path.home() / ".codex"
    config_sqlite_home = sqlite_home_from_config(default_codex_home / "config.toml")
    if config_sqlite_home:
        candidates.append(config_sqlite_home / STATE_DB_FILENAME)
    candidates.append(default_codex_home / STATE_DB_FILENAME)

    for candidate in candidates:
        if candidate.exists():
            return candidate.resolve()

    searched = "\n".join(f"  - {candidate}" for candidate in candidates)
    raise SystemExit(f"没有找到 Codex state 数据库。已检查：\n{searched}")


def sqlite_home_from_config(config_path: Path) -> Path | None:
    if not config_path.exists():
        return None
    value: str | None = None
    if tomllib is not None:
        try:
            data = tomllib.loads(config_path.read_text(encoding="utf-8"))
        except Exception:
            data = {}
        raw = data.get("sqlite_home")
        if isinstance(raw, str):
            value = raw
    if value is None:
        for line in config_path.read_text(encoding="utf-8", errors="ignore").splitlines():
            stripped = line.strip()
            if stripped.startswith("sqlite_home"):
                _, _, raw_value = stripped.partition("=")
                value = raw_value.strip().strip("'\"")
                break
    if not value:
        return None
    return Path(os.path.expandvars(value)).expanduser()


def local_epoch_for_date(value: str, end_of_day: bool = False) -> int:
    parsed = datetime.strptime(value, "%Y-%m-%d").date()
    local_tz = datetime.now().astimezone().tzinfo
    local_time = time.max if end_of_day else time.min
    return int(datetime.combine(parsed, local_time, tzinfo=local_tz).timestamp())


def connect_readonly(path: Path) -> sqlite3.Connection:
    uri = f"file:{path.as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def snapshot_database(source: Path, destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        destination.unlink()
    src = connect_readonly(source)
    try:
        dst = sqlite3.connect(destination)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    return destination.resolve()


def table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {str(row["name"]) for row in rows}


def sql_literal(value: Any) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, (int, float)):
        return str(value)
    return "'" + str(value).replace("'", "''") + "'"


def column_expr(columns: set[str], name: str, default: Any) -> str:
    if name in columns:
        return name
    return f"{sql_literal(default)} AS {name}"


def fetch_threads(
    db_path: Path,
    since_epoch: int | None,
    until_epoch: int | None,
) -> tuple[list[dict[str, Any]], set[str]]:
    conn = connect_readonly(db_path)
    try:
        columns = table_columns(conn, "threads")
        required = {"created_at", "tokens_used"}
        missing = sorted(required - columns)
        if missing:
            missing_list = ", ".join(missing)
            raise SystemExit(f"`threads` 表缺少必需字段：{missing_list}")

        fields = [
            column_expr(columns, "id", ""),
            column_expr(columns, "created_at", 0),
            column_expr(columns, "updated_at", 0),
            column_expr(columns, "tokens_used", 0),
            column_expr(columns, "archived", 0),
            column_expr(columns, "model", ""),
            column_expr(columns, "model_provider", ""),
            column_expr(columns, "source", ""),
            column_expr(columns, "title", ""),
            column_expr(columns, "first_user_message", ""),
            column_expr(columns, "preview", ""),
            column_expr(columns, "rollout_path", ""),
            column_expr(columns, "cwd", ""),
        ]
        where: list[str] = []
        params: list[Any] = []
        if since_epoch is not None:
            where.append("created_at >= ?")
            params.append(since_epoch)
        if until_epoch is not None:
            where.append("created_at <= ?")
            params.append(until_epoch)
        where_sql = f" WHERE {' AND '.join(where)}" if where else ""
        query = f"SELECT {', '.join(fields)} FROM threads{where_sql} ORDER BY created_at ASC"
        rows = [dict(row) for row in conn.execute(query, params).fetchall()]
        return rows, columns
    finally:
        conn.close()


def fmt_int(value: int | float) -> str:
    return f"{int(round(value)):,}"


def fmt_tokens(value: int | float) -> str:
    number = float(value or 0)
    sign = "-" if number < 0 else ""
    number = abs(number)
    if number >= 100_000_000:
        return f"{sign}{fmt_decimal(number / 100_000_000)}亿"
    if number >= 10_000:
        return f"{sign}{fmt_decimal(number / 10_000)}万"
    return f"{sign}{int(round(number))}"


def fmt_pct(value: float) -> str:
    return f"{value:.2f}%"


def fmt_decimal(value: int | float, digits: int = 2) -> str:
    text = f"{float(value):.{digits}f}".rstrip("0").rstrip(".")
    return text or "0"


def format_month_label(value: str) -> str:
    try:
        parsed = datetime.strptime(value, "%Y-%m")
    except (TypeError, ValueError):
        return value or "当前月份"
    return f"{parsed.year} 年 {parsed.month} 月"


def format_day_label(value: str) -> str:
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d")
    except (TypeError, ValueError):
        return value or "暂无日期"
    return f"{parsed.month} 月 {parsed.day} 日"


def dt_from_epoch(value: Any) -> datetime | None:
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        return None
    if seconds <= 0:
        return None
    return datetime.fromtimestamp(seconds).astimezone()


def source_group(raw_source: Any) -> str:
    source = str(raw_source or "").strip()
    if not source:
        return "未知来源"
    if source.startswith("{"):
        return "子 Agent"
    return source


def model_name(raw_model: Any) -> str:
    model = str(raw_model or "").strip()
    return model if model else "未记录模型"


def provider_name(raw_provider: Any) -> str:
    provider = str(raw_provider or "").strip()
    return provider if provider else "未知 provider"


def title_for(row: dict[str, Any]) -> str:
    for key in ("title", "first_user_message", "preview", "id"):
        value = str(row.get(key) or "").strip()
        if value:
            return " ".join(value.split())
    return "未命名会话"


def pct(part: int | float, total: int | float) -> float:
    if not total:
        return 0.0
    return round(float(part) * 100.0 / float(total), 2)


def aggregate(rows: list[dict[str, Any]], key_fn) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = key_fn(row)
        if key not in grouped:
            grouped[key] = {"key": key, "threads": 0, "tokens": 0}
        grouped[key]["threads"] += 1
        grouped[key]["tokens"] += int(row["tokens_used"] or 0)
    return list(grouped.values())


def enrich_group_rows(items: list[dict[str, Any]], total_tokens: int) -> list[dict[str, Any]]:
    enriched: list[dict[str, Any]] = []
    for item in items:
        threads = int(item["threads"])
        tokens = int(item["tokens"])
        enriched.append(
            {
                **item,
                "tokens_display": fmt_tokens(tokens),
                "tokens_full": fmt_int(tokens),
                "share_pct": pct(tokens, total_tokens),
                "share_display": fmt_pct(pct(tokens, total_tokens)),
                "avg_tokens": round(tokens / threads) if threads else 0,
                "avg_display": fmt_tokens(tokens / threads if threads else 0),
            }
        )
    return enriched


def build_report(
    rows: list[dict[str, Any]],
    *,
    db_path: Path,
    source_db_path: Path,
    snapshot_path: Path | None,
    args: argparse.Namespace,
    available_columns: set[str],
) -> dict[str, Any]:
    normalized: list[dict[str, Any]] = []
    for row in rows:
        created = dt_from_epoch(row.get("created_at"))
        updated = dt_from_epoch(row.get("updated_at"))
        tokens = int(row.get("tokens_used") or 0)
        archived = int(row.get("archived") or 0)
        normalized.append(
            {
                **row,
                "tokens_used": tokens,
                "archived": archived,
                "archived_label": "已归档" if archived else "未归档",
                "source_group": source_group(row.get("source")),
                "model_name": model_name(row.get("model")),
                "provider_name": provider_name(row.get("model_provider")),
                "thread_title": title_for(row),
                "created_dt": created,
                "updated_dt": updated,
                "created_display": created.strftime("%Y-%m-%d %H:%M:%S") if created else "",
                "updated_display": updated.strftime("%Y-%m-%d %H:%M:%S") if updated else "",
                "month": created.strftime("%Y-%m") if created else "未知月份",
                "day": created.strftime("%Y-%m-%d") if created else "未知日期",
            }
        )

    total_tokens = sum(row["tokens_used"] for row in normalized)
    thread_count = len(normalized)
    avg_tokens = round(total_tokens / thread_count) if thread_count else 0
    max_tokens = max((row["tokens_used"] for row in normalized), default=0)
    zero_threads = sum(1 for row in normalized if row["tokens_used"] == 0)
    active_tokens = sum(row["tokens_used"] for row in normalized if not row["archived"])
    archived_tokens = total_tokens - active_tokens
    subagent_tokens = sum(row["tokens_used"] for row in normalized if row["source_group"] == "子 Agent")
    active_threads = sum(1 for row in normalized if not row["archived"])
    archived_threads = thread_count - active_threads
    first_created = min((row["created_dt"] for row in normalized if row["created_dt"]), default=None)
    last_created = max((row["created_dt"] for row in normalized if row["created_dt"]), default=None)

    monthly = enrich_group_rows(aggregate(normalized, lambda row: row["month"]), total_tokens)
    monthly.sort(key=lambda item: item["key"])
    for item in monthly:
        item["month"] = item.pop("key")

    daily = enrich_group_rows(aggregate(normalized, lambda row: row["day"]), total_tokens)
    for item in daily:
        item["day"] = item.pop("key")
    daily_all = sorted(daily, key=lambda item: item["day"])
    daily_top = sorted(daily, key=lambda item: item["tokens"], reverse=True)
    daily_by_day = {item["day"]: item for item in daily_all}

    sources = enrich_group_rows(aggregate(normalized, lambda row: row["source_group"]), total_tokens)
    sources.sort(key=lambda item: item["tokens"], reverse=True)
    for item in sources:
        item["source"] = item.pop("key")

    model_groups: dict[tuple[str, str], dict[str, Any]] = {}
    for row in normalized:
        key = (row["model_name"], row["provider_name"])
        if key not in model_groups:
            model_groups[key] = {
                "model": row["model_name"],
                "provider": row["provider_name"],
                "threads": 0,
                "tokens": 0,
            }
        model_groups[key]["threads"] += 1
        model_groups[key]["tokens"] += row["tokens_used"]
    models = enrich_group_rows(list(model_groups.values()), total_tokens)
    models.sort(key=lambda item: item["tokens"], reverse=True)

    top_limit = max(args.top, 1)

    def session_summary(row: dict[str, Any], index: int) -> dict[str, Any]:
        return {
            "rank": index,
            "id": row.get("id") or "",
            "tokens": row["tokens_used"],
            "tokens_display": fmt_tokens(row["tokens_used"]),
            "tokens_full": fmt_int(row["tokens_used"]),
            "created": row["created_display"],
            "updated": row["updated_display"],
            "model": row["model_name"],
            "provider": row["provider_name"],
            "source": row["source_group"],
            "archived": row["archived_label"],
            "title": row["thread_title"],
            "cwd": row.get("cwd") or "",
            "rollout_path": row.get("rollout_path") or "",
        }

    month_views: list[dict[str, Any]] = []
    for month_item in monthly:
        month = month_item["month"]
        month_tokens = int(month_item["tokens"])
        month_threads = int(month_item["threads"])
        month_rows = [row for row in normalized if row["month"] == month]
        month_active_tokens = sum(row["tokens_used"] for row in month_rows if not row["archived"])
        month_archived_tokens = month_tokens - month_active_tokens
        month_subagent_tokens = sum(row["tokens_used"] for row in month_rows if row["source_group"] == "子 Agent")
        month_active_threads = sum(1 for row in month_rows if not row["archived"])
        month_archived_threads = month_threads - month_active_threads
        month_zero_threads = sum(1 for row in month_rows if row["tokens_used"] == 0)
        month_max_tokens = max((row["tokens_used"] for row in month_rows), default=0)
        month_top_sessions = sorted(month_rows, key=lambda row: row["tokens_used"], reverse=True)[:top_limit]
        month_top_session_rows = [session_summary(row, index) for index, row in enumerate(month_top_sessions, start=1)]

        month_sources = enrich_group_rows(aggregate(month_rows, lambda row: row["source_group"]), month_tokens)
        month_sources.sort(key=lambda item: item["tokens"], reverse=True)
        for item in month_sources:
            item["source"] = item.pop("key")

        month_model_groups: dict[tuple[str, str], dict[str, Any]] = {}
        for row in month_rows:
            key = (row["model_name"], row["provider_name"])
            if key not in month_model_groups:
                month_model_groups[key] = {
                    "model": row["model_name"],
                    "provider": row["provider_name"],
                    "threads": 0,
                    "tokens": 0,
                }
            month_model_groups[key]["threads"] += 1
            month_model_groups[key]["tokens"] += row["tokens_used"]
        month_models = enrich_group_rows(list(month_model_groups.values()), month_tokens)
        month_models.sort(key=lambda item: item["tokens"], reverse=True)

        day_rows: list[dict[str, Any]] = []
        if len(month) == 7 and month[4] == "-":
            year = int(month[:4])
            month_number = int(month[5:7])
            day_count = calendar.monthrange(year, month_number)[1]
            day_keys = [f"{month}-{day:02d}" for day in range(1, day_count + 1)]
        else:
            day_keys = sorted(item["day"] for item in daily_all if str(item["day"]).startswith(month))

        for day_key in day_keys:
            existing = daily_by_day.get(day_key)
            day_tokens = int(existing["tokens"]) if existing else 0
            day_threads = int(existing["threads"]) if existing else 0
            day_avg = round(day_tokens / day_threads) if day_threads else 0
            day_rows.append(
                {
                    "day": day_key,
                    "label": day_key[-2:] if len(day_key) >= 2 else day_key,
                    "threads": day_threads,
                    "tokens": day_tokens,
                    "tokens_display": fmt_tokens(day_tokens),
                    "tokens_full": fmt_int(day_tokens),
                    "avg_tokens": day_avg,
                    "avg_display": fmt_tokens(day_avg),
                    "share_pct": pct(day_tokens, month_tokens),
                    "share_display": fmt_pct(pct(day_tokens, month_tokens)),
                }
            )

        top_day = max(day_rows, key=lambda item: item["tokens"], default=None)
        month_views.append(
            {
                "month": month,
                "threads": month_threads,
                "tokens": month_tokens,
                "tokens_display": month_item["tokens_display"],
                "tokens_full": month_item["tokens_full"],
                "avg_tokens": month_item["avg_tokens"],
                "avg_display": month_item["avg_display"],
                "share_pct": month_item["share_pct"],
                "share_display": month_item["share_display"],
                "max_tokens": month_max_tokens,
                "max_tokens_display": fmt_tokens(month_max_tokens),
                "zero_threads": month_zero_threads,
                "active_threads": month_active_threads,
                "archived_threads": month_archived_threads,
                "active_tokens": month_active_tokens,
                "active_tokens_display": fmt_tokens(month_active_tokens),
                "archived_tokens": month_archived_tokens,
                "archived_tokens_display": fmt_tokens(month_archived_tokens),
                "active_share": pct(month_active_tokens, month_tokens),
                "archived_share": pct(month_archived_tokens, month_tokens),
                "subagent_tokens": month_subagent_tokens,
                "subagent_tokens_display": fmt_tokens(month_subagent_tokens),
                "subagent_share": pct(month_subagent_tokens, month_tokens),
                "days": day_rows,
                "top_day": top_day,
                "sources": month_sources,
                "models": month_models,
                "top_sessions": month_top_session_rows,
            }
        )

    current_month = datetime.now().astimezone().strftime("%Y-%m")
    month_names = {item["month"] for item in month_views}
    default_month = current_month if current_month in month_names else (month_views[-1]["month"] if month_views else "")

    top_sessions = sorted(normalized, key=lambda row: row["tokens_used"], reverse=True)[:top_limit]
    top_session_rows = [session_summary(row, index) for index, row in enumerate(top_sessions, start=1)]

    source_spark = [round(item["tokens"] / 1_000_000, 2) for item in sources[:8]]
    monthly_spark = [round(item["tokens"] / 1_000_000, 2) for item in monthly]
    avg_spark = [round(item["avg_tokens"] / 1_000_000, 2) for item in monthly]

    title = args.title
    total_query = (
        "sqlite3 -header -column "
        + json.dumps(str(source_db_path))
        + " \"select count(*) as threads, sum(tokens_used) as total_tokens from threads;\""
    )
    monthly_query = (
        "sqlite3 -header -column "
        + json.dumps(str(source_db_path))
        + " \"select strftime('%Y-%m', created_at, 'unixepoch', 'localtime') as month, "
        + "count(*) as threads, sum(tokens_used) as tokens from threads group by month order by month desc;\""
    )

    return {
        "meta": {
            "title": title,
            "generated_at": datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z"),
            "source_db_path": str(source_db_path),
            "queried_db_path": str(db_path),
            "snapshot_path": str(snapshot_path) if snapshot_path else "",
            "snapshot_enabled": bool(snapshot_path),
            "since": args.since or "",
            "until": args.until or "",
            "available_columns": sorted(available_columns),
        },
        "summary": {
            "threads": thread_count,
            "threads_display": fmt_int(thread_count),
            "total_tokens": total_tokens,
            "total_tokens_display": fmt_tokens(total_tokens),
            "total_tokens_full": fmt_int(total_tokens),
            "avg_tokens": avg_tokens,
            "avg_tokens_display": fmt_tokens(avg_tokens),
            "max_tokens": max_tokens,
            "max_tokens_display": fmt_tokens(max_tokens),
            "zero_threads": zero_threads,
            "active_threads": active_threads,
            "archived_threads": archived_threads,
            "active_tokens": active_tokens,
            "active_tokens_display": fmt_tokens(active_tokens),
            "archived_tokens": archived_tokens,
            "archived_tokens_display": fmt_tokens(archived_tokens),
            "active_share": pct(active_tokens, total_tokens),
            "archived_share": pct(archived_tokens, total_tokens),
            "subagent_tokens": subagent_tokens,
            "subagent_tokens_display": fmt_tokens(subagent_tokens),
            "subagent_share": pct(subagent_tokens, total_tokens),
            "first_thread": first_created.strftime("%Y-%m-%d %H:%M:%S") if first_created else "",
            "last_thread": last_created.strftime("%Y-%m-%d %H:%M:%S") if last_created else "",
            "monthly_spark": monthly_spark,
            "avg_spark": avg_spark,
            "source_spark": source_spark,
        },
        "monthly": monthly,
        "daily": daily_all,
        "daily_top": daily_top[:10],
        "month_views": month_views,
        "default_month": default_month,
        "sources": sources,
        "models": models,
        "top_sessions": top_session_rows,
        "queries": {
            "total": total_query,
            "monthly": monthly_query,
        },
    }


def render_report(report: dict[str, Any], template_path: Path, out_path: Path) -> None:
    if not template_path.exists():
        raise SystemExit(f"没有找到 HTML 模板：{template_path}")
    template = template_path.read_text(encoding="utf-8")
    report_json = json.dumps(report, ensure_ascii=False).replace("</", "<\\/")
    title = html.escape(str(report["meta"]["title"]))
    html_out = template.replace("__REPORT_TITLE__", title).replace("__REPORT_JSON__", report_json)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html_out, encoding="utf-8")


def write_json(report: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


def clip_text(value: Any, limit: int = 110) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def json_block(block_type: str, payload: Any) -> str:
    body = json.dumps(payload, ensure_ascii=False, indent=2)
    return f"~~~{block_type}\n{body}\n~~~"


def top_title(report: dict[str, Any], index: int = 0) -> str:
    sessions = report.get("top_sessions") or []
    if len(sessions) <= index:
        return "暂无会话"
    return clip_text(sessions[index].get("title"), 38)


def frontmatter_text(value: Any) -> str:
    text = " ".join(str(value or "").split())
    return text.replace(":", "：") or "Codex Token 使用报告"


CHART_COLORS = [
    "#C85F43",
    "#4E7C88",
    "#D19A45",
    "#6D8A57",
    "#7A6EA8",
    "#C06B7B",
    "#3F6C9B",
]


def svg_number(value: int | float) -> str:
    return fmt_decimal(float(value), 3)


def esc_text(value: Any) -> str:
    return html.escape(str(value), quote=True)


def smooth_svg_path(points: list[tuple[float, float]]) -> str:
    if not points:
        return ""
    if len(points) == 1:
        x, y = points[0]
        return f"M {svg_number(x)} {svg_number(y)}"

    commands = [f"M {svg_number(points[0][0])} {svg_number(points[0][1])}"]
    for index in range(len(points) - 1):
        p0 = points[index - 1] if index > 0 else points[index]
        p1 = points[index]
        p2 = points[index + 1]
        p3 = points[index + 2] if index + 2 < len(points) else p2
        c1x = p1[0] + (p2[0] - p0[0]) / 6
        c1y = p1[1] + (p2[1] - p0[1]) / 6
        c2x = p2[0] - (p3[0] - p1[0]) / 6
        c2y = p2[1] - (p3[1] - p1[1]) / 6
        commands.append(
            "C "
            f"{svg_number(c1x)} {svg_number(c1y)}, "
            f"{svg_number(c2x)} {svg_number(c2y)}, "
            f"{svg_number(p2[0])} {svg_number(p2[1])}"
        )
    return " ".join(commands)


def line_chart_svg(monthly: list[dict[str, Any]]) -> str:
    rows = [item for item in monthly if int(item.get("tokens") or 0) > 0]
    if not rows:
        return '<svg class="usage-chart-svg" viewBox="0 0 960 260" role="img" aria-label="暂无月度数据"></svg>'

    width = 960
    height = 330
    left = 72
    right = 28
    top = 34
    bottom = 62
    inner_width = width - left - right
    inner_height = height - top - bottom
    base_y = height - bottom
    max_tokens = max(int(item["tokens"]) for item in rows)
    y_max = max_tokens * 1.08 if max_tokens else 1
    step_x = inner_width / max(len(rows) - 1, 1)

    points: list[tuple[float, float]] = []
    for index, item in enumerate(rows):
        x = left + (step_x * index if len(rows) > 1 else inner_width / 2)
        y = base_y - (int(item["tokens"]) / y_max) * inner_height
        points.append((x, y))

    line_path = smooth_svg_path(points)
    area_path = f"{line_path} L {svg_number(points[-1][0])} {svg_number(base_y)} L {svg_number(points[0][0])} {svg_number(base_y)} Z"
    label_step = max(1, math.ceil(len(rows) / 8))
    top_index = max(range(len(rows)), key=lambda idx: int(rows[idx]["tokens"]))

    grid = []
    for tick in range(5):
        ratio = tick / 4
        y = base_y - ratio * inner_height
        value = y_max * ratio
        grid.append(
            f'<line x1="{left}" y1="{svg_number(y)}" x2="{width - right}" y2="{svg_number(y)}" class="grid-line" />'
            f'<text x="{left - 12}" y="{svg_number(y + 4)}" class="axis-label" text-anchor="end">{esc_text(fmt_tokens(value))}</text>'
        )

    x_labels = []
    for index, item in enumerate(rows):
        if index % label_step == 0 or index == len(rows) - 1:
            x, _ = points[index]
            x_labels.append(
                f'<text x="{svg_number(x)}" y="{height - 24}" class="axis-label" text-anchor="middle">{esc_text(item["month"])}</text>'
            )

    markers = []
    for index, item in enumerate(rows):
        if index % label_step == 0 or index == top_index or index == len(rows) - 1:
            x, y = points[index]
            markers.append(
                f'<g><circle cx="{svg_number(x)}" cy="{svg_number(y)}" r="4.5" class="point" />'
                f'<title>{esc_text(item["month"])}：{esc_text(item["tokens_display"])}</title></g>'
            )

    top_x, top_y = points[top_index]
    top_row = rows[top_index]
    top_callout_y = max(top + 18, top_y - 34)
    return f'''
<svg class="usage-chart-svg" viewBox="0 0 {width} {height}" role="img" aria-label="月度 Token 使用曲线">
  <defs>
    <linearGradient id="usage-area-fill" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0%" stop-color="#C85F43" stop-opacity="0.24"/>
      <stop offset="100%" stop-color="#C85F43" stop-opacity="0.02"/>
    </linearGradient>
  </defs>
  {''.join(grid)}
  <line x1="{left}" y1="{base_y}" x2="{width - right}" y2="{base_y}" class="axis-line" />
  <path d="{esc_text(area_path)}" class="area-path" />
  <path d="{esc_text(line_path)}" class="line-path" />
  {''.join(markers)}
  <g class="callout">
    <line x1="{svg_number(top_x)}" y1="{svg_number(top_y)}" x2="{svg_number(top_x)}" y2="{svg_number(top_callout_y + 10)}" />
    <rect x="{svg_number(min(max(left, top_x - 74), width - right - 148))}" y="{svg_number(top_callout_y - 18)}" width="148" height="34" rx="8" />
    <text x="{svg_number(min(max(left, top_x - 74), width - right - 148) + 12)}" y="{svg_number(top_callout_y + 4)}">{esc_text(top_row["month"])} · {esc_text(top_row["tokens_display"])}</text>
  </g>
  {''.join(x_labels)}
</svg>'''


def polar_point(cx: float, cy: float, radius: float, angle: float) -> tuple[float, float]:
    radians = math.radians(angle - 90)
    return cx + radius * math.cos(radians), cy + radius * math.sin(radians)


def donut_segment_path(
    cx: float,
    cy: float,
    outer_radius: float,
    inner_radius: float,
    start_angle: float,
    end_angle: float,
) -> str:
    if end_angle - start_angle >= 359.99:
        end_angle = start_angle + 359.99
    outer_start = polar_point(cx, cy, outer_radius, start_angle)
    outer_end = polar_point(cx, cy, outer_radius, end_angle)
    inner_end = polar_point(cx, cy, inner_radius, end_angle)
    inner_start = polar_point(cx, cy, inner_radius, start_angle)
    large_arc = 1 if end_angle - start_angle > 180 else 0
    return (
        f"M {svg_number(outer_start[0])} {svg_number(outer_start[1])} "
        f"A {outer_radius} {outer_radius} 0 {large_arc} 1 {svg_number(outer_end[0])} {svg_number(outer_end[1])} "
        f"L {svg_number(inner_end[0])} {svg_number(inner_end[1])} "
        f"A {inner_radius} {inner_radius} 0 {large_arc} 0 {svg_number(inner_start[0])} {svg_number(inner_start[1])} Z"
    )


def chart_items(rows: list[dict[str, Any]], limit: int = 6) -> tuple[list[dict[str, Any]], int]:
    total = sum(int(item.get("tokens") or 0) for item in rows)
    items = [
        {
            "label": str(item.get("label") or item.get("source") or item.get("model") or "未命名"),
            "tokens": int(item.get("tokens") or 0),
        }
        for item in rows
        if int(item.get("tokens") or 0) > 0
    ]
    items.sort(key=lambda item: item["tokens"], reverse=True)
    if len(items) > limit:
        keep = items[: limit - 1]
        other_tokens = sum(item["tokens"] for item in items[limit - 1 :])
        if other_tokens:
            keep.append({"label": "其他", "tokens": other_tokens})
        items = keep
    for item in items:
        item["display"] = fmt_tokens(item["tokens"])
        item["share"] = pct(item["tokens"], total)
    return items, total


def donut_chart_svg(items: list[dict[str, Any]], total: int, aria_label: str) -> str:
    width = 260
    height = 240
    cx = 120
    cy = 120
    outer = 88
    inner = 52
    start = 0.0
    segments = []
    labels = []
    if total <= 0 or not items:
        segments.append(f'<circle cx="{cx}" cy="{cy}" r="{(outer + inner) / 2}" class="empty-ring" />')
    for index, item in enumerate(items):
        angle = (item["tokens"] / total) * 360 if total else 0
        end = start + angle
        color = CHART_COLORS[index % len(CHART_COLORS)]
        path = donut_segment_path(cx, cy, outer, inner, start, end)
        segments.append(f'<path d="{esc_text(path)}" fill="{color}"><title>{esc_text(item["label"])}：{esc_text(item["display"])}，{item["share"]}%</title></path>')
        if item["share"] >= 7:
            mid = start + angle / 2
            lx, ly = polar_point(cx, cy, (outer + inner) / 2, mid)
            labels.append(f'<text x="{svg_number(lx)}" y="{svg_number(ly + 4)}" class="slice-label" text-anchor="middle">{fmt_decimal(item["share"], 0)}%</text>')
        start = end

    legend = []
    for index, item in enumerate(items):
        color = CHART_COLORS[index % len(CHART_COLORS)]
        legend.append(
            f'<div class="legend-row"><span class="legend-dot" style="background:{color}"></span>'
            f'<span class="legend-label">{esc_text(clip_text(item["label"], 24))}</span>'
            f'<strong>{esc_text(item["display"])}</strong><em>{fmt_decimal(item["share"])}%</em></div>'
        )

    return f'''
<div class="donut-layout">
  <svg class="usage-chart-svg donut-svg" viewBox="0 0 {width} {height}" role="img" aria-label="{esc_text(aria_label)}">
    {''.join(segments)}
    {''.join(labels)}
    <text x="{cx}" y="{cy - 8}" class="donut-total" text-anchor="middle">{esc_text(fmt_tokens(total))}</text>
    <text x="{cx}" y="{cy + 18}" class="donut-caption" text-anchor="middle">合计</text>
  </svg>
  <div class="legend-list">{''.join(legend)}</div>
</div>'''


def bar_chart_svg(rows: list[dict[str, Any]]) -> str:
    bars = [item for item in rows[:10] if int(item.get("tokens") or 0) > 0]
    width = 960
    row_height = 34
    height = max(178, 54 + row_height * max(len(bars), 1))
    label_width = 122
    right = 118
    max_tokens = max((int(item["tokens"]) for item in bars), default=1)
    body = []
    if not bars:
        body.append('<text x="32" y="88" class="axis-label">暂无峰值日期数据</text>')
    for index, item in enumerate(bars):
        y = 36 + index * row_height
        bar_width = max(6, (int(item["tokens"]) / max_tokens) * (width - label_width - right - 36))
        color = CHART_COLORS[index % len(CHART_COLORS)]
        body.append(
            f'<text x="18" y="{y + 18}" class="bar-label">{esc_text(item["day"])}</text>'
            f'<rect x="{label_width}" y="{y + 3}" width="{svg_number(bar_width)}" height="18" rx="5" fill="{color}" opacity="0.88">'
            f'<title>{esc_text(item["day"])}：{esc_text(item["tokens_display"])}</title></rect>'
            f'<text x="{svg_number(label_width + bar_width + 10)}" y="{y + 18}" class="bar-value">{esc_text(item["tokens_display"])}</text>'
        )
    return f'''
<svg class="usage-chart-svg" viewBox="0 0 {width} {height}" role="img" aria-label="峰值日期柱形图">
  {''.join(body)}
</svg>'''


def chart_report_html(report: dict[str, Any]) -> str:
    payload_json = json.dumps(
        {
            "months": report.get("month_views") or [],
            "defaultMonth": report.get("default_month") or "",
        },
        ensure_ascii=False,
    ).replace("</", "<\\/")
    template = '''<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Codex Usage Charts</title>
  <style>
    :root {
      color-scheme: light;
      --paper: #F7F1E7;
      --surface: #FFFDF8;
      --surface-soft: #F4EBDF;
      --line: #DFD0BD;
      --ink: #1F2529;
      --muted: #69727C;
      --accent: #C85F43;
      --blue: #4E7C88;
      --green: #6D8A57;
      --gold: #D19A45;
      font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", sans-serif;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: var(--paper);
      color: var(--ink);
    }
    .usage-chart-shell {
      width: min(100%, 1180px);
      margin: 0 auto;
      padding: 22px;
      display: grid;
      grid-template-columns: repeat(12, minmax(0, 1fr));
      gap: 16px;
    }
    .usage-chart-panel {
      grid-column: span 6;
      min-width: 0;
      border: 0;
      border-radius: 0;
      background: transparent;
      padding: 10px 0;
      box-shadow: none;
    }
    .usage-chart-panel.wide { grid-column: span 12; }
    .chart-head {
      display: flex;
      align-items: end;
      justify-content: space-between;
      gap: 18px;
      margin-bottom: 14px;
    }
    .chart-kicker {
      margin-bottom: 6px;
      color: var(--accent);
      font-size: 12px;
      font-weight: 800;
      letter-spacing: 0;
    }
    h2 {
      margin: 0;
      font-size: 19px;
      line-height: 1.24;
      letter-spacing: 0;
    }
    .chart-note {
      margin: 0;
      color: var(--muted);
      font-size: 13px;
      line-height: 1.45;
      text-align: right;
    }
    .month-control {
      display: flex;
      align-items: center;
      gap: 10px;
      margin: 0 0 16px;
    }
    .month-control label {
      color: var(--muted);
      font-size: 12px;
      font-weight: 780;
    }
    .month-select {
      margin-left: 8px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: rgba(255, 253, 248, .68);
      color: var(--ink);
      cursor: pointer;
      font: inherit;
      font-size: 13px;
      font-weight: 760;
      min-height: 34px;
      min-width: 132px;
      padding: 6px 34px 6px 12px;
    }
    .month-stats {
      display: grid;
      grid-template-columns: repeat(6, minmax(0, 1fr));
      gap: 10px;
      margin-bottom: 10px;
    }
    .month-stat {
      border-top: 1px solid var(--line);
      padding-top: 10px;
    }
    .month-stat span {
      display: block;
      color: var(--muted);
      font-size: 11px;
      font-weight: 760;
      line-height: 1.2;
    }
    .month-stat strong {
      display: block;
      margin-top: 5px;
      color: var(--ink);
      font-size: 18px;
      line-height: 1.1;
      font-weight: 820;
    }
    .usage-chart-svg {
      display: block;
      width: 100%;
      height: auto;
    }
    .chart-tooltip {
      position: fixed;
      z-index: 10;
      min-width: 184px;
      max-width: 240px;
      pointer-events: none;
      border: 1px solid #D7C8B8;
      border-radius: 8px;
      background: rgba(255, 253, 248, .97);
      box-shadow: 0 14px 36px rgba(45, 36, 26, .14);
      padding: 10px 12px;
      color: var(--ink);
      font-size: 12px;
      line-height: 1.45;
    }
    .chart-tooltip strong {
      display: block;
      margin-bottom: 4px;
      font-size: 13px;
    }
    .chart-tooltip span {
      display: flex;
      justify-content: space-between;
      gap: 14px;
      color: var(--muted);
    }
    .chart-tooltip b {
      color: var(--ink);
    }
    .hit-point {
      fill: transparent;
      pointer-events: all;
      cursor: crosshair;
    }
    .grid-line { stroke: #E6D9CA; stroke-width: 1; }
    .axis-line { stroke: #BDAF9D; stroke-width: 1.2; }
    .axis-label, .bar-label, .bar-value {
      fill: var(--muted);
      font-size: 12px;
    }
    .bar-value { fill: var(--ink); font-weight: 700; }
    .area-path { fill: url(#usage-area-fill); }
    .line-path {
      fill: none;
      stroke: var(--accent);
      stroke-width: 3;
      stroke-linecap: round;
      stroke-linejoin: round;
    }
    .point {
      fill: #FFFDF8;
      stroke: var(--accent);
      stroke-width: 2.5;
    }
    .callout line { stroke: var(--accent); stroke-width: 1.2; stroke-dasharray: 3 4; }
    .callout rect { fill: #FFF4EE; stroke: #E3B09E; }
    .callout text { fill: var(--ink); font-size: 12px; font-weight: 760; }
    .donut-layout {
      display: grid;
      grid-template-columns: minmax(210px, 260px) minmax(0, 1fr);
      gap: 18px;
      align-items: center;
    }
    .donut-svg {
      width: min(260px, 100%);
      min-height: 220px;
      justify-self: center;
    }
    .empty-ring {
      fill: none;
      stroke: #E6D9CA;
      stroke-width: 38;
    }
    .slice-label {
      fill: #fff;
      font-size: 11px;
      font-weight: 800;
      paint-order: stroke;
      stroke: rgba(20, 20, 20, .22);
      stroke-width: 2px;
    }
    .donut-total {
      fill: var(--ink);
      font-size: 22px;
      font-weight: 820;
    }
    .donut-caption {
      fill: var(--muted);
      font-size: 12px;
      font-weight: 700;
    }
    .legend-list {
      display: grid;
      gap: 8px;
      min-width: 0;
    }
    .legend-row {
      display: grid;
      grid-template-columns: 10px minmax(0, 1fr) auto auto;
      gap: 8px;
      align-items: center;
      min-height: 26px;
      font-size: 12px;
      color: var(--muted);
    }
    .legend-dot {
      width: 9px;
      height: 9px;
      border-radius: 999px;
    }
    .legend-label {
      min-width: 0;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
      color: var(--ink);
    }
    .legend-row strong {
      color: var(--ink);
      font-size: 12px;
    }
    .legend-row em {
      color: var(--muted);
      font-style: normal;
      font-variant-numeric: tabular-nums;
    }
    @media (max-width: 760px) {
      .usage-chart-shell { padding: 14px; }
      .usage-chart-panel { grid-column: span 12; padding: 14px 0; }
      .chart-head { display: block; }
      .chart-note { margin-top: 6px; text-align: left; }
      .month-stats { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      .donut-layout { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
  <main class="usage-chart-shell">
    <section class="usage-chart-panel wide">
      <div class="chart-head">
        <div>
          <div class="chart-kicker">月度趋势</div>
          <h2 id="daily-title">每日 Token 趋势</h2>
        </div>
        <p class="chart-note" id="month-note">指向曲线上的日期点，查看当天明细。</p>
      </div>
      <div class="month-control">
        <label for="month-select">月份
          <select class="month-select" id="month-select" aria-label="选择月份"></select>
        </label>
      </div>
      <div class="month-stats">
        <div class="month-stat"><span>本月 Token</span><strong id="stat-total">0</strong></div>
        <div class="month-stat"><span>本月会话</span><strong id="stat-threads">0</strong></div>
        <div class="month-stat"><span>单会话均值</span><strong id="stat-avg">0</strong></div>
        <div class="month-stat"><span>最大会话</span><strong id="stat-max">0</strong></div>
        <div class="month-stat"><span>未归档占比</span><strong id="stat-active">0%</strong></div>
        <div class="month-stat"><span>子 Agent 占比</span><strong id="stat-subagent">0%</strong></div>
      </div>
      <svg id="daily-line" class="usage-chart-svg" viewBox="0 0 960 330" role="img" aria-label="每日 Token 曲线"></svg>
      <div id="chart-tooltip" class="chart-tooltip" hidden></div>
    </section>
    <section class="usage-chart-panel">
      <div class="chart-head">
        <div>
          <div class="chart-kicker">来源占比</div>
          <h2 id="source-title">主要入口</h2>
        </div>
        <p class="chart-note">最多展示前五项，其余合并。</p>
      </div>
      <div id="source-donut"></div>
    </section>
    <section class="usage-chart-panel">
      <div class="chart-head">
        <div>
          <div class="chart-kicker">模型占比</div>
          <h2 id="model-title">模型分布</h2>
        </div>
        <p class="chart-note">同时显示模型和提供方。</p>
      </div>
      <div id="model-donut"></div>
    </section>
    <section class="usage-chart-panel wide">
      <div class="chart-head">
        <div>
          <div class="chart-kicker">峰值日期</div>
          <h2 id="bar-title">高消耗日期</h2>
        </div>
        <p class="chart-note">按 Token 总量从高到低排列。</p>
      </div>
      <svg id="daily-bars" class="usage-chart-svg" viewBox="0 0 960 220" role="img" aria-label="高消耗日期柱形图"></svg>
    </section>
  </main>
  <script type="application/json" id="month-data">__MONTH_DATA__</script>
  <script>
    const raw = document.getElementById("month-data").textContent;
    const chartData = JSON.parse(raw);
    const months = chartData.months || [];
    const monthMap = new Map(months.map((item) => [item.month, item]));
    const colors = ["#C85F43", "#4E7C88", "#D19A45", "#6D8A57", "#7A6EA8", "#C06B7B", "#3F6C9B"];

    function escapeHtml(value) {
      return String(value ?? "").replace(/[&<>"']/g, (char) => ({
        "&": "&amp;",
        "<": "&lt;",
        ">": "&gt;",
        '"': "&quot;",
        "'": "&#39;",
      }[char]));
    }

    function trimNumber(value, digits = 2) {
      return Number(value || 0).toFixed(digits).replace(/\\.?0+$/, "");
    }

    function formatTokens(value) {
      const number = Math.abs(Number(value || 0));
      const sign = Number(value || 0) < 0 ? "-" : "";
      if (number >= 100000000) return `${sign}${trimNumber(number / 100000000)}亿`;
      if (number >= 10000) return `${sign}${trimNumber(number / 10000)}万`;
      return `${sign}${Math.round(number)}`;
    }

    function formatPct(value) {
      return `${trimNumber(value)}%`;
    }

    function formatMonthLabel(value) {
      const match = String(value || "").match(/^(\\d{4})-(\\d{2})$/);
      if (!match) return value || "当前月份";
      return `${match[1]} 年 ${Number(match[2])} 月`;
    }

    function smoothPath(points) {
      if (!points.length) return "";
      if (points.length === 1) return `M ${points[0][0]} ${points[0][1]}`;
      const parts = [`M ${points[0][0]} ${points[0][1]}`];
      for (let index = 0; index < points.length - 1; index += 1) {
        const p0 = index > 0 ? points[index - 1] : points[index];
        const p1 = points[index];
        const p2 = points[index + 1];
        const p3 = index + 2 < points.length ? points[index + 2] : p2;
        const c1x = p1[0] + (p2[0] - p0[0]) / 6;
        const c1y = p1[1] + (p2[1] - p0[1]) / 6;
        const c2x = p2[0] - (p3[0] - p1[0]) / 6;
        const c2y = p2[1] - (p3[1] - p1[1]) / 6;
        parts.push(`C ${trimNumber(c1x, 3)} ${trimNumber(c1y, 3)}, ${trimNumber(c2x, 3)} ${trimNumber(c2y, 3)}, ${trimNumber(p2[0], 3)} ${trimNumber(p2[1], 3)}`);
      }
      return parts.join(" ");
    }

    function polarPoint(cx, cy, radius, angle) {
      const radians = (angle - 90) * Math.PI / 180;
      return [cx + radius * Math.cos(radians), cy + radius * Math.sin(radians)];
    }

    function donutPath(cx, cy, outer, inner, start, end) {
      const adjustedEnd = end - start >= 359.99 ? start + 359.99 : end;
      const outerStart = polarPoint(cx, cy, outer, start);
      const outerEnd = polarPoint(cx, cy, outer, adjustedEnd);
      const innerEnd = polarPoint(cx, cy, inner, adjustedEnd);
      const innerStart = polarPoint(cx, cy, inner, start);
      const large = adjustedEnd - start > 180 ? 1 : 0;
      return `M ${trimNumber(outerStart[0], 3)} ${trimNumber(outerStart[1], 3)} A ${outer} ${outer} 0 ${large} 1 ${trimNumber(outerEnd[0], 3)} ${trimNumber(outerEnd[1], 3)} L ${trimNumber(innerEnd[0], 3)} ${trimNumber(innerEnd[1], 3)} A ${inner} ${inner} 0 ${large} 0 ${trimNumber(innerStart[0], 3)} ${trimNumber(innerStart[1], 3)} Z`;
    }

    function prepareItems(rows, labelFor, limit = 6) {
      const source = rows
        .map((row) => ({
          label: labelFor(row),
          tokens: Number(row.tokens || 0),
          display: row.tokens_display || formatTokens(row.tokens),
          share: Number(row.share_pct || 0),
        }))
        .filter((row) => row.tokens > 0)
        .sort((a, b) => b.tokens - a.tokens);
      const total = source.reduce((sum, row) => sum + row.tokens, 0);
      if (source.length > limit) {
        const keep = source.slice(0, limit - 1);
        const rest = source.slice(limit - 1).reduce((sum, row) => sum + row.tokens, 0);
        keep.push({
          label: "其他",
          tokens: rest,
          display: formatTokens(rest),
          share: total ? (rest * 100 / total) : 0,
        });
        return { items: keep, total };
      }
      return { items: source, total };
    }

    function renderDonut(rootId, rows, total) {
      const root = document.getElementById(rootId);
      const width = 260;
      const height = 240;
      const cx = 120;
      const cy = 120;
      const outer = 88;
      const inner = 52;
      let start = 0;
      let segments = "";
      let labels = "";
      if (!rows.length || total <= 0) {
        segments = `<circle cx="${cx}" cy="${cy}" r="${(outer + inner) / 2}" class="empty-ring" />`;
      }
      rows.forEach((item, index) => {
        const angle = total ? item.tokens * 360 / total : 0;
        const end = start + angle;
        const color = colors[index % colors.length];
        segments += `<path d="${donutPath(cx, cy, outer, inner, start, end)}" fill="${color}"><title>${escapeHtml(item.label)}：${escapeHtml(item.display)}，${formatPct(item.share)}</title></path>`;
        if (item.share >= 7) {
          const [x, y] = polarPoint(cx, cy, (outer + inner) / 2, start + angle / 2);
          labels += `<text x="${trimNumber(x, 3)}" y="${trimNumber(y + 4, 3)}" class="slice-label" text-anchor="middle">${trimNumber(item.share, 0)}%</text>`;
        }
        start = end;
      });
      const legend = rows.map((item, index) => `<div class="legend-row"><span class="legend-dot" style="background:${colors[index % colors.length]}"></span><span class="legend-label">${escapeHtml(item.label)}</span><strong>${escapeHtml(item.display)}</strong><em>${formatPct(item.share)}</em></div>`).join("");
      root.innerHTML = `<div class="donut-layout"><svg class="usage-chart-svg donut-svg" viewBox="0 0 ${width} ${height}" role="img" aria-label="环形饼图">${segments}${labels}<text x="${cx}" y="${cy - 8}" class="donut-total" text-anchor="middle">${formatTokens(total)}</text><text x="${cx}" y="${cy + 18}" class="donut-caption" text-anchor="middle">合计</text></svg><div class="legend-list">${legend}</div></div>`;
    }

    function bindLineHover(svg, days, points) {
      const tooltip = document.getElementById("chart-tooltip");
      const hide = () => {
        tooltip.hidden = true;
      };
      svg.querySelectorAll("[data-point-index]").forEach((node) => {
        const index = Number(node.getAttribute("data-point-index"));
        const day = days[index];
        const point = points[index];
        const show = (event) => {
          tooltip.hidden = false;
          tooltip.innerHTML = `<strong>${escapeHtml(day.day)}</strong><span>Token <b>${escapeHtml(day.tokens_display || formatTokens(day.tokens))}</b></span><span>会话数 <b>${day.threads || 0}</b></span><span>均值 <b>${escapeHtml(day.avg_display || formatTokens(day.avg_tokens))}</b></span><span>本月占比 <b>${escapeHtml(day.share_display || formatPct(day.share_pct))}</b></span>`;
          const maxLeft = Math.max(12, window.innerWidth - 260);
          tooltip.style.left = `${Math.min(maxLeft, event.clientX + 14)}px`;
          tooltip.style.top = `${Math.max(12, event.clientY - 18)}px`;
          svg.querySelectorAll(".point").forEach((pointNode) => pointNode.setAttribute("r", "4.5"));
          const marker = svg.querySelector(`[data-marker-index="${index}"]`);
          if (marker) marker.setAttribute("r", "7");
        };
        node.addEventListener("mouseenter", show);
        node.addEventListener("mousemove", show);
        node.addEventListener("mouseleave", (event) => {
          hide();
          const marker = svg.querySelector(`[data-marker-index="${index}"]`);
          if (marker) marker.setAttribute("r", "4.5");
        });
      });
      svg.addEventListener("mouseleave", hide);
    }

    function renderLine(view) {
      const svg = document.getElementById("daily-line");
      const days = view.days || [];
      const width = 960;
      const height = 330;
      const left = 72;
      const right = 28;
      const top = 34;
      const bottom = 62;
      const baseY = height - bottom;
      const innerWidth = width - left - right;
      const innerHeight = height - top - bottom;
      const maxTokens = Math.max(...days.map((day) => Number(day.tokens || 0)), 1);
      const yMax = maxTokens * 1.08;
      const stepX = days.length > 1 ? innerWidth / (days.length - 1) : innerWidth;
      const points = days.map((day, index) => {
        const x = left + (days.length > 1 ? stepX * index : innerWidth / 2);
        const y = baseY - (Number(day.tokens || 0) / yMax) * innerHeight;
        return [Number(trimNumber(x, 3)), Number(trimNumber(y, 3))];
      });
      const linePath = smoothPath(points);
      const areaPath = `${linePath} L ${points[points.length - 1]?.[0] || left} ${baseY} L ${points[0]?.[0] || left} ${baseY} Z`;
      const topIndex = Math.max(0, days.reduce((best, day, index) => Number(day.tokens || 0) > Number(days[best]?.tokens || 0) ? index : best, 0));
      const labelStep = Math.max(1, Math.ceil(days.length / 10));
      const grid = Array.from({ length: 5 }, (_, index) => {
        const ratio = index / 4;
        const y = baseY - ratio * innerHeight;
        return `<line x1="${left}" y1="${trimNumber(y, 3)}" x2="${width - right}" y2="${trimNumber(y, 3)}" class="grid-line" /><text x="${left - 12}" y="${trimNumber(y + 4, 3)}" class="axis-label" text-anchor="end">${formatTokens(yMax * ratio)}</text>`;
      }).join("");
      const labels = days.map((day, index) => {
        if (index % labelStep !== 0 && index !== days.length - 1) return "";
        return `<text x="${points[index][0]}" y="${height - 24}" class="axis-label" text-anchor="middle">${escapeHtml(day.label)}</text>`;
      }).join("");
      const markers = days.map((day, index) => {
        if (index % labelStep !== 0 && index !== topIndex && index !== days.length - 1) return "";
        return `<g><circle cx="${points[index][0]}" cy="${points[index][1]}" r="4.5" class="point" data-marker-index="${index}" /><title>${escapeHtml(day.day)}：${escapeHtml(day.tokens_display || formatTokens(day.tokens))}</title></g>`;
      }).join("");
      const hitTargets = days.map((day, index) => `<circle cx="${points[index][0]}" cy="${points[index][1]}" r="12" class="hit-point" data-point-index="${index}" />`).join("");
      const topDay = days[topIndex] || {};
      const topX = points[topIndex]?.[0] || left;
      const topY = points[topIndex]?.[1] || baseY;
      const calloutY = Math.max(top + 18, topY - 34);
      const calloutX = Math.min(Math.max(left, topX - 74), width - right - 148);
      const calloutText = Number(topDay.tokens || 0) > 0 ? `${topDay.day} · ${topDay.tokens_display}` : `${view.month} 暂无消耗`;
      svg.innerHTML = `<defs><linearGradient id="usage-area-fill" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stop-color="#C85F43" stop-opacity="0.24"/><stop offset="100%" stop-color="#C85F43" stop-opacity="0.02"/></linearGradient></defs>${grid}<line x1="${left}" y1="${baseY}" x2="${width - right}" y2="${baseY}" class="axis-line" /><path d="${areaPath}" class="area-path" /><path d="${linePath}" class="line-path" />${markers}<g class="callout"><line x1="${topX}" y1="${topY}" x2="${topX}" y2="${calloutY + 10}" /><rect x="${calloutX}" y="${calloutY - 18}" width="148" height="34" rx="8" /><text x="${calloutX + 12}" y="${calloutY + 4}">${escapeHtml(calloutText)}</text></g>${labels}${hitTargets}`;
      bindLineHover(svg, days, points);
    }

    function renderBars(view) {
      const svg = document.getElementById("daily-bars");
      const bars = [...(view.days || [])].filter((day) => Number(day.tokens || 0) > 0).sort((a, b) => Number(b.tokens || 0) - Number(a.tokens || 0)).slice(0, 10);
      const width = 960;
      const rowHeight = 34;
      const height = Math.max(178, 54 + rowHeight * Math.max(bars.length, 1));
      svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
      if (!bars.length) {
        svg.innerHTML = `<text x="18" y="54" class="axis-label">${escapeHtml(view.month)} 暂无日期消耗</text>`;
        return;
      }
      const labelWidth = 122;
      const right = 118;
      const maxTokens = Math.max(...bars.map((day) => Number(day.tokens || 0)), 1);
      svg.innerHTML = bars.map((day, index) => {
        const y = 36 + index * rowHeight;
        const barWidth = Math.max(6, Number(day.tokens || 0) / maxTokens * (width - labelWidth - right - 36));
        const color = colors[index % colors.length];
        return `<text x="18" y="${y + 18}" class="bar-label">${escapeHtml(day.day)}</text><rect x="${labelWidth}" y="${y + 3}" width="${trimNumber(barWidth, 3)}" height="18" rx="5" fill="${color}" opacity="0.88"><title>${escapeHtml(day.day)}：${escapeHtml(day.tokens_display)}</title></rect><text x="${trimNumber(labelWidth + barWidth + 10, 3)}" y="${y + 18}" class="bar-value">${escapeHtml(day.tokens_display)}</text>`;
      }).join("");
    }

    function initMonthSelect(activeMonth) {
      const root = document.getElementById("month-select");
      const ordered = [...months].sort((a, b) => String(b.month).localeCompare(String(a.month)));
      root.innerHTML = ordered.map((view) => `<option value="${escapeHtml(view.month)}">${escapeHtml(formatMonthLabel(view.month))}</option>`).join("");
      root.value = activeMonth;
      root.addEventListener("change", () => {
        renderMonth(root.value);
      });
    }

    function renderMonth(month) {
      const view = monthMap.get(month) || months[months.length - 1];
      if (!view) return;
      const select = document.getElementById("month-select");
      if (select.value !== view.month) select.value = view.month;
      const label = formatMonthLabel(view.month);
      document.getElementById("daily-title").textContent = `${label} · 每日 Token 趋势`;
      document.getElementById("month-note").textContent = `指向曲线上的日期点，可以查看当天 Token、会话数、均值和占比。`;
      document.getElementById("source-title").textContent = `${label} · 主要入口`;
      document.getElementById("model-title").textContent = `${label} · 模型分布`;
      document.getElementById("bar-title").textContent = `${label} · 高消耗日期`;
      document.getElementById("stat-total").textContent = view.tokens_display || formatTokens(view.tokens);
      document.getElementById("stat-threads").textContent = String(view.threads || 0);
      document.getElementById("stat-avg").textContent = view.avg_display || formatTokens(view.avg_tokens);
      document.getElementById("stat-max").textContent = view.max_tokens_display || formatTokens(view.max_tokens);
      document.getElementById("stat-active").textContent = formatPct(view.active_share || 0);
      document.getElementById("stat-subagent").textContent = formatPct(view.subagent_share || 0);
      renderLine(view);
      const source = prepareItems(view.sources || [], (row) => row.source || "未知来源");
      const model = prepareItems(view.models || [], (row) => `${row.model || "未记录模型"} · ${row.provider || "未知提供方"}`);
      renderDonut("source-donut", source.items, source.total);
      renderDonut("model-donut", model.items, model.total);
      renderBars(view);
    }

    const initialMonth = chartData.defaultMonth || months[months.length - 1]?.month;
    initMonthSelect(initialMonth);
    renderMonth(initialMonth);
  </script>
</body>
</html>'''
    return template.replace("__MONTH_DATA__", payload_json)


def chart_data_url(report: dict[str, Any]) -> str:
    payload = base64.b64encode(chart_report_html(report).encode("utf-8")).decode("ascii")
    return f"data:text/html;base64,{payload}"


def html_doc_markdown(report: dict[str, Any]) -> str:
    meta = report["meta"]
    summary = report["summary"]
    charts_url = chart_data_url(report)

    top_month = max(report["monthly"], key=lambda item: item["tokens"], default=None)
    max_avg_month = max(report["monthly"], key=lambda item: item["avg_tokens"], default=None)
    top_source = max(report["sources"], key=lambda item: item["tokens"], default=None)
    top_model = max(report["models"], key=lambda item: item["tokens"], default=None)
    default_month = report.get("default_month") or ""
    default_month_view = next((item for item in report.get("month_views", []) if item.get("month") == default_month), None)
    default_month_days = (default_month_view or {}).get("days", [])
    default_sources = (default_month_view or {}).get("sources") or report["sources"]
    default_models = (default_month_view or {}).get("models") or report["models"]
    default_top_sessions = (default_month_view or {}).get("top_sessions") or report["top_sessions"]
    top_day = (default_month_view or {}).get("top_day") or max(report["daily_top"], key=lambda item: item["tokens"], default=None)
    default_month_label = format_month_label(default_month)
    top_month_label = format_month_label(top_month["month"]) if top_month else "暂无月份"
    max_avg_month_label = format_month_label(max_avg_month["month"]) if max_avg_month else "暂无月份"
    top_day_label = format_day_label(top_day["day"]) if top_day else "暂无日期"

    blocks: list[str] = []
    blocks.append(
        "---\n"
        f"title: {frontmatter_text(meta['title'])}\n"
        "subtitle: Codex Token 月度用量分析\n"
        "lang: zh-CN\n"
        "glossary:\n"
        "  Token: 模型处理文本时使用的计量单位，输入、输出、工具上下文都会影响数量\n"
        "  Thread: Codex 里的一次对话记录\n"
        "  Provider: 模型调用来源或服务提供方，例如 openai、codex、sub2api\n"
        "---"
    )

    blocks.append(
        json_block(
            "actions",
            [
                {"label": "复制总量查询", "primary": True, "copy": report["queries"]["total"]},
                {"label": "复制月度查询", "copy": report["queries"]["monthly"]},
            ],
        )
    )

    blocks.append(
        json_block(
            "hero",
            {
                "id": "hero",
                "span": 12,
                "kicker": "Codex Usage",
                "title": f"{default_month_label} Codex Token 消耗报告",
                "body": (
                    f"本月共消耗 {default_month_view['tokens_display'] if default_month_view else summary['total_tokens_display']} Token，"
                    f"分布在 {default_month_view['threads'] if default_month_view else summary['threads']} 个会话中。"
                    "下方整理了每日走势、主要来源、模型分布和高消耗会话。"
                ),
                "tags": ["本月用量", "每日走势", "来源拆分", "模型分布", "高消耗会话"],
            },
        )
    )

    blocks.append(
        json_block(
            "embed",
            {
                "id": "usage-real-charts",
                "span": 12,
                "title": f"{default_month_label} · 每日 Token 趋势",
                "body": "把每天的消耗连成曲线，指向日期点可以查看当天会话数、单会话均值和本月占比。",
                "url": charts_url,
                "height": "980px",
            },
        )
    )

    blocks.append(
        json_block(
            "matrix",
            {
                "id": "monthly-table",
                "span": 12,
                "title": f"{top_month_label}总量最高，{max_avg_month_label}单会话均值最高",
                "search": True,
                "sortable": True,
                "columns": [
                    {"key": "month", "label": "月份", "width": "112px"},
                    {"key": "threads", "label": "会话数", "width": "96px"},
                    {"key": "tokens", "label": "Token 数", "minWidth": "130px"},
                    {"key": "share", "label": "占比", "width": "100px"},
                    {"key": "avg", "label": "均值", "width": "110px"},
                ],
                "rows": [
                    {
                        "month": item["month"],
                        "threads": item["threads"],
                        "tokens": item["tokens_display"],
                        "share": {"badge": item["share_display"], "tone": "danger" if item["share_pct"] >= 30 else "info"},
                        "avg": item["avg_display"],
                    }
                    for item in report["monthly"]
                ],
            },
        )
    )

    blocks.append(
        json_block(
            "matrix",
            {
                "id": "source-table",
                "span": 6,
                "title": f"{default_month_label}主要来源",
                "search": True,
                "sortable": True,
                "columns": [
                    {"key": "source", "label": "来源", "minWidth": "130px"},
                    {"key": "threads", "label": "会话数", "width": "90px"},
                    {"key": "tokens", "label": "Token 数", "minWidth": "120px"},
                    {"key": "share", "label": "占比", "width": "96px"},
                    {"key": "avg", "label": "均值", "width": "100px"},
                ],
                "rows": [
                    {
                        "source": item["source"],
                        "threads": item["threads"],
                        "tokens": item["tokens_display"],
                        "share": {"badge": item["share_display"], "tone": "accent" if item["share_pct"] >= 50 else "success"},
                        "avg": item["avg_display"],
                    }
                    for item in default_sources
                ],
            },
        )
    )

    blocks.append(
        json_block(
            "matrix",
            {
                "id": "daily-top-table",
                "span": 6,
                "title": f"{default_month_label}每日明细：{top_day_label}最高",
                "search": True,
                "sortable": True,
                "columns": [
                    {"key": "day", "label": "日期", "minWidth": "120px"},
                    {"key": "threads", "label": "会话数", "width": "90px"},
                    {"key": "tokens", "label": "Token 数", "minWidth": "120px"},
                    {"key": "share", "label": "占比", "width": "96px"},
                    {"key": "avg", "label": "均值", "width": "100px"},
                ],
                "rows": [
                    {
                        "day": item["day"],
                        "threads": item["threads"],
                        "tokens": item["tokens_display"],
                        "share": {"badge": item["share_display"], "tone": "danger" if item["share_pct"] >= 15 else "info"},
                        "avg": item["avg_display"],
                    }
                    for item in default_month_days
                ],
            },
        )
    )

    blocks.append(
        json_block(
            "matrix",
            {
                "id": "model-table",
                "span": 12,
                "title": f"{default_month_label}模型和提供方",
                "search": True,
                "sortable": True,
                "filters": [{"key": "provider", "label": "提供方", "options": sorted({item["provider"] for item in default_models})}],
                "columns": [
                    {"key": "model", "label": "模型", "minWidth": "150px"},
                    {"key": "provider", "label": "提供方", "minWidth": "120px"},
                    {"key": "threads", "label": "会话数", "width": "90px"},
                    {"key": "tokens", "label": "Token 数", "minWidth": "130px"},
                    {"key": "share", "label": "占比", "width": "100px"},
                ],
                "rows": [
                    {
                        "model": item["model"],
                        "provider": item["provider"],
                        "threads": item["threads"],
                        "tokens": item["tokens_display"],
                        "share": {"badge": item["share_display"], "tone": "warning" if item["model"] == "未记录模型" else "accent"},
                    }
                    for item in default_models
                ],
            },
        )
    )

    blocks.append(
        json_block(
            "matrix",
            {
                "id": "top-sessions",
                "span": 12,
                "title": f"{default_month_label}高消耗会话",
                "search": True,
                "sortable": True,
                "filters": [
                    {"key": "source", "label": "来源", "options": sorted({item["source"] for item in default_top_sessions})},
                    {"key": "archived", "label": "状态", "options": sorted({item["archived"] for item in default_top_sessions})},
                ],
                "columns": [
                    {"key": "rank", "label": "#", "width": "56px"},
                    {"key": "tokens", "label": "Token 数", "minWidth": "120px"},
                    {"key": "created", "label": "创建时间", "minWidth": "150px"},
                    {"key": "model", "label": "模型", "minWidth": "130px"},
                    {"key": "provider", "label": "提供方", "minWidth": "110px"},
                    {"key": "source", "label": "来源", "width": "100px"},
                    {"key": "archived", "label": "状态", "width": "96px"},
                    {"key": "title", "label": "标题", "minWidth": "320px"},
                ],
                "rows": [
                    {
                        "rank": item["rank"],
                        "tokens": {"badge": item["tokens_display"], "tone": "danger" if item["rank"] <= 3 else "info"},
                        "created": item["created"],
                        "model": item["model"],
                        "provider": item["provider"],
                        "source": item["source"],
                        "archived": {"badge": item["archived"], "tone": "success" if item["archived"] == "未归档" else "warning"},
                        "title": clip_text(item["title"], 90),
                        "details": f"ID: `{item['id']}`\n\n工作目录: `{item['cwd']}`\n\nRollout: `{item['rollout_path']}`",
                    }
                    for item in default_top_sessions
                ],
            },
        )
    )

    return "\n\n".join(blocks) + "\n"


def resolve_html_doc_dir(raw: str | None) -> Path:
    candidates: list[Path] = []
    if raw:
        candidates.append(Path(raw).expanduser())
    env_dir = os.environ.get("HTML_DOC_SKILL_DIR")
    if env_dir:
        candidates.append(Path(env_dir).expanduser())
    candidates.extend(
        [
            Path.home() / ".agents" / "skills" / "html-doc",
            Path.home() / ".codex" / "skills" / "html-doc",
        ]
    )
    for candidate in candidates:
        render_script = candidate / "scripts" / "render-html-doc.mjs"
        if render_script.exists():
            return candidate.resolve()
    searched = "\n".join(f"  - {candidate}" for candidate in candidates)
    raise SystemExit(f"没有找到 html-doc 渲染器。已检查：\n{searched}")


def render_html_doc(markdown_path: Path, out_path: Path, html_doc_dir: Path) -> None:
    render_script = html_doc_dir / "scripts" / "render-html-doc.mjs"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        ["node", str(render_script), str(markdown_path), str(out_path)],
        cwd=html_doc_dir,
        text=True,
        capture_output=True,
    )
    if result.returncode != 0:
        details = (result.stderr or result.stdout or "").strip()
        raise SystemExit(f"html-doc 渲染失败：\n{details}")


def main() -> int:
    args = parse_args()
    source_db = resolve_db_path(args)
    out_path = Path(args.out).expanduser().resolve()
    template_path = Path(args.template).expanduser().resolve()
    md_path = Path(args.md_out).expanduser().resolve() if args.md_out else out_path.with_suffix(".md")
    json_out_path = Path(args.json_out).expanduser().resolve() if args.json_out else None
    if args.renderer == "data" and json_out_path is None:
        json_out_path = out_path.with_suffix(".json")

    since_epoch = local_epoch_for_date(args.since) if args.since else None
    until_epoch = local_epoch_for_date(args.until, end_of_day=True) if args.until else None

    snapshot_path: Path | None = None
    query_db = source_db
    temp_dir: tempfile.TemporaryDirectory[str] | None = None
    if not args.no_snapshot:
        if args.snapshot:
            snapshot_path = Path(args.snapshot).expanduser().resolve()
        else:
            snapshot_path = out_path.with_suffix(".snapshot.sqlite")
        query_db = snapshot_database(source_db, snapshot_path)
    else:
        temp_dir = None

    try:
        rows, columns = fetch_threads(query_db, since_epoch, until_epoch)
        report = build_report(
            rows,
            db_path=query_db,
            source_db_path=source_db,
            snapshot_path=snapshot_path,
            args=args,
            available_columns=columns,
        )
        if args.renderer == "standalone":
            render_report(report, template_path, out_path)
        elif args.renderer != "data":
            markdown = html_doc_markdown(report)
            md_path.parent.mkdir(parents=True, exist_ok=True)
            md_path.write_text(markdown, encoding="utf-8")
            if args.renderer == "html-doc":
                html_doc_dir = resolve_html_doc_dir(args.html_doc_dir)
                render_html_doc(md_path, out_path, html_doc_dir)
        if json_out_path:
            write_json(report, json_out_path)
    finally:
        if temp_dir is not None:
            temp_dir.cleanup()

    if args.renderer == "data":
        print(f"JSON 数据: {json_out_path}")
    else:
        print(f"HTML 报告: {out_path}")
    if args.renderer in {"html-doc", "md"}:
        print(f"html-doc Markdown: {md_path}")
    if json_out_path and args.renderer != "data":
        print(f"JSON 汇总: {json_out_path}")
    if snapshot_path:
        print(f"SQLite 快照: {snapshot_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
