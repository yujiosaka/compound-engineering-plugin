#!/usr/bin/env python3
"""Extract the conversation skeleton from a Claude Code, Codex, Cursor, Pi, or oh-my-pi (omp) JSONL session file.

Usage:
  cat <session.jsonl> | python3 extract-skeleton.py
  cat <session.jsonl> | python3 extract-skeleton.py --output PATH

Auto-detects platform (Claude Code, Codex, Cursor, Pi, or oh-my-pi (omp)) from the JSONL structure.
Extracts:
  - User messages (text only, no tool results)
  - Assistant text (no thinking/reasoning blocks)
  - Collapsed tool call summaries (consecutive same-tool calls grouped)

Consecutive tool calls of the same type are collapsed:
  3+ Read calls -> "[tools] 3x Read (file1, file2, +1 more) -> all ok"
Codex call/result pairs are deduplicated (only the result with status is kept).

When --output PATH is given, the extracted skeleton is written to PATH and
stdout receives only a one-line JSON status (_meta with wrote/bytes/stats).
This lets callers route bulk content to a scratch file without round-tripping
extraction bytes through orchestrator tool results.

Without --output, extracted content goes to stdout and ends with a _meta line.
"""
import argparse
import io
import os
import sys
import json
import re

parser = argparse.ArgumentParser(add_help=True)
parser.add_argument(
    "--output",
    metavar="PATH",
    help="Write extracted skeleton to PATH instead of stdout. Stdout receives a one-line _meta status.",
)
parser.add_argument("--json", action="store_true", help="Emit one JSON object with text and meta.")
parser.add_argument("--start-record", help="Inclusive canonical record SHA256; requires --end-record and --json.")
parser.add_argument("--end-record", help="Inclusive canonical record SHA256; requires --start-record and --json.")
parser.add_argument("--boundaries-json", action="store_true", help="Emit at most 1000 body-free native boundary headers.")
args = parser.parse_args()
if (args.start_record is None) != (args.end_record is None):
    parser.error("--start-record and --end-record are required together")
if args.start_record is not None and not args.json:
    parser.error("record selection requires --json")
if args.boundaries_json and (args.output or args.start_record is not None):
    parser.error("--boundaries-json cannot combine with --output or record selection")
if args.boundaries_json:
    args.json = True
if args.json and args.output:
    parser.error("--json and --output are mutually exclusive")
import runpy
shared = runpy.run_path(os.path.join(os.path.dirname(__file__), "extract-metadata.py"))

# Session logs are UTF-8; force UTF-8 I/O so Windows' cp1252 default can't crash
# on non-Latin-1 content like emoji, smart quotes, or box-drawing (#1258).
sys.stdin.reconfigure(encoding="utf-8", errors="replace")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# Capture-and-redirect when --output is set: prints in the rest of the script
# go to the buffer; at the end the buffer is written to PATH and a status
# line is emitted to the real stdout.
_original_stdout = sys.stdout
if args.output or args.json:
    sys.stdout = io.StringIO()

stats = {"lines": 0, "parse_errors": 0, "user": 0, "assistant": 0, "tool": 0, "summary": 0}

# Claude Code wrapper tags to strip from user message content.
# Strip entirely (tag + content): framework noise and raw command output.
# Strip tags only (keep content): command-message, command-name, command-args, user_query.
_STRIP_BLOCK = re.compile(
    r"<(?:task-notification|local-command-caveat|local-command-stdout|local-command-stderr|system-reminder)[^>]*>.*?</(?:task-notification|local-command-caveat|local-command-stdout|local-command-stderr|system-reminder)>",
    re.DOTALL,
)
_STRIP_TAG = re.compile(
    r"</?(?:command-message|command-name|command-args|user_query)[^>]*>"
)


def clean_text(text):
    """Strip framework wrapper tags from message text (Claude and Cursor)."""
    text = _STRIP_BLOCK.sub("", text)
    text = _STRIP_TAG.sub("", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text

# Buffer for pending tool entries: [{"ts", "name", "target", "status"}]
pending_tools = []


def flush_tools():
    """Print buffered tool entries, collapsing consecutive same-name groups."""
    if not pending_tools:
        return

    # Group consecutive entries by tool name
    groups = []
    for entry in pending_tools:
        if groups and groups[-1][0]["name"] == entry["name"]:
            groups[-1].append(entry)
        else:
            groups.append([entry])

    for group in groups:
        name = group[0]["name"]
        if len(group) <= 2:
            # Print individually
            for e in group:
                status = f" -> {e['status']}" if e.get("status") else ""
                ts_prefix = f"[{e['ts']}] " if e.get("ts") else ""
                print(f"{ts_prefix}[tool] {name} {e['target']}{status}")
                stats["tool"] += 1
        else:
            # Collapse
            ts = group[0].get("ts", "")
            targets = [e["target"] for e in group if e.get("target")]
            ok = sum(1 for e in group if e.get("status") == "ok")
            err = sum(1 for e in group if e.get("status") and e["status"] != "ok")
            no_status = len(group) - ok - err

            # Show first 2 targets, then "+N more"
            if len(targets) > 2:
                target_str = ", ".join(targets[:2]) + f", +{len(targets) - 2} more"
            elif targets:
                target_str = ", ".join(targets)
            else:
                target_str = ""

            if no_status == len(group):
                status_str = ""
            elif err == 0:
                status_str = " -> all ok"
            else:
                status_str = f" -> {ok} ok, {err} error"

            ts_prefix = f"[{ts}] " if ts else ""
            print(f"{ts_prefix}[tools] {len(group)}x {name} ({target_str}){status_str}")
            stats["tool"] += len(group)

    pending_tools.clear()


def _safe_slice(value, n):
    """Slice value if it is a string; otherwise return ''.

    Some Claude Code / MCP tool inputs put structured data (dicts, lists) in
    fields like `query` or `prompt`. `dict[:N]` raises TypeError, so guard
    every slice with an isinstance check.
    """
    return value[:n] if isinstance(value, str) else ""


def summarize_claude_tool(block):
    """Extract name and target from a Claude Code tool_use block."""
    name = block.get("name", "unknown")
    inp = block.get("input", {})
    fp = inp.get("file_path")
    p = inp.get("path")
    target = (
        (fp if isinstance(fp, str) else None)
        or (p if isinstance(p, str) else None)
        or _safe_slice(inp.get("command"), 120)
        or _safe_slice(inp.get("pattern"), 200)
        or _safe_slice(inp.get("query"), 80)
        or _safe_slice(inp.get("prompt"), 80)
        or ""
    )
    if isinstance(target, str) and len(target) > 120:
        target = target[:120]
    return name, target


def handle_claude(obj):
    msg_type = obj.get("type")
    ts = obj.get("timestamp", "")[:19]

    if msg_type == "user":
        msg = obj.get("message", {})
        content = msg.get("content", "")

        if isinstance(content, list):
            for block in content:
                if block.get("type") == "tool_result":
                    is_error = block.get("is_error", False)
                    status = "error" if is_error else "ok"
                    tool_use_id = block.get("tool_use_id")
                    matched = False
                    if tool_use_id:
                        for entry in pending_tools:
                            if entry.get("id") == tool_use_id:
                                entry["status"] = status
                                matched = True
                                break
                    if not matched:
                        # Fallback: assign to earliest pending entry without a status
                        for entry in pending_tools:
                            if not entry.get("status"):
                                entry["status"] = status
                                break

            texts = [
                c.get("text", "")
                for c in content
                if c.get("type") == "text" and len(c.get("text", "")) > 0
            ]
            content = " ".join(texts)

        if isinstance(content, str):
            content = clean_text(content)
            if len(content) > 0:
                flush_tools()
                print(f"[{ts}] [user] {content[:800]}")
                print("---")
                stats["user"] += 1

    elif msg_type == "assistant":
        msg = obj.get("message", {})
        content = msg.get("content", [])
        if isinstance(content, list):
            has_text = False
            for block in content:
                if block.get("type") == "text":
                    text = clean_text(block.get("text", ""))
                    if len(text) > 0:
                        if not has_text:
                            flush_tools()
                            has_text = True
                        print(f"[{ts}] [assistant] {text[:800]}")
                        print("---")
                        stats["assistant"] += 1
                elif block.get("type") == "tool_use":
                    name, target = summarize_claude_tool(block)
                    entry = {"ts": ts, "name": name, "target": target}
                    tool_id = block.get("id")
                    if tool_id:
                        entry["id"] = tool_id
                    pending_tools.append(entry)


def handle_codex(obj):
    ts = obj.get("timestamp", "")[:19]
    if obj.get("type") == "_reader_tool":
        pending_tools.append({"ts": ts, "name": obj["name"], "target": obj["target"][:120], "status": obj["status"]})
    elif obj.get("type") == "response_item":
        p = obj.get("payload", {})
        if p.get("type") == "message" and p.get("role") in ("user", "assistant"):
            text = shared["message_text"](p.get("content"))
            if p.get("role") == "user":
                text = text.split("</system_instruction>")[-1].strip()
            if text:
                flush_tools()
                role = p["role"]
                print(f"[{ts}] [{role}] {text[:800]}")
                print("---")
                stats[role] += 1


def _pi_text_content(content):
    if isinstance(content, str):
        return [content]
    if not isinstance(content, list):
        return []
    return [
        block.get("text", "")
        for block in content
        if isinstance(block, dict) and block.get("type") == "text"
    ]


def _pi_active_path_objects(objects):
    """Return only entries on Pi's active leaf-to-root path."""
    by_id = {
        obj.get("id"): obj
        for obj in objects
        if isinstance(obj.get("id"), str) and obj.get("type") != "session"
    }
    leaf_id = None
    for obj in objects:
        if obj.get("type") != "session" and isinstance(obj.get("id"), str):
            leaf_id = obj["id"]
    if not leaf_id:
        return objects

    active_ids = set()
    current = leaf_id
    while isinstance(current, str) and current and current not in active_ids:
        active_ids.add(current)
        parent = by_id.get(current, {}).get("parentId")
        current = parent if isinstance(parent, str) else None
    return [
        obj
        for obj in objects
        if obj.get("type") == "session" or obj.get("id") in active_ids
    ]


def _pi_context_objects(objects):
    """Return Pi entries that participate in active LLM context."""
    active = _pi_active_path_objects(objects)
    compactions = [obj for obj in active if obj.get("type") == "compaction"]
    if not compactions:
        return active

    first_kept = compactions[-1].get("firstKeptEntryId")
    if not isinstance(first_kept, str):
        return active

    latest_compaction_id = compactions[-1].get("id")
    started = False
    found_first_kept = False
    context = [obj for obj in active if obj.get("type") == "session"]
    context.append(compactions[-1])
    for obj in active:
        if obj.get("type") == "session":
            continue
        if obj.get("id") == first_kept:
            started = True
            found_first_kept = True
        if obj.get("id") == latest_compaction_id:
            continue
        if started:
            context.append(obj)
    return context if found_first_kept and len(context) > 1 else active


def handle_pi(obj):
    """Pi/omp sessions: type='message' with message.role and content blocks.

    omp's physical type:'title' slot line lands here too and returns below
    like any non-message entry. type:'title_change' entries are a different
    pi entry type — they also carry no message payload and return the same
    way; do not conflate the two when filtering."""
    entry_type = obj.get("type")
    ts = obj.get("timestamp", "")[:19]

    if entry_type in ("compaction", "branch_summary"):
        text = clean_text(obj.get("summary", ""))
        if len(text) > 15:
            flush_tools()
            print(f"[{ts}] [summary] {text[:800]}")
            print("---")
            stats["summary"] += 1
        return

    if entry_type == "custom_message":
        text = clean_text(" ".join(_pi_text_content(obj.get("content", []))))
        if len(text) > 15:
            flush_tools()
            print(f"[{ts}] [summary] {text[:800]}")
            print("---")
            stats["summary"] += 1
        return

    if entry_type != "message":
        return

    msg = obj.get("message", {})
    role = msg.get("role", "")
    content = msg.get("content", [])

    if role == "bashExecution":
        exit_code = msg.get("exitCode")
        if msg.get("cancelled"):
            status = "cancelled"
        elif exit_code in (None, 0):
            status = "ok"
        else:
            status = f"error(exit {exit_code})"
        command = _safe_slice(msg.get("command"), 120)
        pending_tools.append({"ts": ts, "name": "bash", "target": command, "status": status})
        return

    if role == "custom":
        text = clean_text(" ".join(_pi_text_content(content)))
        if len(text) > 15:
            flush_tools()
            print(f"[{ts}] [summary] {text[:800]}")
            print("---")
            stats["summary"] += 1
        return

    if role == "user":
        text = clean_text(" ".join(_pi_text_content(content)))
        if len(text) > 15:
            flush_tools()
            print(f"[{ts}] [user] {text[:800]}")
            print("---")
            stats["user"] += 1

    elif role == "assistant":
        if isinstance(content, str):
            text = clean_text(content)
            if len(text) > 20:
                flush_tools()
                print(f"[{ts}] [assistant] {text[:800]}")
                print("---")
                stats["assistant"] += 1
            return

        has_text = False
        for block in (content if isinstance(content, list) else []):
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                text = clean_text(block.get("text", ""))
                if len(text) > 20:
                    if not has_text:
                        flush_tools()
                        has_text = True
                    print(f"[{ts}] [assistant] {text[:800]}")
                    print("---")
                    stats["assistant"] += 1
            elif block.get("type") == "toolCall":
                name = block.get("name", "unknown")
                args = block.get("arguments", {})
                if not isinstance(args, dict):
                    args = {}
                target = (
                    _safe_slice(args.get("path"), 200)
                    or _safe_slice(args.get("file_path"), 200)
                    or _safe_slice(args.get("command"), 120)
                    or _safe_slice(args.get("pattern"), 200)
                    or _safe_slice(args.get("query"), 80)
                    or _safe_slice(args.get("prompt"), 80)
                    or ""
                )
                if isinstance(target, str) and len(target) > 120:
                    target = target[:120]
                entry = {"ts": ts, "name": name, "target": target}
                tool_id = block.get("id")
                if tool_id:
                    entry["id"] = tool_id
                pending_tools.append(entry)

    elif role == "toolResult":
        tool_call_id = msg.get("toolCallId")
        is_error = bool(msg.get("isError"))
        if isinstance(content, list):
            is_error = is_error or any(
                isinstance(block, dict) and block.get("type") == "toolError"
                for block in content
            )
        status = "error" if is_error else "ok"
        if tool_call_id:
            for entry in pending_tools:
                if entry.get("id") == tool_call_id:
                    entry["status"] = status
                    break
        else:
            for entry in pending_tools:
                if not entry.get("status"):
                    entry["status"] = status
                    break


def handle_cursor(obj):
    """Cursor agent transcripts: role-based, no timestamps, same content structure as Claude."""
    role = obj.get("role")
    content = obj.get("message", {}).get("content", [])

    if role == "user":
        texts = []
        for block in (content if isinstance(content, list) else []):
            if block.get("type") == "text":
                texts.append(block.get("text", ""))
        text = clean_text(" ".join(texts))
        if len(text) > 15:
            flush_tools()
            # No timestamps available in Cursor transcripts
            print(f"[user] {text[:800]}")
            print("---")
            stats["user"] += 1

    elif role == "assistant":
        has_text = False
        for block in (content if isinstance(content, list) else []):
            if block.get("type") == "text":
                text = block.get("text", "")
                # Skip [REDACTED] placeholder blocks
                if len(text) > 20 and text.strip() != "[REDACTED]":
                    if not has_text:
                        flush_tools()
                        has_text = True
                    print(f"[assistant] {text[:800]}")
                    print("---")
                    stats["assistant"] += 1
            elif block.get("type") == "tool_use":
                name = block.get("name", "unknown")
                inp = block.get("input", {})
                p = inp.get("path")
                fp = inp.get("file_path")
                target = (
                    (p if isinstance(p, str) else None)
                    or (fp if isinstance(fp, str) else None)
                    or _safe_slice(inp.get("command"), 120)
                    or _safe_slice(inp.get("pattern"), 200)
                    or _safe_slice(inp.get("glob_pattern"), 200)
                    or _safe_slice(inp.get("target_directory"), 200)
                    or ""
                )
                if isinstance(target, str) and len(target) > 120:
                    target = target[:120]
                # No status info available — Cursor doesn't log tool results
                pending_tools.append({"ts": "", "name": name, "target": target})


# Auto-detect platform from first few lines, then process all
detected = None
buffer = []
# omp files physically begin with a fixed-width type:'title' slot line before
# the pi-shaped type:'session' header; bare pi files start with the header.
seen_title_slot = False

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    buffer.append(line)
    stats["lines"] += 1

    # Scan until a decisive record appears (short-circuits once detected).
    # Claude Code sessions front-load 10+ non-message metadata records
    # (last-prompt, custom-title, attachments, ...), so capping detection to
    # the first N lines silently misroutes them to the codex handler and
    # produces an empty skeleton with parse_errors: 0 (issue #923).
    if not detected:
        try:
            obj = json.loads(line)
            if obj.get("type") == "session" and "cwd" in obj:
                detected = "omp" if seen_title_slot else "pi"
            elif obj.get("type") == "title" and len(buffer) == 1:
                seen_title_slot = True
            elif obj.get("type") in ("user", "assistant"):
                detected = "claude"
            elif obj.get("type") in ("session_meta", "turn_context", "response_item", "event_msg"):
                detected = "codex"
            elif obj.get("role") in ("user", "assistant") and "type" not in obj:
                detected = "cursor"
        except (json.JSONDecodeError, KeyError, AttributeError, TypeError):
            pass

handlers = {"claude": handle_claude, "codex": handle_codex, "cursor": handle_cursor, "pi": handle_pi, "omp": handle_pi}
handler = handlers.get(detected, handle_codex)

objects = []
for line in buffer:
    try:
        value = json.loads(line)
        if not isinstance(value, dict):
            raise TypeError("object required")
        objects.append(value)
    except (json.JSONDecodeError, KeyError, AttributeError, TypeError):
        stats["parse_errors"] += 1

if detected in ("pi", "omp"):
    objects = _pi_context_objects(objects)

if args.boundaries_json:
    boundaries, diagnostics = shared["prepare_window"](objects, detected, None, None, stats["parse_errors"], boundaries=True)
    stats.update(diagnostics)
    sys.stdout = _original_stdout
    print(json.dumps({"boundaries": boundaries, "meta": {"_meta": True, **stats}}))
    sys.exit(0)
if args.start_record is not None:
    objects, diagnostics = shared["prepare_window"](objects, detected, args.start_record, args.end_record, stats["parse_errors"])
else:
    objects, diagnostics = shared["prepare_records"](objects, detected)
stats.update(diagnostics)

for obj in objects:
    try:
        handler(obj)
    except (KeyError, TypeError, AttributeError, ValueError):
        stats["parse_errors"] += 1

# Flush any remaining buffered tools
flush_tools()

print(json.dumps({"_meta": True, **stats}))

if args.output:
    body = sys.stdout.getvalue()
    sys.stdout = _original_stdout
    with open(args.output, "w", encoding="utf-8") as f:
        f.write(body)
    bytes_written = os.path.getsize(args.output)
    print(json.dumps({"_meta": True, "wrote": args.output, "bytes": bytes_written, **stats}))

if args.json:
    body = sys.stdout.getvalue()
    sys.stdout = _original_stdout
    text = "\n".join(body.splitlines()[:-1])
    print(json.dumps({"text": text, "meta": {"_meta": True, **stats}}))
