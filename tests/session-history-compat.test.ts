import { describe, expect, test } from "bun:test"
import { mkdtemp, mkdir, writeFile, rm, utimes } from "node:fs/promises"
import { tmpdir } from "node:os"
import { join } from "node:path"

// All records are synthetic. No local session history is read by this suite.
const scripts = process.env.SESSION_HISTORY_SCRIPT_DIR ?? join(import.meta.dir, "../skills/ce-compound/scripts/session-history")
const ts = "2025-01-01T00:00:00Z"
const cwd = "/synthetic/allowed"
const meta = { type: "session_meta", timestamp: ts, payload: { id: "session-1", cwd, timestamp: ts, source: "cli", cli_version: "synthetic" } }
const row = (type: string, payload: unknown) => ({ type, timestamp: ts, payload })
const call = (id: string, custom = false, args: unknown = { cmd: "check synthetic", workdir: cwd }) => row("response_item", { type: custom ? "custom_tool_call" : "function_call", call_id: id, name: "exec_command", [custom ? "input" : "arguments"]: JSON.stringify(args) })
const result = (id: string, output: unknown, custom = false) => row("response_item", { type: custom ? "custom_tool_call_output" : "function_call_output", call_id: id, output })
const jsonl = (rows: unknown[]) => rows.map(value => JSON.stringify(value)).join("\n") + "\n"
async function run(name: string, rows: unknown[], extra: string[] = [], tail = "") {
  const proc = Bun.spawn(["python3", join(scripts, name), "--json", ...extra], { stdin: new TextEncoder().encode(jsonl(rows) + tail), stdout: "pipe", stderr: "pipe" })
  const [stdout, stderr, code] = await Promise.all([new Response(proc.stdout).text(), new Response(proc.stderr).text(), proc.exited])
  expect(stderr).toBe("")
  expect(code).toBe(0)
  return JSON.parse(stdout)
}
async function owned(body: (root: string) => Promise<void>) {
  const root = await mkdtemp(join(tmpdir(), "session-reader-compat-"))
  try { await body(root) } finally { await rm(root, { recursive: true, force: true }) }
}

describe("native result grammar and explicit incomplete evidence", () => {
  for (const custom of [false, true]) {
    for (const output of [{ exit_code: 1, output: "synthetic failure" }, [{ type: "text", text: '{"exit_code":1,"output":"synthetic failure"}' }], { results: [{ content: [{ type: "text", text: '{"result":{"exit_code":1}}' }] }] }]) {
      test(`${custom ? "custom" : "function"} structured ${JSON.stringify(output)}`, async () => {
        const rows = [meta, call("failed", custom), result("failed", output, custom), call("passed", custom), result("passed", { exit_code: 0 }, custom)]
        const skeleton = await run("extract-skeleton.py", rows)
        const errors = await run("extract-errors.py", rows)
        expect(skeleton.meta.tool).toBe(2)
        expect(errors.meta.errors_found).toBe(1)
        expect(skeleton.meta.tool_observations.map((o: any) => o.status)).toEqual(["error", "ok"])
        expect(skeleton.meta.tool_observations[0].executed_cwd).toBe(cwd)
        expect(skeleton.meta.tool_observations[0].result_digest).toMatch(/^[a-f0-9]{64}$/)
        expect(skeleton.meta.unmatched_calls + skeleton.meta.unmatched_results).toBe(0)
      })
    }
  }
  test("plain exit_code mention is unknown, never an executed failure", async () => {
    const out = await run("extract-errors.py", [meta, call("text"), result("text", "documentation says exit_code: 1")])
    expect(out.meta.errors_found).toBe(0)
    expect(out.meta.unknown_results).toBe(1)
  })
  test("native tool alias joins by call ID; user alias is counted once", async () => {
    const rows = [meta, row("response_item", { type: "message", role: "user", content: [{ type: "input_text", text: "Use the saved value" }] }), row("event_msg", { type: "user_message", message: "Use the saved value" }), call("a"), row("event_msg", { type: "exec_command_end", call_id: "a", exit_code: 1, cwd, command: ["check synthetic"] }), result("a", { exit_code: 1 })]
    const out = await run("extract-skeleton.py", rows)
    expect(out.meta.user).toBe(1)
    expect(out.meta.tool).toBe(1)
    expect(out.meta.identity_conflicts).toBe(0)
    const conflict = await run("extract-skeleton.py", [...rows, result("a", { exit_code: 0 })])
    expect(conflict.meta.identity_conflicts).toBeGreaterThan(0)
  })
  test("same user text at a later timestamp is a separate observation", async () => {
    const out = await run("extract-skeleton.py", [meta, row("response_item", { type: "message", role: "user", content: [{ type: "input_text", text: "Again" }] }), { ...row("event_msg", { type: "user_message", message: "Again" }), timestamp: "2025-01-01T00:01:00Z" }])
    expect(out.meta.user).toBe(2)
  })
  test("missing executed cwd never inherits session cwd; orphan IDs stay incomplete", async () => {
    const out = await run("extract-skeleton.py", [meta, call("unknown", false, { cmd: "check" }), result("unknown", { exit_code: 0 }), result("orphan", { exit_code: 0 }), call("unfinished")])
    expect(out.meta.tool_observations[0].executed_cwd).toBeNull()
    expect(out.meta.unmatched_results).toBe(1)
    expect(out.meta.unmatched_calls).toBe(1)
  })
  test("unknown format, malformed tail and unsupported record differ from supported zero", async () => {
    for (const name of ["extract-skeleton.py", "extract-errors.py"]) {
      expect((await run(name, [{ type: "future-format" }])).meta.unsupported).toBe(true)
      const good = await run(name, [meta])
      expect(good.meta.unsupported).toBe(false)
      expect(good.meta.parse_errors).toBe(0)
      expect(good.text).toBe("")
      expect((await run(name, [meta], [], '{"broken":')).meta.parse_errors).toBe(1)
      expect((await run(name, [meta, row("response_item", { type: "future-tool" })])).meta.unknown_records).toBe(1)
    }
  })
  test("overlong, redacted and explicitly omitted output stay visible", async () => {
    const out = await run("extract-skeleton.py", [meta, row("response_item", { type: "message", role: "user", content: [{ type: "input_text", text: "x".repeat(801) }] }), call("long", false, { cmd: "c".repeat(121) }), result("long", { exit_code: 1, output: "[REDACTED]" + "x".repeat(201), truncated: true })])
    expect(out.meta.truncated).toBeGreaterThan(0)
    expect(out.meta.omitted).toBeGreaterThan(0)
  })
  test("Claude native identity and result status survive structured output", async () => {
    const rows = [{ type: "assistant", cwd, sessionId: "child", message: { content: [{ type: "tool_use", id: "claude-call", name: "Bash", input: { command: "check", cwd } }] } }, { type: "user", cwd, sessionId: "child", message: { content: [{ type: "tool_result", tool_use_id: "claude-call", is_error: true, content: [{ type: "text", text: "synthetic failure" }] }] } }]
    const out = await run("extract-skeleton.py", rows)
    expect(out.meta.tool).toBe(1)
    expect(out.meta.tool_observations[0]).toMatchObject({ call_id: "claude-call", executed_cwd: cwd, status: "error" })
    expect((await run("extract-errors.py", rows)).meta.errors_found).toBe(1)
  })
  test("metadata preserves parent/source/unknown/conflicting IDs without content", async () => {
    await owned(async root => {
      const path = join(root, "record.jsonl")
      await writeFile(path, jsonl([{ ...meta, payload: { ...meta.payload, source: { subagent: { thread_spawn: { parent_thread_id: "parent" } } } } }, row("turn_context", { cwd: "/synthetic/other" }), row("response_item", { type: "message", role: "user", content: [{ type: "input_text", text: "PRIVATE SYNTHETIC CONTENT" }] })]))
      const out = await run("extract-metadata.py", [], [path])
      expect(out.sessions[0].parent_session).toBe("parent")
      expect(out.sessions[0].identity_conflicts).toBe(1)
      expect(out.sessions[0].metadata_missing).toBe(false)
      expect(out.sessions[0].metadata_limited).toBe(true)
      expect(JSON.stringify(out)).not.toContain("PRIVATE SYNTHETIC CONTENT")
      const oversized = await run("extract-metadata.py", [], ["--max-bytes", "20", path])
      expect(oversized.sessions[0].oversized).toBe(true)
      expect(oversized.sessions[0].metadata_missing).toBe(true)
      await writeFile(path, jsonl([{ type: "unknown" }]))
      expect((await run("extract-metadata.py", [], [path])).sessions[0].platform).toBe("unknown")
    })
  })
  test("discovery reads only the explicit root, finds nested Claude children and respects mtime", async () => {
    await owned(async root => {
      const parent = join(root, "sessions", "bucket", "parent")
      const child = join(parent, "subagents", "child.jsonl")
      await mkdir(join(parent, "subagents"), { recursive: true })
      await writeFile(child, jsonl([{ type: "user", sessionId: "child", cwd, message: { content: "synthetic correction" } }]))
      const old = join(parent, "old.jsonl")
      await writeFile(old, jsonl([meta]))
      await utimes(old, new Date(0), new Date(0))
      const outside = join(root, "outside.jsonl")
      await writeFile(outside, jsonl([meta]))
      const proc = Bun.spawn(["bash", join(scripts, "discover-sessions.sh"), "synthetic", "7", "--platform", "claude", "--session-root", join(root, "sessions")], { stdout: "pipe", stderr: "pipe" })
      const out = await new Response(proc.stdout).text()
      expect(await proc.exited).toBe(0)
      expect(out.trim()).toBe(child)
      const metadata = await run("extract-metadata.py", [], [child])
      expect(metadata.sessions[0].parent_session).toBe("parent")
      expect(metadata.sessions[0].parent_source).toBe("path")
    })
  })
})
