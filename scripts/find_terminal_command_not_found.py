#!/usr/bin/env python3
"""Find Hermes terminal tool calls that failed with "command not found".

Reads Hermes's SQLite session store (``$HERMES_HOME/state.db`` by default),
correlates assistant terminal tool calls with their tool-result messages, and
reports calls whose result looks like a missing executable / shell command.

This is read-only: it never mutates the session database.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import pathlib
import re
import shlex
import sqlite3
import sys
import time
from dataclasses import dataclass
from typing import Any, Iterable

COMMAND_NOT_FOUND_PATTERNS: tuple[re.Pattern[str], ...] = (
    # bash/sh/zsh/fish/csh style diagnostics
    re.compile(r"(?im)(?:^|\n)\s*(?:/[^:\n]+:|[-\w./]+:)?\s*(?:line\s+\d+:\s*)?(?P<cmd>[\w./+-]+)\s*:\s*command not found\b"),
    re.compile(r"(?im)(?:^|\n)\s*(?:zsh|fish|csh|tcsh)(?::\d+)?:\s*command not found:\s*(?P<cmd>[^\s:]+)"),
    re.compile(r"(?im)(?:^|\n)\s*(?:sh|dash|bash|/bin/sh):\s*\d+:\s*(?P<cmd>[^:\s]+):\s*not found\b"),
    re.compile(r"(?im)(?:^|\n)\s*env:\s*[‘'\"]?(?P<cmd>[^'\"’\n:]+)[’'\"]?:\s*No such file or directory\b"),
    re.compile(r"(?im)(?:^|\n)\s*(?:[^:\n]+:\s*)?line\s+\d+:\s*(?P<cmd>[^:\s]+):\s*No such file or directory\b"),
    re.compile(r"(?im)(?:^|\n)\s*(?:xargs|env|sudo|bash|sh|zsh|fish|[^:\n]+):\s*(?P<cmd>[^:\s]+):\s*No such file or directory\b"),
    # Generic fallback for already-normalized tool traces.
    re.compile(r"(?im)\bcommand not found\b"),
)

SENSITIVE_RE = re.compile(
    r"(?i)(api[_-]?key|token|secret|password|authorization|bearer\s+)(=|:|\s+)[^\s,'\"]+"
)


@dataclass
class TerminalCall:
    session_id: str
    assistant_message_id: int | None
    tool_call_id: str | None
    timestamp: float | None
    command: str
    args_preview: str


def parse_since(value: str | None) -> float | None:
    if not value:
        return None
    text = value.strip()
    m = re.fullmatch(r"(\d+)([hdw])", text.lower())
    if m:
        n = int(m.group(1))
        return time.time() - n * {"h": 3600, "d": 86400, "w": 604800}[m.group(2)]
    try:
        return float(text)
    except ValueError:
        pass
    try:
        return dt.datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError as exc:
        raise SystemExit(f"Could not parse --since {value!r}; use 24h, 7d, epoch, or ISO timestamp") from exc


def iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return dt.datetime.fromtimestamp(float(ts)).astimezone().isoformat(timespec="seconds")


def hermes_home(value: str | None) -> pathlib.Path:
    return pathlib.Path(value or os.environ.get("HERMES_HOME", "~/.hermes")).expanduser()


def db_path(args: argparse.Namespace) -> pathlib.Path:
    if args.db:
        return pathlib.Path(args.db).expanduser()
    return hermes_home(args.home) / "state.db"


def redact(text: str) -> str:
    return SENSITIVE_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}[REDACTED]", text)


def one_line(text: Any, limit: int) -> str:
    if text is None:
        return ""
    if not isinstance(text, str):
        text = json.dumps(text, ensure_ascii=False, default=str)
    text = redact(text).replace("\r", "\\r").replace("\n", " ")
    return text[:limit]


def load_json_maybe(raw: Any) -> Any:
    if raw is None:
        return None
    if isinstance(raw, (dict, list)):
        return raw
    if not isinstance(raw, str):
        return raw
    try:
        return json.loads(raw)
    except Exception:
        return raw


def parse_tool_calls(raw: Any) -> list[dict[str, Any]]:
    data = load_json_maybe(raw)
    if not data:
        return []
    if isinstance(data, dict):
        data = data.get("tool_calls") or data.get("calls") or [data]
    if not isinstance(data, list):
        return []
    calls: list[dict[str, Any]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        raw_function = item.get("function")
        function: dict[str, Any] = raw_function if isinstance(raw_function, dict) else {}
        name = function.get("name") or item.get("name") or item.get("tool_name")
        args = function.get("arguments") if "arguments" in function else item.get("arguments", item.get("args"))
        args = load_json_maybe(args)
        calls.append({
            "id": item.get("id") or item.get("tool_call_id"),
            "name": name or "unknown",
            "args": args if args is not None else {},
        })
    return calls


def command_from_args(args: Any) -> str:
    if isinstance(args, dict):
        command = args.get("command") or args.get("cmd") or args.get("_raw")
        if command is not None:
            return str(command)
    if isinstance(args, str):
        return args
    return ""


def result_fields(content: Any) -> tuple[int | None, str, str]:
    """Return (exit_code, output_text, error_text) from a tool result payload."""
    parsed = load_json_maybe(content)
    if isinstance(parsed, dict):
        exit_code = parsed.get("exit_code")
        try:
            exit_code = int(exit_code) if exit_code is not None else None
        except Exception:
            exit_code = None
        output = parsed.get("output", "")
        error = parsed.get("error", "")
        return exit_code, str(output or ""), str(error or "")
    text = "" if parsed is None else str(parsed)
    return None, text, ""


def detect_command_not_found(text: str) -> tuple[bool, str | None]:
    # Tool results may contain JSON strings with escaped newlines plus appended
    # loop warnings. Normalize enough for shell-diagnostic regexes to match.
    text = text.replace("\\r", "\r").replace("\\n", "\n")
    for pattern in COMMAND_NOT_FOUND_PATTERNS:
        m = pattern.search(text)
        if m:
            cmd = m.groupdict().get("cmd") if m.groupdict() else None
            if cmd:
                cmd = cmd.strip().strip("'\"‘’“”")
            return True, cmd if cmd else None
    return False, None


def first_word(command: str) -> str | None:
    try:
        parts = shlex.split(command, posix=True)
    except Exception:
        parts = command.strip().split()
    if not parts:
        return None
    # Common shell wrappers: report the actual shell snippet when possible.
    if parts[0] in {"bash", "sh", "zsh", "fish"} and len(parts) >= 3 and parts[1] in {"-c", "-lc"}:
        try:
            nested = shlex.split(parts[2], posix=True)
            return nested[0] if nested else parts[0]
        except Exception:
            return parts[0]
    return parts[0]


def iter_candidate_tool_rows(con: sqlite3.Connection, since: float | None) -> Iterable[sqlite3.Row]:
    """Yield only tool-result rows likely to contain a missing-command failure.

    A full session DB may have hundreds of thousands of messages. Filtering in
    SQLite first keeps this helper usable on live gateway profiles.
    """
    params: list[Any] = []
    clauses = [
        "m.role = 'tool'",
        "("
        "m.content LIKE '%command not found%' OR "
        "m.content LIKE '%: not found%' OR "
        "m.content LIKE '%No such file or directory%' OR "
        "m.content LIKE '%\"exit_code\": 127%' OR "
        "m.content LIKE '%\"exit_code\":127%'"
        ")",
    ]
    if since is not None:
        clauses.append("m.timestamp >= ?")
        params.append(since)
    where = "WHERE " + " AND ".join(clauses)
    return con.execute(
        f"""
        SELECT m.id, m.session_id, m.role, m.content, m.tool_call_id, m.tool_calls,
               m.tool_name, m.timestamp, s.source, s.title
          FROM messages m
          LEFT JOIN sessions s ON s.id = m.session_id
          {where}
         ORDER BY m.id
        """,
        params,
    )


def find_terminal_call_for_result(
    con: sqlite3.Connection,
    result_row: dict[str, Any],
    preview: int,
) -> TerminalCall | None:
    """Find the assistant terminal call paired with a tool result row."""
    session_id = result_row["session_id"]
    tool_call_id = result_row.get("tool_call_id")

    # Most current Hermes rows retain tool_call_id, but scan a small local
    # window instead of the whole session so old/large DBs stay responsive.
    candidates = con.execute(
        """
        SELECT id, session_id, tool_calls, timestamp
          FROM messages
         WHERE session_id = ? AND tool_calls IS NOT NULL AND id < ?
         ORDER BY id DESC
         LIMIT 40
        """,
        (session_id, result_row["id"]),
    ).fetchall()

    fallback_terminal: TerminalCall | None = None
    for cand in candidates:
        candd = dict(cand)
        for call in parse_tool_calls(candd.get("tool_calls")):
            if call.get("name") != "terminal":
                continue
            args = call.get("args", {})
            terminal_call = TerminalCall(
                session_id=session_id,
                assistant_message_id=candd.get("id"),
                tool_call_id=call.get("id"),
                timestamp=candd.get("timestamp"),
                command=command_from_args(args),
                args_preview=one_line(args, preview),
            )
            if tool_call_id:
                if terminal_call.tool_call_id == tool_call_id:
                    return terminal_call
                continue
            if result_row.get("tool_name") == "terminal" and fallback_terminal is None:
                fallback_terminal = terminal_call

    if fallback_terminal is not None:
        return fallback_terminal

    # Gateway/tool-result-only surfaces may not retain assistant tool_calls.
    if result_row.get("tool_name") == "terminal":
        return TerminalCall(
            session_id=session_id,
            assistant_message_id=None,
            tool_call_id=tool_call_id,
            timestamp=result_row.get("timestamp"),
            command="",
            args_preview="",
        )
    return None


def scan(con: sqlite3.Connection, since: float | None, preview: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []

    for row in iter_candidate_tool_rows(con, since):
        rowd = dict(row)
        terminal_call = find_terminal_call_for_result(con, rowd, preview)
        if terminal_call is None:
            continue
        if "find_terminal_command_not_found.py" in terminal_call.command:
            # Avoid self-contaminating future scans with this script's own reports.
            continue

        exit_code, output, error = result_fields(rowd.get("content"))
        blob = "\n".join(part for part in (output, error, str(rowd.get("content") or "")) if part)
        matched, missing_cmd = detect_command_not_found(blob)
        if not matched and exit_code != 127:
            continue

        rows.append({
            "time": iso(rowd.get("timestamp") or terminal_call.timestamp),
            "session_id": rowd["session_id"],
            "source": rowd.get("source"),
            "title": rowd.get("title"),
            "assistant_message_id": terminal_call.assistant_message_id,
            "tool_result_message_id": rowd.get("id"),
            "tool_call_id": terminal_call.tool_call_id,
            "exit_code": exit_code,
            "missing_command": missing_cmd or first_word(terminal_call.command),
            "command": one_line(terminal_call.command, preview),
            "output_preview": one_line(output or error or rowd.get("content"), preview),
        })

    return rows


def print_text(rows: list[dict[str, Any]], *, total: int, limit: int) -> None:
    print(f"command-not-found terminal failures: {total} (showing {len(rows)})")
    for item in rows[:limit]:
        print(
            f"{item['time']} | {item['source'] or 'unknown'} | {item['session_id']} | "
            f"exit={item['exit_code']} | missing={item['missing_command'] or '?'} | "
            f"msg={item['tool_result_message_id']} | {item['command']}"
        )
        if item.get("output_preview"):
            print(f"  ↳ {item['output_preview']}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Flag Hermes terminal commands whose tool output indicates command not found."
    )
    parser.add_argument("--home", help="Hermes home/profile path; defaults to $HERMES_HOME or ~/.hermes")
    parser.add_argument("--db", help="Explicit state.db path (overrides --home)")
    parser.add_argument("--since", help="Only scan messages after this point: 24h, 7d, epoch, or ISO timestamp")
    parser.add_argument("--limit", type=int, default=100, help="Maximum rows to print (default: 100)")
    parser.add_argument("--preview", type=int, default=220, help="Preview chars for command/output fields")
    parser.add_argument("--format", choices=("text", "json", "csv"), default="text")
    args = parser.parse_args()

    path = db_path(args)
    if not path.exists():
        raise SystemExit(f"Missing Hermes state DB: {path}")

    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        rows = scan(con, parse_since(args.since), args.preview)
    finally:
        con.close()

    limited = rows[: args.limit]
    if args.format == "json":
        print(json.dumps({"state_db": str(path), "count": len(rows), "results": limited}, indent=2, ensure_ascii=False))
    elif args.format == "csv":
        fieldnames = [
            "time", "session_id", "source", "title", "assistant_message_id",
            "tool_result_message_id", "tool_call_id", "exit_code", "missing_command",
            "command", "output_preview",
        ]
        writer = csv.DictWriter(sys.stdout, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(limited)
    else:
        print_text(limited, total=len(rows), limit=args.limit)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
