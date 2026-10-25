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


def completed_item(obj):
    """Keep native execution evidence without guessing its outer tool call."""
    p = obj.get("payload", {})
    if obj.get("type") != "event_msg" or not isinstance(p, dict) or p.get("type") != "item_completed":
        return obj
    item = p.get("item", {})
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
    calls, results, messages, normalized, aliases = {}, {}, {}, [], {}
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
    emitted = set()
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


def prepare_records(objects, platform):
    diagnostics = dict(platform=platform, unsupported=platform not in ("claude", "codex", "pi", "omp"), unknown_records=0,
                       omitted=0, truncated=0, identity_conflicts=0, unmatched_calls=0, unmatched_results=0,
                       unknown_results=0, tool_observations=[], unknown_record_types={})
    diagnostics["identity_conflicts"] += reader_metadata([json.dumps(obj) for obj in objects])["identity_conflicts"]
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
                if not isinstance(item, dict) or item.get("type") not in ("Reasoning", "AgentMessage", "SubAgentActivity", "CommandExecution", "McpToolCall", "FileChange"):
                    unknown_record(diagnostics, obj)
                elif item.get("type") in ("CommandExecution", "McpToolCall", "FileChange") and item.get("status") not in ("completed", "failed"):
                    unknown_record(diagnostics, obj)
                elif item.get("type") == "AgentMessage":
                    inspect_message_content(item.get("content"), platform, diagnostics)
                    text = message_text(item.get("content"))
                    matched = any(text and text == message_text(message["payload"].get("content")) and ((item.get("id") and item["id"] == message["payload"].get("id")) or (obj.get("timestamp") and obj["timestamp"] == message.get("timestamp"))) for message in canonical_messages)
                    if not matched:
                        diagnostics["omitted"] += 1
        objects = codex_records(objects, diagnostics)
    elif platform == "claude":
        known = {"user", "assistant", "system", "progress", "file-history-snapshot", "queue-operation", "summary", "last-prompt", "custom-title", "agent-name", "agent-color", "pr-link", "saved_hook_context", "bridge_status"}
        for obj in objects:
            if obj.get("type") not in known:
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
