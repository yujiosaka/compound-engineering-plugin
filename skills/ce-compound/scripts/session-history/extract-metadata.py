#!/usr/bin/env python3
"""Extract session metadata from Claude Code, Codex, Cursor, Pi, and oh-my-pi (omp) JSONL files.

Batch mode (preferred — one invocation for all files):
  python3 extract-metadata.py /path/to/dir/*.jsonl
  python3 extract-metadata.py file1.jsonl file2.jsonl file3.jsonl

Single-file mode (stdin):
  head -20 <session.jsonl> | python3 extract-metadata.py

Auto-detects platform from the JSONL structure.
Outputs one JSON object per file, one per line.
Includes a final _meta line with processing stats.
"""
import sys
import json
import os
import hashlib

MAX_BYTES = 8 * 1024 * 1024
MAX_LINES = 25  # Only need first ~25 lines for metadata


def try_claude(lines):
    result = None
    cwd = ""
    for line in lines:
        try:
            obj = json.loads(line.strip())
        except (json.JSONDecodeError, KeyError, AttributeError, TypeError):
            continue
        if not cwd and obj.get("cwd"):
            cwd = obj["cwd"]
        if result is None and obj.get("type") in ("user", "assistant") and ("sessionId" in obj or "cwd" in obj):
            result = {
                "platform": "claude",
                "branch": obj.get("gitBranch", ""),
                "ts": obj.get("timestamp", ""),
                "session": obj.get("sessionId", ""),
            }
            if obj.get("cwd"):
                cwd = obj["cwd"]
        if result is not None and cwd:
            break
    if result is not None and cwd:
        result["cwd"] = cwd
    return result


def try_codex(lines):
    meta = {}
    for line in lines:
        try:
            obj = json.loads(line.strip())
            if obj.get("type") == "session_meta":
                p = obj.get("payload", {})
                meta["platform"] = "codex"
                meta["cwd"] = p.get("cwd", "")
                meta["session"] = p.get("id", "")
                meta["ts"] = p.get("timestamp", obj.get("timestamp", ""))
                meta["source"] = p.get("source", "")
                meta["cli_version"] = p.get("cli_version", "")
            elif obj.get("type") == "turn_context":
                p = obj.get("payload", {})
                meta["platform"] = "codex"
                meta["model"] = p.get("model", "")
                meta["cwd"] = meta.get("cwd") or p.get("cwd", "")
        except (json.JSONDecodeError, KeyError, AttributeError, TypeError):
            pass
    return meta if meta else None


def try_omp(lines):
    """oh-my-pi (omp) sessions: a fixed-width type='title' slot line physically
    first, then a pi-shaped type='session' header with cwd. Checked before Pi:
    a bare pi file has no title slot and must still detect as pi."""
    seen_first = False
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        try:
            obj = json.loads(stripped)
        except (json.JSONDecodeError, KeyError, AttributeError, TypeError):
            if not seen_first:
                return None
            continue
        if not seen_first:
            seen_first = True
            if obj.get("type") != "title":
                return None
            continue
        if obj.get("type") == "session" and "cwd" in obj:
            return {
                "platform": "omp",
                "cwd": obj.get("cwd", ""),
                "session": obj.get("id", ""),
                "ts": obj.get("timestamp", ""),
            }
    return None


def try_pi(lines):
    """Pi sessions: type='session' header with cwd, followed by message entries."""
    for line in lines:
        try:
            obj = json.loads(line.strip())
            if obj.get("type") == "session" and "cwd" in obj:
                return {
                    "platform": "pi",
                    "cwd": obj.get("cwd", ""),
                    "session": obj.get("id", ""),
                    "ts": obj.get("timestamp", ""),
                }
        except (json.JSONDecodeError, KeyError, AttributeError, TypeError):
            pass
    return None


def try_cursor(lines):
    """Cursor agent transcripts: role-based entries, no timestamps or metadata fields."""
    for line in lines:
        try:
            obj = json.loads(line.strip())
            # Cursor entries have 'role' at top level but no 'type'
            if obj.get("role") in ("user", "assistant") and "type" not in obj:
                return {"platform": "cursor"}
        except (json.JSONDecodeError, KeyError, AttributeError, TypeError):
            pass
    return None


def extract_from_lines(lines):
    return try_claude(lines) or try_codex(lines) or try_omp(lines) or try_pi(lines) or try_cursor(lines)


TAIL_BYTES = 16384  # Read last 16KB to find final timestamp past trailing metadata


def get_last_timestamp(filepath, size):
    """Read the tail of a file to find the last message with a timestamp."""
    try:
        with open(filepath, "rb") as f:
            f.seek(max(0, size - TAIL_BYTES))
            tail = f.read(TAIL_BYTES).decode("utf-8", errors="ignore")
            lines = tail.strip().split("\n")
        for line in reversed(lines):
            try:
                obj = json.loads(line.strip())
                if "timestamp" in obj:
                    return obj["timestamp"]
            except (json.JSONDecodeError, KeyError, AttributeError, TypeError):
                pass
    except (OSError, IOError):
        pass
    return None


def _pi_active_path_objects(objects):
    """Return only entries on Pi's active leaf-to-root path.

    Pi session files are append-only trees. The final non-session entry is the
    active leaf; abandoned branches remain in the file but are not in context.
    """
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

    # Pi emits compaction summary first, then entries from firstKeptEntryId
    # onward. Exclude older ancestors so keyword search mirrors context.
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


def _append_pi_content_text(chunks, content):
    if isinstance(content, str):
        chunks.append(content)
    elif isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                chunks.append(block.get("text", ""))


def _append_pi_tool_call_targets(chunks, content):
    """Append searchable Pi toolCall targets without indexing tool output."""
    if not isinstance(content, list):
        return
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "toolCall":
            continue
        args = block.get("arguments", {})
        if not isinstance(args, dict):
            continue
        for key in ("path", "file_path", "command", "pattern", "query", "prompt"):
            value = args.get(key)
            if isinstance(value, str):
                chunks.append(value)


def _extract_user_assistant_text(filepath):
    """Return concatenated user + assistant text content from a session JSONL.

    Skips JSONL metadata field names and values (sessionId, gitBranch, uuid,
    timestamps, type tags), tool_use blocks (tool names + tool inputs),
    tool_result blocks (tool outputs), and thinking/reasoning blocks. Only
    content the user or assistant actually said is included.

    Without this filtering, common topic words like "session" would match every
    JSONL file via the sessionId field, drowning out real content matches.
    """
    chunks = []
    try:
        objects = []
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    objects.append(json.loads(line.strip()))
                except (json.JSONDecodeError, ValueError):
                    continue

        # omp files share the pi-shaped session header, so this covers both.
        is_pi = any(
            obj.get("type") == "session" and "cwd" in obj for obj in objects
        )
        if is_pi:
            objects = _pi_context_objects(objects)

        for obj in objects:
            # Claude Code: type-tagged top-level
            t = obj.get("type")
            if t == "user":
                msg = obj.get("message", {})
                content = msg.get("content")
                if isinstance(content, str):
                    chunks.append(content)
                elif isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            chunks.append(block.get("text", ""))
                        # Skip tool_result blocks — tool outputs are not user content.
                continue
            if t == "assistant":
                msg = obj.get("message", {})
                content = msg.get("content", [])
                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            chunks.append(block.get("text", ""))
                        # Skip tool_use and thinking blocks.
                continue

            # Codex: payload-typed events
            if t == "event_msg":
                p = obj.get("payload", {})
                if p.get("type") == "user_message":
                    # Strip Codex/Conductor `<system_instruction>...</system_instruction>`
                    # wrapper before counting. Without this, generic wrapper terms
                    # (e.g., "Conductor", environment labels) false-match against
                    # boilerplate the user did not author. Mirrors the same split
                    # used in ce-session-extract/scripts/extract-skeleton.py.
                    msg = p.get("message", "")
                    if isinstance(msg, str):
                        parts = msg.split("</system_instruction>")
                        chunks.append(parts[-1] if parts else msg)
                continue
            if t == "response_item":
                p = obj.get("payload", {})
                if p.get("type") == "message" and p.get("role") in ("user", "assistant"):
                    for block in p.get("content", []):
                        if isinstance(block, dict) and block.get("type") in ("input_text", "output_text"):
                            chunks.append(block.get("text", ""))
                continue

            # Pi: type='message' envelope with AgentMessage under message.
            if t == "message" and "message" in obj:
                msg = obj.get("message", {})
                role = msg.get("role", "")
                if role == "bashExecution":
                    command = msg.get("command", "")
                    if isinstance(command, str):
                        chunks.append(command)
                    # Search command text only. Output is tool output and can
                    # be large/noisy in the same way as toolResult content.
                    continue
                content = msg.get("content", [])
                if role == "custom":
                    _append_pi_content_text(chunks, content)
                    continue
                if role not in ("user", "assistant"):
                    continue
                _append_pi_content_text(chunks, content)
                if role == "assistant":
                    _append_pi_tool_call_targets(chunks, content)
                continue

            if t in ("compaction", "branch_summary"):
                summary = obj.get("summary", "")
                if isinstance(summary, str):
                    chunks.append(summary)
                continue

            if t == "custom_message":
                _append_pi_content_text(chunks, obj.get("content", []))
                continue

            # Cursor: role-tagged with no top-level type
            if obj.get("role") in ("user", "assistant") and "type" not in obj:
                msg = obj.get("message", {})
                for block in msg.get("content", []) if isinstance(msg.get("content"), list) else []:
                    if isinstance(block, dict) and block.get("type") == "text":
                        chunks.append(block.get("text", ""))
                continue
    except (OSError, IOError):
        pass
    return "\n".join(chunks)


def count_keyword_matches(filepath, keywords):
    """Case-insensitive substring count for each keyword in user/assistant text.

    Returns a dict {original_keyword: count}. Scans only content the user or
    assistant said — not JSONL metadata, tool calls, tool outputs, or thinking
    blocks — so common topic words like "session" do not false-match against
    the sessionId field.
    """
    text_lower = _extract_user_assistant_text(filepath).lower()
    return {kw: text_lower.count(kw.lower()) for kw in keywords}


def process_file(filepath, max_bytes=MAX_BYTES):
    """Extract metadata only. Keyword scanning is done separately so callers
    can apply cheap filters (e.g. --cwd-filter) before paying the full-file
    content scan cost."""
    try:
        size = os.path.getsize(filepath)
        if size > max_bytes:
            return {"file": filepath, "size": size, "platform": "unknown", "oversized": True, **reader_metadata([])}, None
        with open(filepath, "rb") as f:
            lines, sampled_bytes = [], 0
            for _ in range(MAX_LINES):
                line = f.readline(max_bytes - sampled_bytes + 1)
                if not line:
                    break
                sampled_bytes += len(line)
                if sampled_bytes > max_bytes:
                    return {"file": filepath, "size": size, "platform": "unknown", "oversized": True, **reader_metadata([])}, None
                lines.append(line.decode("utf-8"))
        result = extract_from_lines(lines) or {"platform": "unknown"}
        result.update(reader_metadata(lines))
        for key in ("source", "cli_version"):
            values = result["identities"][key]
            if len(values) == 1:
                result[key] = values[0]
        if result:
            result["file"] = filepath
            result["size"] = size
            result["oversized"] = False
            if "/subagents/" in filepath.replace("\\", "/"):
                path_parent = os.path.basename(os.path.dirname(os.path.dirname(filepath)))
                if result["parent_session"] and result["parent_session"] != path_parent:
                    result["identity_conflicts"] += 1
                result["parent_session"] = result["parent_session"] or path_parent
                result["parent_source"] = "path"
            return result, None
        else:
            return None, filepath
    except (OSError, IOError, UnicodeError) as e:
        return None, filepath


def _normalize_cwd(path):
    path = path.replace("\\", "/").strip()
    if len(path) > 1 and path.endswith("/") and not (
        len(path) == 3 and path[1] == ":"
    ):
        path = path.rstrip("/")
    # Windows drive paths are case-insensitive. Do not rewrite POSIX /d/...
    # to D:/ — those are different filesystems.
    if len(path) >= 2 and path[1] == ":":
        path = path[0].upper() + ":" + path[2:].casefold()
    return path


def _is_abs_cwd(path):
    if os.path.isabs(path):
        return True
    return len(path) >= 2 and path[1] == ":"


def _cwd_paths_related(session_cwd, cwd_filter):
    if session_cwd == cwd_filter:
        return True
    prefix = cwd_filter if cwd_filter.endswith("/") else cwd_filter + "/"
    if session_cwd.startswith(prefix):
        return True
    prefix = session_cwd if session_cwd.endswith("/") else session_cwd + "/"
    return cwd_filter.startswith(prefix)


def _attach_timestamps(result, filepath):
    if result["platform"] == "cursor":
        # Cursor transcripts have no timestamps in JSONL.
        # Use file modification time as the best available signal.
        # Derive session ID from the parent directory name (UUID).
        mtime = os.path.getmtime(filepath)
        from datetime import datetime, timezone

        result["ts"] = datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat()
        result["session"] = os.path.basename(os.path.dirname(filepath))
        return
    last_ts = get_last_timestamp(filepath, result["size"])
    if last_ts:
        result["last_ts"] = last_ts


def cwd_matches_filter(session_cwd, cwd_filter):
    if not session_cwd or not cwd_filter:
        return True
    session = _normalize_cwd(session_cwd)
    filt = _normalize_cwd(cwd_filter)
    if not session or not filt:
        return True
    if _is_abs_cwd(filt):
        return _cwd_paths_related(session, filt)
    return filt in session.split("/")


def reader_metadata(lines):
    """Diagnostics for the sampled metadata; absence outside the sample is unknown."""
    values = {key: [] for key in ("cwd", "session", "parent_session", "source", "cli_version")}
    errors = 0
    for line in lines:
        try:
            obj = json.loads(line)
            if not isinstance(obj, dict):
                raise ValueError("object required")
        except (ValueError, TypeError):
            errors += 1
            continue
        p = obj.get("payload", {}) if obj.get("type") in ("session_meta", "turn_context") else obj
        if not isinstance(p, dict):
            errors += 1
            continue
        source = p.get("source")
        spawn = source.get("subagent", {}).get("thread_spawn", {}) if isinstance(source, dict) and isinstance(source.get("subagent"), dict) else {}
        spawn = spawn if isinstance(spawn, dict) else {}
        data = {"cwd": p.get("cwd"), "session": p.get("sessionId") or (p.get("id") if obj.get("type") == "session_meta" else None),
                "parent_session": p.get("parent_session_id") or p.get("parentSessionId") or spawn.get("parent_thread_id"),
                "source": source, "cli_version": p.get("cli_version") or p.get("version")}
        for key, value in data.items():
            if value is not None and value != "" and value not in values[key]:
                values[key].append(value)
    return {"identity_conflicts": sum(len(v) > 1 for v in values.values()),
            "identities": values, "metadata_missing": any(not values[k] for k in ("cwd", "session")),
            "missing_fields": [k for k in ("cwd", "session") if not values[k]],
            "parent_session": values["parent_session"][0] if len(values["parent_session"]) == 1 else None,
            "parse_errors": errors, "metadata_limited": True,
            "limits": {"head_lines": MAX_LINES, "tail_bytes": TAIL_BYTES}}


def result_evidence(value, depth=0):
    """Interpret result envelopes, never words that merely mention exit_code."""
    if depth > 32:
        return [], [], 1
    codes, flags, omitted = [], [], 0
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except (ValueError, TypeError):
            decoded = None
        if isinstance(decoded, (dict, list)):
            return result_evidence(decoded, depth + 1)
        # Codex's legacy process envelope is line-oriented, with a literal status line.
        import re
        codes = [int(m) for m in re.findall(r"(?m)^Process exited with code (-?\d+)\s*$", value)]
        return codes, flags, int("[REDACTED]" in value or "… truncated" in value or "output truncated" in value.lower() or "truncated output" in value.lower())
    if isinstance(value, list):
        children = value
    elif isinstance(value, dict):
        code = value.get("exit_code", value.get("exitCode"))
        if isinstance(code, int) and not isinstance(code, bool):
            codes.append(code)
        for key in ("is_error", "isError"):
            if isinstance(value.get(key), bool):
                flags.append(value[key])
        omitted += int(any(value.get(k) for k in ("truncated", "omitted", "output_truncated")))
        children = [value[k] for k in ("output", "content", "text", "result", "results", "data", "structuredContent") if k in value]
    else:
        children = []
    for child in children:
        c, f, o = result_evidence(child, depth + 1)
        codes.extend(c)
        flags.extend(f)
        omitted += o
    return codes, flags, omitted


def known_completed_envelope(obj):
    p = obj.get("payload")
    return (isinstance(p, dict) and set(p) == {"type", "thread_id", "turn_id", "item", "started_at_ms", "completed_at_ms"}
            and p["type"] == "item_completed"
            and all(isinstance(p[key], str) and p[key] for key in ("thread_id", "turn_id"))
            and all(isinstance(p[key], int) and not isinstance(p[key], bool) and p[key] >= 0 for key in ("started_at_ms", "completed_at_ms")))


def known_completed_user(obj):
    if not known_completed_envelope(obj):
        return False
    item = obj["payload"]["item"]
    return (isinstance(item, dict) and set(item) == {"type", "id", "content"} and item["type"] == "UserMessage"
            and isinstance(item["id"], str) and bool(item["id"]) and isinstance(item["content"], list)
            and all(isinstance(b, dict) and set(b) == {"type", "text", "text_elements"}
                    and b["type"] == "text" and isinstance(b["text"], str) and b["text_elements"] == [] for b in item["content"]))


def known_web_extension(obj):
    if not known_completed_envelope(obj):
        return False
    item = obj["payload"]["item"]
    if (not isinstance(item, dict) or set(item) != {"type", "kind", "id", "query", "action", "results"}
            or item["type"] != "Extension" or item["kind"] != "web.search"
            or not all(isinstance(item[key], str) for key in ("id", "query")) or not item["id"]):
        return False
    action = item["action"]
    if not isinstance(action, dict) or set(action) != {"type", "url"} or action["type"] != "openPage" or not isinstance(action["url"], str):
        return False
    fields = {"type", "domain", "ref_id", "snippet", "title", "url"}
    return isinstance(item["results"], list) and all(isinstance(r, dict) and fields <= set(r) <= fields | {"thumbnail_url"}
            and r["type"] == "text_result" and all(isinstance(v, str) for v in r.values()) for r in item["results"])


def completed_item(obj):
    """Keep native execution evidence without guessing its outer tool call."""
    p = obj.get("payload", {})
    if obj.get("type") != "event_msg" or not isinstance(p, dict) or p.get("type") != "item_completed":
        return obj
    item = p.get("item", {})
    if known_completed_user(obj):
        return {**obj, "payload": {"type": "user_message", "id": item["id"], "message": message_text(item["content"])}}
    if not isinstance(item, dict) or item.get("type") not in ("CommandExecution", "McpToolCall", "FileChange"):
        return obj
    result = {"type": "exec_command_end", "call_id": item.get("id"), "requires_association": True,
              "name": item.get("tool") if isinstance(item.get("tool"), str) else item["type"]}
    for source, target in (("status", "native_status"), ("cwd", "cwd"), ("command", "command"), ("result", "output"), ("stderr", "stderr")):
        if source in item:
            result[target] = item[source]
    if "aggregated_output" in item or "stdout" in item:
        result["aggregated_output"] = item.get("aggregated_output", item.get("stdout"))
    if "exit_code" in item:
        result["exit_code"] = item["exit_code"]
    if item.get("status") == "failed":
        result["is_error"] = True
    return {**obj, "payload": result}


def tool_facts(payload):
    """Comparable observations; missing fields never erase an observed fact."""
    codes, flags, omitted = result_evidence(payload)
    legacy, _, legacy_omitted = result_evidence(payload.get("aggregated_output", ""))
    codes.extend(legacy)
    command = payload.get("command")
    if isinstance(command, list):
        command = command[-1] if command else None
    facts = {"codes": sorted(set(codes)), "flags": sorted(set(flags)), "omitted": omitted + legacy_omitted}
    for key, value in (("name", payload.get("name")), ("command", command), ("cwd", payload.get("cwd")), ("output", payload.get("output")), ("aggregated_output", payload.get("aggregated_output")), ("stderr", payload.get("stderr")), ("native_status", payload.get("native_status"))):
        if value is not None and (value != "" or key in ("output", "aggregated_output", "stderr")):
            if isinstance(value, str):
                try:
                    value = json.loads(value)
                except ValueError:
                    pass
            facts[key] = value
    facts["status"] = "error" if any(c != 0 for c in codes) or True in flags else ("ok" if codes or flags else "unknown")
    return facts


def codex_records(objects, diagnostics):
    """Join native call IDs, retaining every result until all evidence is reconciled."""
    objects = [completed_item(obj) for obj in objects]
    calls, results, messages, normalized, aliases, message_ids = {}, {}, {}, [], {}, {}
    for obj in objects:
        p = obj.get("payload", {})
        if not isinstance(p, dict):
            continue
        kind, identity = p.get("type"), p.get("call_id")
        identity = identity if isinstance(identity, str) and identity else None
        if obj.get("type") == "response_item" and kind in ("function_call", "custom_tool_call"):
            if not identity:
                diagnostics["unmatched_calls"] += 1
            elif identity in calls and calls[identity] != p:
                diagnostics["identity_conflicts"] += 1
            else:
                calls[identity] = p
        elif obj.get("type") == "response_item" and kind in ("function_call_output", "custom_tool_call_output"):
            if identity:
                results.setdefault(identity, []).append(p)
        elif obj.get("type") == "event_msg" and kind == "exec_command_end" and identity:
            aliases.setdefault(identity, []).append(p)
        elif obj.get("type") == "response_item" and kind == "message":
            key = (p.get("role"), message_text(p.get("content")), obj.get("timestamp"))
            messages[key] = messages.get(key, 0) + 1
            if isinstance(p.get("id"), str) and p["id"]:
                message_ids.setdefault(p["id"], set()).add((p.get("role"), message_text(p.get("content"))))
    native_messages = {}
    for obj in objects:
        p = obj.get("payload", {})
        if isinstance(p, dict) and p.get("type") == "user_message" and isinstance(p.get("id"), str):
            native_messages.setdefault(p["id"], set()).add(("user", p.get("message")))
    for identity, values in native_messages.items():
        diagnostics["identity_conflicts"] += int(len(values | message_ids.get(identity, set())) > 1)
    emitted = set()
    emitted_messages = set()
    for obj in objects:
        p = obj.get("payload", {})
        if not isinstance(p, dict):
            normalized.append(obj)
            continue
        kind, identity = p.get("type"), p.get("call_id")
        identity = identity if isinstance(identity, str) and identity else None
        if obj.get("type") == "event_msg" and kind in ("user_message", "agent_message"):
            role = "user" if kind == "user_message" else "assistant"
            key = (role, p.get("message", ""), obj.get("timestamp"))
            mid = p.get("id")
            if isinstance(mid, str):
                if mid in emitted_messages or (role, p.get("message", "")) in message_ids.get(mid, set()):
                    continue
                emitted_messages.add(mid)
            if obj.get("timestamp") and messages.get(key, 0):
                messages[key] -= 1
                continue
            normalized.append({**obj, "type": "response_item", "payload": {"type": "message", "role": role, "content": [{"type": "input_text" if role == "user" else "output_text", "text": p.get("message", "")}]}})
            continue
        is_result = obj.get("type") == "response_item" and kind in ("function_call_output", "custom_tool_call_output")
        is_alias = obj.get("type") == "event_msg" and kind == "exec_command_end"
        if is_alias and identity in results:
            continue
        if is_result or is_alias:
            if identity and identity in emitted:
                continue
            if identity:
                emitted.add(identity)
            call = calls.get(identity, {})
            if not identity or (not call and (not is_alias or p.get("requires_association"))):
                diagnostics["unmatched_results"] += 1
            arguments = call.get("arguments", call.get("input", {}))
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except ValueError:
                    arguments = {}
            if not isinstance(arguments, dict):
                arguments = {}
            group = results.get(identity, []) + aliases.get(identity, []) if identity else [p]
            facts = [tool_facts(value) for value in group]
            # A disagreement on any mutually observed fact makes the identity unsafe.
            # Compare all results, rather than replacing an earlier alias with a later one.
            for key in ("codes", "flags", "status", "name", "command", "cwd", "output", "aggregated_output", "stderr", "native_status"):
                values = {json.dumps(f[key], sort_keys=True) for f in facts if key in f and not (key in ("codes", "flags") and not f[key]) and not (key == "status" and f[key] == "unknown")}
                if len(values) > 1:
                    diagnostics["identity_conflicts"] += 1
            cwd_values = [arguments[k] for k in ("workdir", "cwd") if isinstance(arguments.get(k), str)]
            cwd_values.extend(f["cwd"] for f in facts if isinstance(f.get("cwd"), str))
            if len(set(cwd_values)) > 1:
                diagnostics["identity_conflicts"] += 1
            executed_cwd = cwd_values[0] if len(set(cwd_values)) == 1 else None
            command = arguments.get("cmd", arguments.get("command", p.get("command", "")))
            if isinstance(command, list):
                command = command[-1] if command else ""
            if command and any(f.get("command") and f["command"] != command for f in facts):
                diagnostics["identity_conflicts"] += 1
            codes = [c for f in facts for c in f["codes"]]
            flags = [flag for f in facts for flag in f["flags"]]
            diagnostics["omitted"] += sum(f["omitted"] for f in facts)
            status = "error" if any(c != 0 for c in codes) or True in flags else ("ok" if codes or flags else "unknown")
            if status == "unknown":
                diagnostics["unknown_results"] += 1
            # Keep an error-bearing excerpt when conflicting results share an ID.
            selected = next((value for value, f in zip(group, facts) if f["status"] == "error"), p)
            output = selected["output"] if selected.get("output") is not None else selected
            name = call.get("name", p.get("name", "exec") if is_alias else "unknown")
            evidence = sorted({json.dumps(f, sort_keys=True, separators=(",", ":")) for f in facts})
            observation = {"call_id": identity, "name": name, "executed_cwd": executed_cwd, "status": status, "timestamp": obj.get("timestamp"), "result_digest": hashlib.sha256(json.dumps(evidence).encode()).hexdigest()}
            diagnostics["tool_observations"].append(observation)
            normalized.append({"type": "_reader_tool", "timestamp": obj.get("timestamp", ""), **observation, "target": command if isinstance(command, str) else "", "output": output, "exit_codes": codes})
        elif obj.get("type") != "response_item" or kind not in ("function_call", "custom_tool_call"):
            normalized.append(obj)
    diagnostics["unmatched_calls"] += len(set(calls) - emitted)
    return normalized


def message_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") in ("text", "input_text", "output_text") and isinstance(b.get("text"), str))
    return ""


def unknown_record(diagnostics, obj):
    """Expose bounded grammar tags, never message bodies or argument values."""
    import re
    def tag(value):
        return value if isinstance(value, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", value) else "unknown"
    key = tag(obj.get("type"))
    if key in ("response_item", "event_msg"):
        payload = obj.get("payload")
        key += ":" + tag(payload.get("type") if isinstance(payload, dict) else None)
    counts = diagnostics["unknown_record_types"]
    if key not in counts and len(counts) >= 31:
        key = "other"
    counts[key] = counts.get(key, 0) + 1
    diagnostics["unknown_records"] += 1


def inspect_message_content(content, platform, diagnostics):
    """Unrenderable message content is missing evidence, never a supported zero."""
    if isinstance(content, str):
        diagnostics["omitted"] += int("[REDACTED]" in content or "[TRUNCATED]" in content)
        return
    if not isinstance(content, list):
        unknown_record(diagnostics, {"type": "message_content"})
        return
    text_types = {"input_text", "output_text"} if platform == "codex" else {"text"}
    for block in content:
        if not isinstance(block, dict):
            unknown_record(diagnostics, {"type": "message_content"})
        elif block.get("type") in text_types:
            if not isinstance(block.get("text"), str):
                unknown_record(diagnostics, {"type": "message_content"})
            else:
                inspect_message_content(block["text"], platform, diagnostics)
        elif block.get("type") in ("image", "input_image", "image_url", "audio", "input_audio"):
            diagnostics["omitted"] += 1
        elif platform != "claude" or block.get("type") not in ("tool_use", "tool_result", "thinking", "redacted_thinking"):
            unknown_record(diagnostics, {"type": "message_content"})



def known_context_attachment(obj):
    """Recognized runtime context is unread evidence, not an execution result."""
    root = {"attachment", "cwd", "entrypoint", "gitBranch", "isSidechain", "parentUuid", "sessionId", "timestamp", "type", "userType", "uuid", "version"}
    if set(obj) != root or not isinstance(obj.get("isSidechain"), bool):
        return False
    if any(not isinstance(obj[key], str) for key in root - {"attachment", "isSidechain", "parentUuid"}):
        return False
    if obj["parentUuid"] is not None and not isinstance(obj["parentUuid"], str):
        return False
    shapes = {
        "deferred_tools_delta": {"addedNames": "strings", "addedLines": "strings", "removedNames": "empty", "readdedNames": "empty", "pendingMcpServers": "empty", "needsAuthMcpServers": "empty"},
        "agent_listing_delta": {"addedTypes": "strings", "addedLines": "strings", "removedTypes": "empty", "isInitial": "bool", "showConcurrencyNote": "bool"},
        "mcp_instructions_delta": {"addedNames": "strings", "addedBlocks": "strings", "removedNames": "empty"},
        "skill_listing": {"content": "str", "skillCount": "count", "isInitial": "bool", "names": "strings"},
        "date_change": {"newDate": "str"},
    }
    value = obj.get("attachment")
    shape = shapes.get(value.get("type")) if isinstance(value, dict) and isinstance(value.get("type"), str) else None
    if shape is None or set(value) != set(shape) | {"type"}:
        return False
    checks = {"str": lambda v: isinstance(v, str), "bool": lambda v: isinstance(v, bool),
              "count": lambda v: isinstance(v, int) and not isinstance(v, bool) and v >= 0,
              "strings": lambda v: isinstance(v, list) and all(isinstance(x, str) for x in v),
              "empty": lambda v: isinstance(v, list) and not v}
    return all(checks[kind](value[key]) for key, kind in shape.items())



def full_identity(objects):
    """The full parsed census supplements, and never enlarges, metadata sampling."""
    metadata = reader_metadata([json.dumps(obj) for obj in objects])
    identity = dict(metadata["identities"])
    child, invalid = False, bool(metadata["parse_errors"])
    for obj in objects:
        p = obj.get("payload", {}) if obj.get("type") in ("session_meta", "turn_context") else obj
        if not isinstance(p, dict):
            invalid = True
            continue
        # Inspect every alias, including explicit empty values; precedence must
        # not hide another supplied session, parent or version identity.
        fields = [("cwd", "cwd"), ("sessionId", "session"), ("parent_session_id", "parent_session"),
                  ("parentSessionId", "parent_session"), ("cli_version", "cli_version"), ("version", "cli_version")]
        if obj.get("type") == "session_meta":
            fields.append(("id", "session"))
        for field, key in fields:
            if field not in p:
                continue
            value = p[field]
            if key == "parent_session" and value is None:
                continue  # Native optional parent absence is not a malformed ID.
            if not isinstance(value, str) or not value:
                invalid = True
            elif value not in identity[key]:
                identity[key].append(value)
        if "source" in p:
            source = p["source"]
            invalid = invalid or not isinstance(source, (str, dict)) or not source
            child = child or source == "subagent" or (isinstance(source, dict) and "subagent" in source)
            if isinstance(source, dict):
                invalid = invalid or set(source) != {"subagent"}
                subagent = source.get("subagent")
                spawn = subagent.get("thread_spawn") if isinstance(subagent, dict) else None
                if isinstance(spawn, dict) and "parent_thread_id" in spawn:
                    value = spawn["parent_thread_id"]
                    if not isinstance(value, str) or not value:
                        invalid = True
                    elif value not in identity["parent_session"]:
                        identity["parent_session"].append(value)
        if "isSidechain" in obj:
            invalid = invalid or not isinstance(obj["isSidechain"], bool)
            child = child or obj["isSidechain"] is True
    missing = any(not identity[key] for key in ("cwd", "session"))
    child = child or bool(identity["parent_session"])
    identity.update(child_context=child, metadata_missing=missing,
                    invalid=bool(invalid or missing or (child and not identity["parent_session"])))
    return identity


def prepare_records(objects, platform):
    diagnostics = dict(platform=platform, unsupported=platform not in ("claude", "codex", "pi", "omp"), unknown_records=0,
                       omitted=0, truncated=0, identity_conflicts=0, unmatched_calls=0, unmatched_results=0,
                       unknown_results=0, tool_observations=[], unknown_record_types={})
    diagnostics["full_identity"] = full_identity(objects)
    diagnostics["identity_conflicts"] += sum(len(diagnostics["full_identity"][key]) > 1 for key in ("cwd", "session", "parent_session", "source", "cli_version"))
    for obj in objects:
        p = obj.get("payload") if platform == "codex" else obj.get("message")
        is_message = (platform == "codex" and obj.get("type") == "response_item" and isinstance(p, dict) and p.get("type") == "message") or (platform == "claude" and obj.get("type") in ("user", "assistant"))
        if is_message:
            inspect_message_content(p.get("content") if isinstance(p, dict) else None, platform, diagnostics)
        elif platform == "codex" and obj.get("type") == "event_msg" and isinstance(p, dict) and p.get("type") in ("user_message", "agent_message"):
            inspect_message_content(p.get("message"), platform, diagnostics)
    if platform == "codex":
        known = {"session_meta", "turn_context", "response_item", "event_msg", "compacted", "world_state", "inter_agent_communication_metadata", "token_usage_record"}
        payloads = {"message", "function_call", "function_call_output", "custom_tool_call", "custom_tool_call_output", "reasoning",
                    "user_message", "agent_message", "agent_reasoning", "token_count", "task_started", "task_complete", "turn_aborted",
                    "exec_command_begin", "exec_command_end", "exec_command_output_delta", "context_compacted", "additional_tools", "thread_settings_applied", "item_completed"}
        canonical_messages = [obj for obj in objects if obj.get("type") == "response_item" and isinstance(obj.get("payload"), dict) and obj["payload"].get("type") == "message" and obj["payload"].get("role") == "assistant"]
        for obj in objects:
            if obj.get("type") not in known or (obj.get("type") in ("response_item", "event_msg") and (not isinstance(obj.get("payload"), dict) or obj["payload"].get("type") not in payloads)):
                unknown_record(diagnostics, obj)
            p = obj.get("payload", {})
            if obj.get("type") == "event_msg" and isinstance(p, dict) and p.get("type") == "item_completed":
                item = p.get("item", {})
                if known_completed_user(obj):
                    inspect_message_content(message_text(item["content"]), platform, diagnostics)
                elif known_web_extension(obj):
                    diagnostics["omitted"] += 1
                elif not isinstance(item, dict) or item.get("type") not in ("Reasoning", "AgentMessage", "SubAgentActivity", "CommandExecution", "McpToolCall", "FileChange", "ContextCompaction"):
                    unknown_record(diagnostics, obj)
                elif item.get("type") in ("CommandExecution", "McpToolCall", "FileChange") and item.get("status") not in ("completed", "failed"):
                    unknown_record(diagnostics, obj)
                elif item.get("type") == "ContextCompaction":
                    diagnostics["omitted"] += 1
                elif item.get("type") == "AgentMessage":
                    contents = [item[key] for key in ("content", "text") if key in item] or [None]
                    texts = []
                    for content in contents:
                        if isinstance(content, list):
                            content = [{**block, "type": "output_text"} if isinstance(block, dict) and block.get("type") == "Text" else block for block in content]
                        inspect_message_content(content, platform, diagnostics)
                        texts.append(message_text(content))
                    diagnostics["identity_conflicts"] += int(len(set(texts)) > 1)
                    matched = all(any(text and text == message_text(message["payload"].get("content")) and ((item.get("id") and item["id"] == message["payload"].get("id")) or (obj.get("timestamp") and obj["timestamp"] == message.get("timestamp"))) for message in canonical_messages) for text in texts)
                    if not matched:
                        diagnostics["omitted"] += 1
        objects = codex_records(objects, diagnostics)
    elif platform == "claude":
        known = {"user", "assistant", "system", "progress", "file-history-snapshot", "queue-operation", "summary", "last-prompt", "custom-title", "agent-name", "agent-color", "pr-link", "saved_hook_context", "bridge_status"}
        for obj in objects:
            if obj.get("type") == "attachment" and known_context_attachment(obj):
                diagnostics["omitted"] += 1
            elif obj.get("type") not in known:
                unknown_record(diagnostics, obj)
        calls = {}
        for obj in objects:
            message = obj.get("message", {})
            if not isinstance(message, dict):
                diagnostics["unknown_records"] += 1
                continue
            content = message.get("content", [])
            for block in content if isinstance(content, list) else []:
                if not isinstance(block, dict):
                    diagnostics["unknown_records"] += 1
                    continue
                if block.get("type") == "tool_use":
                    identity = block.get("id")
                    if not isinstance(identity, str):
                        diagnostics["unmatched_calls"] += 1
                    elif identity in calls:
                        diagnostics["identity_conflicts"] += 1
                    else:
                        calls[identity] = block
                elif block.get("type") == "tool_result":
                    identity = block.get("tool_use_id")
                    if not isinstance(identity, str):
                        identity = None
                    call = calls.pop(identity, None)
                    if not call:
                        diagnostics["unmatched_results"] += 1
                    inp = (call or {}).get("input", {})
                    diagnostics["tool_observations"].append({"call_id": identity, "name": (call or {}).get("name", "unknown"), "executed_cwd": inp.get("cwd", inp.get("workdir")) if isinstance(inp, dict) else None, "status": "error" if block.get("is_error") else "ok", "timestamp": obj.get("timestamp"), "result_digest": hashlib.sha256(json.dumps(block.get("content"), sort_keys=True, separators=(",", ":")).encode()).hexdigest()})
                    diagnostics["omitted"] += result_evidence(block.get("content"))[2]
        diagnostics["unmatched_calls"] += len(calls)
    elif platform not in ("pi", "omp", "cursor"):
        for obj in objects:
            unknown_record(diagnostics, obj)
    # The rendered skeleton is deliberately bounded; report every lossy excerpt.
    for obj in objects:
        if obj.get("type") == "_reader_tool":
            diagnostics["truncated"] += int(len(obj["target"]) > 120 or len(str(obj["output"])) > 200)
        else:
            p = obj.get("payload", obj.get("message", {}))
            if isinstance(p, dict):
                content = p.get("content", "")
                diagnostics["truncated"] += int(len(message_text(content)) > 800)
                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "tool_use":
                            inp = block.get("input", {})
                            if isinstance(inp, dict):
                                diagnostics["truncated"] += int(any(isinstance(v, str) and len(v) > 120 for v in inp.values()))
                        elif isinstance(block, dict) and block.get("type") == "tool_result":
                            diagnostics["truncated"] += int(len(str(block.get("content", ""))) > 200)
    return objects, diagnostics



def record_fingerprint(obj):
    """The selector names parsed content, independently of JSON whitespace/key order."""
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")).hexdigest()


def record_links(obj, platform):
    """Only native identities close a range; no body/relevance heuristics."""
    links, missing = set(), 0
    def link(kind, value, required=False):
        nonlocal missing
        if isinstance(value, str) and value:
            links.add((kind, value))
        elif required or value is not None:
            missing += 1
    if platform == "codex":
        normalized = completed_item(obj)
        p = normalized.get("payload", {})
        if not isinstance(p, dict):
            return links, 1
        kind = p.get("type")
        if kind in ("function_call", "custom_tool_call", "function_call_output", "custom_tool_call_output", "exec_command_begin", "exec_command_end", "exec_command_output_delta"):
            link("tool", p.get("call_id"), True)
        if kind == "message":
            link("message", p.get("id"))
            if obj.get("timestamp") and p.get("role") in ("user", "assistant"):
                link("message-alias", json.dumps([p["role"], obj["timestamp"], message_text(p.get("content"))]))
        elif kind in ("user_message", "agent_message") and obj.get("timestamp"):
            link("message", p.get("id"))
            link("message-alias", json.dumps(["user" if kind == "user_message" else "assistant", obj["timestamp"], p.get("message")]))
        elif kind == "item_completed":
            item = p.get("item", {})
            if isinstance(item, dict) and item.get("type") == "AgentMessage":
                link("message", item.get("id"))
                content = item.get("content", item.get("text"))
                if isinstance(content, list):
                    content = [{**b, "type": "output_text"} if isinstance(b, dict) and b.get("type") == "Text" else b for b in content]
                if obj.get("timestamp"):
                    link("message-alias", json.dumps(["assistant", obj["timestamp"], message_text(content)]))
    elif platform == "claude":
        link("record", obj.get("uuid"))
        msg = obj.get("message", {})
        if isinstance(msg, dict):
            content = msg.get("content", [])
            for block in content if isinstance(content, list) else []:
                if isinstance(block, dict) and block.get("type") in ("tool_use", "tool_result"):
                    link("tool", block.get("id" if block["type"] == "tool_use" else "tool_use_id"), True)
    return links, missing


def boundary_headers(objects):
    import re
    def identifier(value, limit=256):
        return value if isinstance(value, str) and len(value) <= limit and re.fullmatch(r"[A-Za-z0-9_.:-]+", value) else None
    rows = []
    for obj in objects[:1000]:
        p = obj.get("payload") if isinstance(obj.get("payload"), dict) else obj.get("message", {})
        p = p if isinstance(p, dict) else {}
        kind = identifier(obj.get("type"), 64) or "unknown"
        if obj.get("type") in ("response_item", "event_msg"):
            kind += ":" + (identifier(p.get("type"), 64) or "unknown")
        timestamp = obj.get("timestamp")
        timestamp = timestamp if isinstance(timestamp, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}T[0-9:.+-]{8,30}Z?", timestamp) else None
        row = {"fingerprint": record_fingerprint(obj), "kind": kind, "timestamp": timestamp}
        item = p.get("item") if isinstance(p.get("item"), dict) else {}
        native_id = identifier(obj.get("uuid") or p.get("call_id") or p.get("id") or item.get("id"))
        role = p.get("role", obj.get("type"))
        if p.get("type") in ("user_message", "agent_message"):
            role = "user" if p["type"] == "user_message" else "assistant"
        elif item.get("type") in ("UserMessage", "AgentMessage"):
            role = "user" if item["type"] == "UserMessage" else "assistant"
        if native_id:
            row["native_id"] = native_id
        if role in ("user", "assistant", "system", "developer"):
            row["role"] = role
        rows.append(row)
    return rows


def prepare_window(objects, platform, start, end, parse_errors=0, boundaries=False):
    """Keep global uncertainty; exclude only records proven outside a native ID closure."""
    import re
    _, whole = prepare_records(objects, platform)
    identities = reader_metadata([json.dumps(obj) for obj in objects])
    links, unknown_links = [], int(whole["full_identity"]["invalid"])
    for obj in objects:
        keys, missing = record_links(obj, platform)
        links.append(keys)
        unknown_links += missing
    # Claude's sequential renderer cannot reconcile repeated tool IDs itself.
    # Compare every same-ID fact before a selected window can hide a disagreement.
    facts = {}
    if platform == "claude":
        for obj in objects:
            msg = obj.get("message", {})
            content = msg.get("content", []) if isinstance(msg, dict) else []
            for block in content if isinstance(content, list) else []:
                if isinstance(block, dict) and block.get("type") in ("tool_use", "tool_result"):
                    key = (block["type"], block.get("id" if block["type"] == "tool_use" else "tool_use_id"))
                    if isinstance(key[1], str):
                        facts.setdefault(key, set()).add(record_fingerprint(block))
    # Reusing a native record/message identity for different content is global,
    # even when every conflicting copy lies outside the requested range.
    for obj in objects:
        p = obj.get("payload", {})
        native_id = obj.get("uuid") if platform == "claude" else p.get("id") if isinstance(p, dict) and p.get("type") == "message" else None
        if isinstance(native_id, str) and native_id:
            facts.setdefault(("record", native_id), set()).add(json.dumps(obj, sort_keys=True, separators=(",", ":")))
    conflicts = sum(len(values) > 1 for values in facts.values())
    global_diag = {key: whole[key] for key in ("unsupported", "identity_conflicts", "unknown_records", "unmatched_calls", "unmatched_results")}
    global_diag["unsupported"] = platform not in ("claude", "codex")
    global_diag["identity_conflicts"] += conflicts
    global_diag.update(parse_errors=parse_errors + identities["parse_errors"], unknown_links=unknown_links, selection_errors=0, metadata_missing=identities["metadata_missing"])
    fingerprints = []
    for obj in objects:
        try:
            fingerprints.append(record_fingerprint(obj))
        except (ValueError, UnicodeError):
            fingerprints.append(None)
            global_diag["parse_errors"] += 1
    if boundaries:
        meta = {**whole, "parse_errors": global_diag["parse_errors"], "global": global_diag,
                "boundaries": {"total": len(objects), "returned": min(1000, len(objects)), "truncated": max(0, len(objects) - 1000)}}
        meta["tool_observations"] = []
        valid_objects = [obj for obj, fp in zip(objects, fingerprints) if fp is not None]
        rows = boundary_headers(valid_objects)
        meta["boundaries"]["returned"] = len(rows)
        return rows, meta
    reason, selected, range_count = None, set(), 0
    if not all(isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value) for value in (start, end)):
        reason = "invalid-fingerprint"
    elif fingerprints.count(start) != 1 or fingerprints.count(end) != 1:
        reason = "boundary-not-unique"
    else:
        first, last = fingerprints.index(start), fingerprints.index(end)
        if first > last:
            reason = "boundary-order"
        else:
            selected = set(range(first, last + 1))
            range_count = len(selected)
    if reason:
        global_diag["selection_errors"] += 1
    # Transitive closure operates on whole records, including multiple calls in one message.
    by_link = {}
    for i, keys in enumerate(links):
        for key in keys:
            by_link.setdefault(key, set()).add(i)
    pending = list(selected)
    while pending:
        i = pending.pop()
        for key in links[i]:
            for other in by_link[key] - selected:
                selected.add(other)
                pending.append(other)
    chosen, diag = prepare_records([obj for i, obj in enumerate(objects) if i in selected], platform)
    _, outside = prepare_records([obj for i, obj in enumerate(objects) if i not in selected], platform)
    outside.pop("tool_observations", None)
    outside.pop("full_identity", None)
    diag["full_identity"] = whole["full_identity"]
    outside["parse_errors"] = 0  # Parse failures cannot be located outside a window.
    diag["unsupported"] = global_diag["unsupported"]
    for key in ("identity_conflicts", "unknown_records", "unmatched_calls", "unmatched_results"):
        diag[key] = max(diag[key], global_diag[key])
    diag["unknown_records"] += unknown_links + global_diag["selection_errors"] + int(global_diag["metadata_missing"])
    diag["parse_errors"] = global_diag["parse_errors"]
    diag["global"] = global_diag
    diag["outside"] = outside
    diag["selection"] = {"mode": "window", "start_record": start, "end_record": end, "valid": reason is None, "reason": reason,
                         "total_records": len(objects), "range_records": range_count, "selected_records": len(selected),
                         "closure_records": len(selected) - range_count, "outside_records": len(objects) - len(selected), "complete_session": False}
    return chosen, diag



# File input keeps a bounded index of identities, never a copy of the transcript.
# Its absolute deadline belongs to the caller and is shared by every intake step.
class StreamLimit(ValueError):
    pass


def stream_arguments(parser):
    parser.add_argument("--file-input")
    parser.add_argument("--prefix-bytes", type=int)
    parser.add_argument("--prefix-sha256")
    parser.add_argument("--deadline-ms", type=int)
    parser.add_argument("--since")
    parser.add_argument("--until")
    parser.add_argument("--boundary-role", choices=("user", "assistant", "system", "developer"))


class PrefixSource:
    def __init__(self, args):
        import time
        self.started = time.monotonic()
        self.path, self.size = args.file_input, args.prefix_bytes
        self.expected = args.prefix_sha256
        if not self.path or not os.path.isabs(self.path):
            raise StreamLimit("explicit-file-required")
        if not isinstance(self.size, int) or not 0 < self.size <= 1024 * 1024 * 1024:
            raise StreamLimit("source-limit")
        if not isinstance(args.deadline_ms, int):
            raise StreamLimit("absolute-deadline-required")
        self.deadline = min(self.started + 180, self.started + args.deadline_ms / 1000 - time.time())
        self.bytes, self.passes, self.records = 0, 0, 0
        self.sha, self.appended = None, None
        self.identity = None
        self.check()

    def check(self):
        import time
        if time.monotonic() >= self.deadline:
            raise StreamLimit("scan-timeout")

    def rows(self):
        import stat
        self.check()
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(self.path, flags)
        with os.fdopen(fd, "rb") as source:
            observed = os.fstat(source.fileno())
            identity = (observed.st_dev, observed.st_ino)
            if not stat.S_ISREG(observed.st_mode) or observed.st_size < self.size:
                raise StreamLimit("source-unavailable")
            if self.identity is not None and identity != self.identity:
                raise StreamLimit("source-replaced")
            self.identity = identity
            sha, offset, ordinal = hashlib.sha256(), 0, 0
            self.passes += 1
            while offset < self.size:
                self.check()
                raw = source.readline(min(MAX_BYTES + 1, self.size - offset))
                if not raw:
                    raise StreamLimit("prefix-short-read")
                self.bytes += len(raw)
                if len(raw) > MAX_BYTES:
                    raise StreamLimit("record-limit")
                sha.update(raw)
                start = offset
                offset += len(raw)
                if not raw.endswith(b"\n"):
                    raise StreamLimit("incomplete-record")
                if not raw.strip():
                    continue
                try:
                    obj = json.loads(raw.decode("utf-8"))
                    if not isinstance(obj, dict):
                        raise ValueError("object required")
                except (UnicodeError, ValueError, RecursionError):
                    obj = None
                self.records = ordinal + 1
                yield ordinal, start, len(raw), obj
                ordinal += 1
            after = os.fstat(source.fileno())
            path_after = os.stat(self.path, follow_symlinks=False)
            if after.st_size < self.size or (path_after.st_dev, path_after.st_ino) != identity:
                raise StreamLimit("source-replaced")
            value = sha.hexdigest()
            if (self.expected and value != self.expected) or (self.sha and value != self.sha):
                raise StreamLimit("prefix-changed")
            self.sha = value
            self.appended = max(0, after.st_size - self.size)
            self.records = ordinal
            self.check()

    def report(self, index=None):
        import resource
        import time
        rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return {"coverage": {"prefix_bytes": self.size, "prefix_sha256": self.sha,
                             "appended_bytes": self.appended, "complete_source": self.sha is not None and self.appended == 0},
                "scan": {"bytes": self.bytes, "passes": self.passes,
                         "elapsed_ms": int((time.monotonic() - self.started) * 1000),
                         "peak_rss_bytes": int(rss if sys.platform == "darwin" else rss * 1024),
                         "index_entries": index.entries if index else 0,
                         "index_bytes": index.bytes if index else 0, "additional_disk_bytes": 0}}


def stream_digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()).hexdigest()


STREAM_FACT_FIELDS = ("codes", "flags", "status", "name", "command", "cwd", "output", "aggregated_output", "stderr", "native_status")
STREAM_INDEX_ENTRIES = 400000
STREAM_INDEX_BYTES = 48 * 1024 * 1024


class StreamIndex:
    def __init__(self, closure=True):
        self.entries, self.bytes = 0, 0
        from array import array
        self.closure = closure
        self.atoms, self.parent, self.records, self.seeds = {}, array("I"), array("Q"), set()
        self.calls, self.results, self.messages, self.agent_messages = {}, {}, {}, []
        self.identities = {k: [] for k in ("cwd", "session", "parent_session", "source", "cli_version")}
        self.invalid, self.child = False, False
        self.unknown, self.parse_errors, self.unknown_links = 0, 0, 0
        self.conflicts = 0
        self.types = {}
        self.omitted, self.truncated = 0, 0

    def reserve(self, size, entries=1):
        # This is a logical retained-index budget, not a process RSS limit.
        # Process high-water memory is reported independently after rendering.
        if self.entries + entries > STREAM_INDEX_ENTRIES or self.bytes + size > STREAM_INDEX_BYTES:
            raise StreamLimit("index-limit")
        self.entries += entries
        self.bytes += size

    def charge(self, value):
        self.reserve(256 + len(json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode()))

    def atom(self, value):
        # Intern binary SHA256 values once. Maps, facts and graph edges retain
        # integer handles, rather than repeated hex strings or source bodies.
        value = bytes.fromhex(stream_digest(value))
        known = self.atoms.get(value)
        if known is not None:
            return known
        self.reserve(160)
        identity = len(self.parent)
        self.atoms[value] = identity
        self.parent.append(identity)
        return identity

    def root(self, key):
        while self.parent[key] != key:
            self.parent[key] = self.parent[self.parent[key]]
            key = self.parent[key]
        return key

    def add_links(self, ordinal, obj, platform, selected):
        links, missing = record_links(obj, platform)
        self.unknown_links += missing
        # Boundary listing still validates every native link, but needs no
        # range closure graph. Cause/result reconciliation is independent below.
        if not self.closure:
            return
        keys = sorted(self.atom(key) for key in links)
        if keys:
            for key in keys[1:]:
                self.parent[self.root(key)] = self.root(keys[0])
            self.reserve(8)
            self.records.append((ordinal << 32) | keys[0])
            if selected and keys[0] not in self.seeds:
                self.reserve(40)
                self.seeds.add(keys[0])

    def add_identity(self, obj):
        value = full_identity([obj])
        # Missing identity on an ordinary row is normal. Invalid types are not.
        raw = obj.get("payload", {}) if obj.get("type") in ("session_meta", "turn_context") else obj
        if not isinstance(raw, dict):
            self.invalid = True
        else:
            for field in ("cwd", "sessionId", "parent_session_id", "parentSessionId", "cli_version", "version"):
                if field in raw and not (field in ("parent_session_id", "parentSessionId") and raw[field] is None):
                    self.invalid |= not isinstance(raw[field], str) or not raw[field]
            if obj.get("type") == "session_meta":
                self.invalid |= not isinstance(raw.get("id"), str) or not raw.get("id")
            if "source" in raw:
                source = raw["source"]
                self.invalid |= not isinstance(source, (str, dict)) or not source
                if isinstance(source, dict):
                    self.invalid |= set(source) != {"subagent"}
                    subagent = source.get("subagent")
                    spawn = subagent.get("thread_spawn") if isinstance(subagent, dict) else None
                    if isinstance(spawn, dict) and "parent_thread_id" in spawn:
                        self.invalid |= not isinstance(spawn["parent_thread_id"], str) or not spawn["parent_thread_id"]
            if "isSidechain" in obj:
                self.invalid |= not isinstance(obj["isSidechain"], bool)
        self.child |= value["child_context"]
        for key in self.identities:
            for v in value[key]:
                if v not in self.identities[key]:
                    self.charge([key, v])
                    self.identities[key].append(v)

    def message(self, identity, signature):
        key = bytes.fromhex(stream_digest(identity))
        if key not in self.messages:
            self.reserve(160)
            self.messages[key] = signature
        elif self.messages[key] != signature:
            self.conflicts += 1

    def add(self, obj, platform):
        self.add_identity(obj)
        _, diag = prepare_records([obj], platform)
        self.conflicts += diag["identity_conflicts"] - sum(len(diag["full_identity"][k]) > 1 for k in self.identities)
        self.unknown += diag["unknown_records"]
        for key, value in diag["unknown_record_types"].items():
            self.types[key if key in self.types or len(self.types) < 31 else "other"] = self.types.get(key, 0) + value
        self.omitted += diag["omitted"]
        self.truncated += diag["truncated"]
        p = completed_item(obj).get("payload", {})
        if platform == "codex" and isinstance(p, dict):
            kind = p.get("type")
            if kind == "message" and obj.get("type") == "response_item":
                signature = self.atom([p.get("role"), message_text(p.get("content"))])
                if p.get("id"):
                    self.message(["message", p["id"]], signature)
                    self.message(["canonical-record", p["id"]], self.atom(obj))
                if obj.get("timestamp"):
                    self.message(["alias", p.get("role"), obj["timestamp"], message_text(p.get("content"))], signature)
            elif kind == "user_message" and isinstance(p.get("id"), str):
                self.message(["message", p["id"]], self.atom(["user", p.get("message")]))
            elif kind == "item_completed" and isinstance(p.get("item"), dict) and p["item"].get("type") == "AgentMessage":
                item = p["item"]
                candidates = []
                for content in [item[k] for k in ("text", "content") if k in item]:
                    if isinstance(content, list):
                        content = [{**b, "type": "output_text"} if isinstance(b, dict) and b.get("type") == "Text" else b for b in content]
                    text = message_text(content)
                    candidates.append((bytes.fromhex(stream_digest(["message", item.get("id")])), bytes.fromhex(stream_digest(["alias", "assistant", obj.get("timestamp"), text])), self.atom(["assistant", text])) if text else (None, None, -1))
                self.reserve(48 + 176 * len(candidates))
                self.agent_messages.append(candidates)
            if obj.get("type") == "response_item" and kind in ("function_call", "custom_tool_call"):
                identity = p.get("call_id")
                if not isinstance(identity, str) or not identity:
                    self.unknown_links += 1
                    return
                key = self.atom(("tool", identity))
                arguments = p.get("arguments", p.get("input", {}))
                if isinstance(arguments, str):
                    try: arguments = json.loads(arguments)
                    except ValueError: arguments = {}
                arguments = arguments if isinstance(arguments, dict) else {}
                command = arguments.get("cmd", arguments.get("command"))
                if isinstance(command, list): command = command[-1] if command else None
                value = (self.atom(p), tuple(self.atom(arguments[k]) for k in ("workdir", "cwd") if isinstance(arguments.get(k), str)), self.atom(command) if command else None)
                if key in self.calls and self.calls[key] != value: self.conflicts += 1
                elif key not in self.calls:
                    self.reserve(128); self.calls[key] = value
            elif ((obj.get("type") == "response_item" and kind in ("function_call_output", "custom_tool_call_output"))
                  or (obj.get("type") == "event_msg" and kind == "exec_command_end")):
                identity = p.get("call_id")
                if not isinstance(identity, str) or not identity:
                    self.unknown_links += 1
                    return
                key = self.atom(("tool", identity))
                facts = tool_facts(p)
                fields = {k: self.atom(v) for k, v in facts.items() if k != "omitted" and not (k in ("codes", "flags") and not v) and not (k == "status" and v == "unknown")}
                if key not in self.results:
                    from array import array
                    self.reserve(160)
                    self.results[key] = array("q", [-1] * len(STREAM_FACT_FIELDS) + [0])
                group = self.results[key]
                group[-1] |= int(kind != "exec_command_end" or p.get("requires_association", False))
                for k, value in fields.items():
                    at = STREAM_FACT_FIELDS.index(k)
                    if group[at] == -1: group[at] = value
                    elif group[at] != value: self.conflicts += 1
        elif platform == "claude":
            if isinstance(obj.get("uuid"), str) and obj["uuid"]:
                self.message(["record", obj["uuid"]], self.atom(obj))
            msg = obj.get("message", {})
            content = msg.get("content", []) if isinstance(msg, dict) else []
            for b in content if isinstance(content, list) else []:
                if not isinstance(b, dict) or b.get("type") not in ("tool_use", "tool_result"): continue
                identity = b.get("id" if b["type"] == "tool_use" else "tool_use_id")
                if not isinstance(identity, str) or not identity:
                    self.unknown_links += 1; continue
                key = self.atom(("tool", identity))
                table = self.calls if b["type"] == "tool_use" else self.results
                value = self.atom(b)
                if key in table and table[key] != value: self.conflicts += 1
                elif key not in table:
                    self.reserve(80); table[key] = value

    def finish(self, platform):
        if platform == "codex":
            for key, group in self.results.items():
                call = self.calls.get(key, (None, (), None))
                cwds = set(call[1])
                cwd, command = group[STREAM_FACT_FIELDS.index("cwd")], group[STREAM_FACT_FIELDS.index("command")]
                if cwd != -1: cwds.add(cwd)
                self.conflicts += int(len(cwds) > 1)
                if call[2] is not None and command != -1 and call[2] != command:
                    self.conflicts += 1
            for candidates in self.agent_messages:
                if candidates and all(self.messages.get(mid) == sig or self.messages.get(alias) == sig for mid, alias, sig in candidates):
                    self.omitted -= 1
        missing = any(not self.identities[k] for k in ("cwd", "session"))
        identity = {**self.identities, "child_context": bool(self.child or self.identities["parent_session"]), "metadata_missing": missing,
                    "invalid": bool(self.invalid or missing or (self.child and not self.identities["parent_session"]))}
        conflicts = self.conflicts + sum(len(v) > 1 for v in self.identities.values())
        unmatched_results = sum(k not in self.calls and (platform == "claude" or v[-1]) for k, v in self.results.items())
        return {"unsupported": platform not in ("claude", "codex"), "identity_conflicts": conflicts, "unknown_records": self.unknown,
                "unmatched_calls": len(set(self.calls) - set(self.results)), "unmatched_results": unmatched_results,
                "parse_errors": self.parse_errors, "unknown_links": self.unknown_links + int(identity["invalid"]),
                "selection_errors": 0, "metadata_missing": missing}, identity


def stream_failure(reason, source=None, index=None):
    diag = {"platform": "unknown", "unsupported": True, "unknown_records": 1, "omitted": 0, "truncated": 0,
            "identity_conflicts": 0, "unmatched_calls": 0, "unmatched_results": 0, "unknown_results": 0,
            "tool_observations": [], "unknown_record_types": {}, "parse_errors": 0, "incomplete_reason": reason,
            "full_identity": {"cwd": [], "session": [], "parent_session": [], "source": [], "cli_version": [],
                              "invalid": True, "metadata_missing": True, "child_context": False}}
    diag["global"] = {k: diag[k] for k in ("unsupported", "unknown_records", "identity_conflicts", "unmatched_calls", "unmatched_results", "parse_errors")}
    diag["global"].update(unknown_links=1, selection_errors=1, metadata_missing=True)
    if source:
        diag.update(source.report(index))
        diag["coverage"].update(complete_source=False, appended_bytes=None)
    return {"platform": "unknown", "objects": [], "boundaries": [], "meta": diag, "lines": source.records if source else 0}


def stream_window(args):
    import datetime
    source, index = None, StreamIndex(closure=not getattr(args, "boundaries_json", False))
    try:
        source = PrefixSource(args)
        import re
        if not isinstance(args.prefix_sha256, str) or not re.fullmatch(r"[a-f0-9]{64}", args.prefix_sha256):
            raise StreamLimit("prefix-hash-required")
        boundaries = getattr(args, "boundaries_json", False)
        if not boundaries and not (args.start_record and args.end_record):
            raise StreamLimit("explicit-window-required")
        def instant(value):
            if value is None: return None
            return datetime.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        since, until = instant(args.since), instant(args.until)
        if since is not None and until is not None and since > until: raise StreamLimit("invalid-period")
        platform, first, last, start_count, end_count = None, None, None, 0, 0
        headers, eligible, rows_count = [], 0, 0
        for ordinal, offset, length, obj in source.rows():
            rows_count += 1
            if obj is None:
                index.parse_errors += 1; continue
            if platform is None:
                if obj.get("type") in ("session_meta", "turn_context", "response_item", "event_msg"): platform = "codex"
                elif obj.get("type") in ("user", "assistant"): platform = "claude"
            if platform is None: raise StreamLimit("unsupported-leading-record")
            fp = record_fingerprint(obj)
            if fp in (args.start_record, args.end_record):
                stamp = instant(obj.get("timestamp"))
                if stamp is None or (since is not None and stamp < since) or (until is not None and stamp > until):
                    raise StreamLimit("boundary-outside-period")
            if fp == args.start_record:
                start_count += 1
                if first is None: first = ordinal
            if fp == args.end_record:
                end_count += 1
                if last is None: last = ordinal
            selected = first is not None and ordinal >= first and (last is None or ordinal <= last)
            index.add_links(ordinal, obj, platform, selected)
            index.add(obj, platform)
            if boundaries:
                h = boundary_headers([obj])[0]
                stamp = instant(h.get("timestamp"))
                if ((since is None or stamp is not None and stamp >= since) and (until is None or stamp is not None and stamp <= until)
                        and (args.boundary_role is None or h.get("role") == args.boundary_role)):
                    eligible += 1
                    if len(headers) < 1000: headers.append(h)
        global_diag, identity = index.finish(platform)
        if boundaries:
            _, diag = prepare_records([], platform)
            diag.update(global_diag, full_identity=identity)
            diag.update(omitted=index.omitted, truncated=index.truncated, unknown_record_types=index.types)
            diag["global"] = global_diag
            diag["boundaries"] = {"total": eligible, "returned": len(headers), "truncated": max(0, eligible - 1000), "source_records": rows_count}
            diag.update(source.report(index))
            return {"platform": platform, "objects": [], "boundaries": headers, "meta": diag, "lines": rows_count}
        if start_count != 1 or end_count != 1 or first > last:
            raise StreamLimit("boundary-not-unique-or-ordered")
        roots = {index.root(k) for k in index.seeds}
        selected_ordinals = {value >> 32 for value in index.records if index.root(value & 0xffffffff) in roots}
        # The first-pass index has served closure/global validation. Release its
        # containers before the second-pass outside accumulator is populated.
        for table in (index.atoms, index.calls, index.results, index.messages, index.agent_messages, index.seeds):
            table.clear()
        del index.parent[:]
        del index.records[:]
        chosen, chosen_bytes, selected_count = [], 0, 0
        outside = StreamIndex(closure=False)
        for ordinal, offset, length, obj in source.rows():
            if obj is None: continue
            if first <= ordinal <= last or ordinal in selected_ordinals:
                chosen_bytes += length
                if chosen_bytes > MAX_BYTES: raise StreamLimit("selection-limit")
                chosen.append(obj); selected_count += 1
            else:
                outside.add(obj, platform)
        normalized, diag = prepare_records(chosen, platform)
        outside_global, _ = outside.finish(platform)
        diag["outside"] = {**outside_global, "omitted": outside.omitted, "truncated": outside.truncated,
                           "unknown_record_types": outside.types, "unknown_results": 0}
        diag["full_identity"] = identity
        diag["global"] = global_diag
        for key in ("identity_conflicts", "unknown_records", "unmatched_calls", "unmatched_results"):
            diag[key] = max(diag[key], global_diag[key])
        diag["unknown_records"] += global_diag["unknown_links"]
        diag["parse_errors"] = global_diag["parse_errors"]
        diag["selection"] = {"mode": "window", "start_record": args.start_record, "end_record": args.end_record,
                             "valid": True, "reason": None, "total_records": rows_count, "range_records": last - first + 1,
                             "selected_records": selected_count, "closure_records": selected_count - (last - first + 1),
                             "outside_records": rows_count - selected_count, "complete_session": False}
        diag.update(source.report(index if index.bytes >= outside.bytes else outside))
        return {"platform": platform, "objects": normalized, "meta": diag, "lines": rows_count}
    except (ValueError, OSError, UnicodeError, TypeError, RecursionError) as error:
        reason = str(error) if isinstance(error, StreamLimit) else "invalid-stream-input"
        return stream_failure(reason, source, index)


def stream_metadata(args):
    source = None
    try:
        source = PrefixSource(args)
        lines, tail, head_bytes = [], [], 0
        for ordinal, offset, length, obj in source.rows():
            if obj is None: raise StreamLimit("malformed-record")
            encoded = json.dumps(obj)
            if len(lines) < MAX_LINES:
                head_bytes += len(encoded.encode())
                if head_bytes > MAX_BYTES: raise StreamLimit("metadata-head-limit")
                lines.append(encoded)
            # Retain only timestamps, never trailing message bodies.
            stamp = obj.get("timestamp")
            if isinstance(stamp, str): tail = [(offset, stamp)]
        result = extract_from_lines(lines) or {"platform": "unknown"}
        result.update(reader_metadata(lines))
        for key in ("source", "cli_version"):
            if len(result["identities"][key]) == 1: result[key] = result["identities"][key][0]
        result.update(file=source.path, size=source.size, oversized=False)
        if tail and tail[0][0] >= max(0, source.size - TAIL_BYTES): result["last_ts"] = tail[0][1]
        report = source.report()
        return {"sessions": [result], "meta": {"files_processed": 1, "parse_errors": 0, **report}}
    except (ValueError, OSError, UnicodeError, TypeError, RecursionError) as error:
        reason = str(error) if isinstance(error, StreamLimit) else "invalid-stream-input"
        return {"sessions": [], "meta": {"files_processed": 1, "parse_errors": 1, "incomplete_reason": reason, **(source.report() if source else {})}}


def bounded_json(value):
    import resource
    encoded = json.dumps(value)
    oversized = len(encoded.encode()) > MAX_BYTES
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    rss = int(rss if sys.platform == "darwin" else rss * 1024)
    observed = value.get("meta", {}).get("scan")
    if observed is not None:
        observed["peak_rss_bytes"] = max(observed["peak_rss_bytes"], rss)
    if oversized:
        failure = stream_failure("output-limit")
        if observed is not None: failure["meta"]["scan"] = observed
        value = {"text": "", "boundaries": [], "meta": failure["meta"]}
    del encoded
    encoded = json.dumps(value)
    if len(encoded.encode()) > MAX_BYTES:
        failure = stream_failure("output-limit")
        if observed is not None: failure["meta"]["scan"] = observed
        return json.dumps({"text": "", "boundaries": [], "meta": failure["meta"]})
    return encoded


def main():
    # Parse arguments: files and optional --cwd-filter / --keyword
    files = []
    cwd_filter = None
    keywords = None
    max_bytes = MAX_BYTES
    args = sys.argv[1:]
    i = 0
    while i < len(args):
        if args[i] == "--max-bytes" and i + 1 < len(args):
            max_bytes = int(args[i + 1])
            if max_bytes <= 0:
                raise ValueError("max bytes must be positive")
            i += 2
        elif args[i] == "--cwd-filter" and i + 1 < len(args):
            cwd_filter = args[i + 1]
            i += 2
        elif args[i] == "--keyword" and i + 1 < len(args):
            keywords = [k for k in args[i + 1].split(",") if k]
            i += 2
        elif not args[i].startswith("-"):
            files.append(args[i])
            i += 1
        else:
            i += 1

    if files:
        # Batch mode: process all files
        processed = 0
        parse_errors = 0
        filtered = 0
        matched = 0
        for filepath in files:
            if not filepath.endswith(".jsonl"):
                continue
            result, error = process_file(filepath, max_bytes)
            processed += 1
            if result:
                # Apply CWD filter first: cheap metadata-only check. Skip
                # sessions from other repos before paying the full-file keyword
                # scan cost — Claude and Codex discovery list across projects,
                # so without this ordering --keyword would scan files that are
                # immediately discarded.
                if cwd_filter:
                    session_cwd = result.get("cwd")
                    if session_cwd:
                        if not cwd_matches_filter(session_cwd, cwd_filter):
                            filtered += 1
                            continue
                    elif result.get("platform") in ("claude", "codex", "pi", "omp"):
                        filtered += 1
                        continue
                if not result.get("oversized"):
                    _attach_timestamps(result, filepath)
                # Apply keyword scan only after cheap filters pass.
                if keywords:
                    matches = count_keyword_matches(filepath, keywords)
                    result["keyword_matches"] = matches
                    result["match_count"] = sum(matches.values())
                    if result["match_count"] == 0:
                        continue
                    matched += 1
                print(json.dumps(result))
            elif error:
                parse_errors += 1

        meta = {"_meta": True, "files_processed": processed, "parse_errors": parse_errors}
        if filtered:
            meta["filtered_by_cwd"] = filtered
        if keywords:
            meta["files_matched"] = matched
        print(json.dumps(meta))
    else:
        # No file arguments: either single-file stdin mode or empty xargs invocation.
        # When xargs runs us with no input (e.g., discover found no files), stdin is
        # empty or a TTY — emit a clean zero-file result instead of a false parse error.
        if sys.stdin.isatty():
            lines = []
        else:
            lines = list(sys.stdin)

        if not lines:
            # No input at all — zero-file result (clean exit for empty pipelines).
            # When --keyword was supplied, emit files_matched: 0 so callers relying
            # on its presence to terminate quickly in zero-match scans see a
            # consistent shape with the batch-mode no-match case.
            meta = {"_meta": True, "files_processed": 0, "parse_errors": 0}
            if keywords:
                meta["files_matched"] = 0
            print(json.dumps(meta))
        else:
            # Genuine single-file stdin mode (backward compatible)
            result = extract_from_lines(lines) or {"platform": "unknown"}
            result.update(reader_metadata(lines))
            if result:
                print(json.dumps(result))
            print(json.dumps({"_meta": True, "files_processed": 1, "parse_errors": 0 if result else 1}))


if __name__ == "__main__":
    if "--file-input" in sys.argv:
        import argparse
        parser = argparse.ArgumentParser()
        parser.add_argument("--json", action="store_true", required=True)
        stream_arguments(parser)
        print(bounded_json(stream_metadata(parser.parse_args())))
        sys.exit(0)
    if "--json" in sys.argv:
        import io
        original = sys.stdout
        sys.stdout = io.StringIO()
        main()
        rows = [json.loads(line) for line in sys.stdout.getvalue().splitlines()]
        sys.stdout = original
        print(json.dumps({"sessions": [r for r in rows if not r.get("_meta")], "meta": rows[-1]}))
    else:
        main()
