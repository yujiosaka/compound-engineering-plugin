import { describe, expect, test } from "bun:test"
import { createHash } from "node:crypto"
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
  test("native completed execution keeps cwd and failure while unknown association stays pending", async () => {
    const completed = (id: string, code: number) => row("event_msg", { type: "item_completed", item: { type: "CommandExecution", id, cwd, exit_code: code, command: "check synthetic", aggregated_output: "synthetic output", status: code ? "failed" : "completed" } })
    const rows = [meta, completed("native-failed", 1), completed("native-passed", 0)]
    const out = await run("extract-skeleton.py", rows)
    expect(out.meta.tool).toBe(2)
    expect(out.meta.unmatched_results).toBe(2)
    expect(out.meta.tool_observations.map((o: any) => [o.executed_cwd, o.status])).toEqual([[cwd, "error"], [cwd, "ok"]])
    expect((await run("extract-errors.py", rows)).meta.errors_found).toBe(1)
    const aliased = await run("extract-skeleton.py", [meta, call("same"), result("same", { exit_code: 0 }), completed("same", 0)])
    expect(aliased.meta.tool).toBe(1)
    expect(aliased.meta.unmatched_results).toBe(0)
    const nativeTools = await run("extract-skeleton.py", [meta, ...["McpToolCall", "FileChange"].map(type => row("event_msg", { type: "item_completed", item: { type, id: type, status: "failed", result: { isError: true } } }))])
    expect(nativeTools.meta.tool).toBe(2)
    expect(nativeTools.meta.unmatched_results).toBe(2)
    expect(nativeTools.meta.tool_observations.every((o: any) => o.status === "error")).toBe(true)
    const supplemental = await run("extract-skeleton.py", [meta, call("same"), result("same", "command response"), completed("same", 0)])
    expect(supplemental.meta.tool_observations[0].status).toBe("ok")
    const metadata = await run("extract-skeleton.py", [meta, row("response_item", { type: "additional_tools", tools: [] }), row("event_msg", { type: "thread_settings_applied" }), ...["world_state", "inter_agent_communication_metadata", "token_usage_record"].map(type => row(type, {})), ...["Reasoning", "SubAgentActivity"].map(type => row("event_msg", { type: "item_completed", item: { type } }))])
    expect(metadata.meta.unknown_records).toBe(0)
    expect(metadata.meta.tool).toBe(0)
  })
  test("all same-ID evidence is reconciled before alias dedup, independent of order", async () => {
    const native = (facts: object) => row("event_msg", { type: "item_completed", item: { type: "CommandExecution", id: "same", cwd, command: "check synthetic", ...facts } })
    const failed = native({ status: "failed" })
    const passed = native({ status: "completed", exit_code: 0 })
    for (const aliases of [[failed, passed], [passed, failed]]) {
      for (const canonical of [[], [result("same", { exit_code: 0 })]]) {
        const rows = [meta, call("same"), ...canonical, ...aliases]
        const skeleton = await run("extract-skeleton.py", rows)
        expect(skeleton.meta.tool).toBe(1)
        expect(skeleton.meta.tool_observations[0].status).toBe("error")
        expect(skeleton.meta.identity_conflicts).toBeGreaterThan(0)
        expect((await run("extract-errors.py", rows)).meta.errors_found).toBe(1)
      }
    }
    for (const changed of [{ tool: "DifferentTool" }, { command: "different synthetic command" }, { result: { isError: false, content: "different result" } }, { result: [] }, { result: "" }, { cwd: "/synthetic/other" }]) {
      const out = await run("extract-skeleton.py", [meta, call("same"), result("same", { exit_code: 0 }), native({ status: "completed", exit_code: 0, result: { isError: false, content: "first result" } }), native({ status: "completed", exit_code: 0, result: { isError: false, content: "first result" }, ...changed })])
      expect(out.meta.identity_conflicts).toBeGreaterThan(0)
    }
    const duplicateResults = await run("extract-errors.py", [meta, call("same"), result("same", { exit_code: 1 }), result("same", { exit_code: 0 })])
    expect(duplicateResults.meta.errors_found).toBe(1)
    expect(duplicateResults.meta.identity_conflicts).toBeGreaterThan(0)
  })
  test("completed AgentMessage requires a matching rendered canonical message", async () => {
    const message = row("response_item", { type: "message", role: "assistant", content: [{ type: "output_text", text: "Synthetic repair verified" }] })
    const completed = (content: unknown) => row("event_msg", { type: "item_completed", item: { type: "AgentMessage", content } })
    for (const name of ["extract-skeleton.py", "extract-errors.py"]) {
      expect((await run(name, [meta, completed("Synthetic repair verified")])).meta.omitted).toBeGreaterThan(0)
      expect((await run(name, [meta, message, completed("Different result")])).meta.omitted).toBeGreaterThan(0)
      expect((await run(name, [meta, message, completed("Synthetic repair verified")])).meta.omitted).toBe(0)
      expect((await run(name, [meta, completed({ content: "Synthetic repair verified" })])).meta.unknown_records).toBeGreaterThan(0)
      const textBlocks = completed([{ type: "Text", text: "Synthetic repair verified" }])
      expect((await run(name, [meta, message, textBlocks])).meta.unknown_records).toBe(0)
      expect((await run(name, [meta, message, textBlocks])).meta.omitted).toBe(0)
      expect((await run(name, [meta, textBlocks])).meta.omitted).toBeGreaterThan(0)
      const directText = row("event_msg", { type: "item_completed", item: { type: "AgentMessage", text: "Synthetic repair verified" } })
      expect((await run(name, [meta, message, directText])).meta.omitted).toBe(0)
      expect((await run(name, [meta, directText])).meta.omitted).toBeGreaterThan(0)
      const competing = row("event_msg", { type: "item_completed", item: { type: "AgentMessage", text: "Different text", content: [{ type: "Text", text: "Synthetic repair verified" }] } })
      expect((await run(name, [meta, message, competing])).meta.identity_conflicts).toBeGreaterThan(0)
      const compacted = await run(name, [meta, row("event_msg", { type: "item_completed", item: { type: "ContextCompaction", id: "compact" } })])
      expect(compacted.meta.unknown_records).toBe(0)
      expect(compacted.meta.omitted).toBe(1)
      expect((await run(name, [meta, completed([{ type: "Text" }])])).meta.unknown_records).toBeGreaterThan(0)
      expect((await run(name, [meta, completed([{ type: "Text", text: "[REDACTED]" }])])).meta.omitted).toBeGreaterThan(0)
    }
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
      const unknown = await run(name, [meta, row("response_item", { type: "future_tool" }), row("event_msg", { type: "unsafe arbitrary body / value" })])
      expect(unknown.meta.unknown_records).toBe(2)
      expect(unknown.meta.unknown_record_types).toEqual({ "response_item:future_tool": 1, "event_msg:unknown": 1 })
      expect(JSON.stringify(unknown.meta)).not.toContain("unsafe arbitrary body")
    }
  })
  test("unknown message blocks, invalid containers and missing text cannot become supported zero", async () => {
    for (const content of [[{ type: "future_text", text: "Synthetic correction" }], { content: [{ type: "input_text", text: "Synthetic correction" }] }, [{ type: "input_text" }], [null]]) {
      for (const name of ["extract-skeleton.py", "extract-errors.py"]) {
        const out = await run(name, [meta, row("response_item", { type: "message", role: "user", content })])
        expect(out.meta.unknown_records).toBeGreaterThan(0)
      }
    }
    const redacted = await run("extract-skeleton.py", [meta, row("response_item", { type: "message", role: "user", content: [{ type: "input_text", text: "[REDACTED]" }] })])
    expect(redacted.meta.omitted).toBe(1)
    const claude = await run("extract-skeleton.py", [{ type: "user", cwd, sessionId: "c", message: { content: [{ type: "future_text", text: "Synthetic correction" }] } }])
    expect(claude.meta.unknown_records).toBeGreaterThan(0)
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


describe("trusted evidence windows in the existing reader", () => {
  const user = (text: string, timestamp = ts) => ({ type: "response_item", timestamp, payload: { type: "message", role: "user", content: [{ type: "input_text", text }] } })
  const claude = (type: string, content: unknown, uuid: string) => ({ type, cwd, sessionId: "claude-synthetic", timestamp: ts, uuid, message: { role: type, content } })
  async function range(rows: unknown[], first: number, last = first) {
    const listed = await run("extract-skeleton.py", rows, ["--boundaries-json"])
    return ["--start-record", listed.boundaries[first].fingerprint, "--end-record", listed.boundaries[last].fingerprint]
  }
  test("boundary headers are canonical, bounded, body-free and cannot choose a window", async () => {
    const record = user("PRIVATE_BODY_NEVER_A_HEADER")
    const rows = [meta, record, call("header-id", false, {cmd: "PRIVATE_COMMAND", workdir:cwd})]
    const out = await run("extract-skeleton.py", rows, ["--boundaries-json"])
    expect(out.text).toBeUndefined()
    expect(out.meta.tool_observations).toEqual([])
    expect(JSON.stringify(out)).not.toContain("PRIVATE_BODY")
    expect(JSON.stringify(out)).not.toContain("PRIVATE_COMMAND")
    expect(out.boundaries[1]).toMatchObject({kind:"response_item:message",timestamp:ts,role:"user"})
    expect(out.boundaries[2].native_id).toBe("header-id")
    const unicode = await run("extract-skeleton.py", [meta, user("日本語")], ["--boundaries-json"])
    const canonical = '{"payload":{"content":[{"text":"日本語","type":"input_text"}],"role":"user","type":"message"},"timestamp":"2025-01-01T00:00:00Z","type":"response_item"}'
    expect(unicode.boundaries[1].fingerprint).toBe(createHash("sha256").update(canonical,"utf8").digest("hex"))
    const reordered = { payload: { content: record.payload.content, role:"user", type:"message" }, timestamp:ts, type:"response_item" }
    expect((await run("extract-skeleton.py", [meta, reordered], ["--boundaries-json"])).boundaries[1].fingerprint).toBe(out.boundaries[1].fingerprint)
    const many = await run("extract-skeleton.py", [meta, ...Array.from({length:1001}, (_,i)=>user(String(i)))], ["--boundaries-json"])
    expect(many.boundaries.length).toBe(1000)
    expect(many.meta.boundaries).toEqual({total:1002,returned:1000,truncated:2})
  })
  test("unrelated outside text, failures and omitted images never enter selected evidence", async () => {
    const picked = user("Use saved values")
    const rows = [meta, picked]
    const selection = await range(rows, 1)
    for(const reader of ["extract-skeleton.py","extract-errors.py"]) {
      const base = await run(reader,rows,selection)
      const extended = await run(reader,[meta,user("OUTSIDE_PRIVATE_".repeat(100)),picked,call("outside"),result("outside",{exit_code:1,output:"OUTSIDE_FAILURE"}),user("later"),row("response_item",{type:"message",role:"user",content:[{type:"input_image",image_url:"OUTSIDE_IMAGE"}]})],selection)
      expect(extended.text).toBe(base.text)
      expect(extended.meta.truncated).toBe(0)
      expect(extended.meta.omitted).toBe(0)
      expect(extended.meta.unknown_records).toBe(0)
      expect(extended.meta.tool_observations).toEqual([])
      expect(extended.meta.outside.truncated).toBeGreaterThan(0)
      expect(extended.meta.outside.omitted).toBeGreaterThan(0)
      expect(extended.meta.selection).toMatchObject({valid:true,range_records:1,selected_records:1,closure_records:0,outside_records:6,complete_session:false})
      expect(JSON.stringify(extended)).not.toContain("OUTSIDE_")
    }
  })
  test("selected long text and selected omitted evidence remain incomplete", async () => {
    for(const selected of [user("long".repeat(300)),row("response_item",{type:"message",role:"user",content:[{type:"input_image"}]})]) {
      const rows=[meta,selected], selection=await range(rows,1)
      for(const reader of ["extract-skeleton.py","extract-errors.py"]) {
        const out=await run(reader,rows,selection)
        expect(out.meta.truncated+out.meta.omitted).toBeGreaterThan(0)
      }
    }
  })
  test("missing duplicate reversed and malformed boundaries refuse without text", async () => {
    const rows=[meta,user("first"),user("last")],selection=await range(rows,1,2)
    for(const [input,args] of [[rows,["--start-record","0".repeat(64),"--end-record",selection[3]]],[[...rows,rows[1]],selection],[rows,["--start-record",selection[3],"--end-record",selection[1]]],[rows,["--start-record","invalid","--end-record",selection[3]]],[rows,["--start-record","","--end-record",""]]] as Array<[unknown[],string[]]>) {
      for(const reader of ["extract-skeleton.py","extract-errors.py"]) {
        const out=await run(reader,input,args)
        expect(out.text).toBe("")
        expect(out.meta.selection.valid).toBe(false)
        expect(out.meta.global.selection_errors).toBe(1)
        expect(out.meta.unknown_records).toBeGreaterThan(0)
      }
    }
  })
  test("both boundaries are inclusive and selection closes outside calls results and aliases", async () => {
    const alias=row("event_msg",{type:"exec_command_end",call_id:"linked",exit_code:1,cwd,command:["check synthetic"]})
    const rows=[meta,call("linked"),user("selected correction"),result("linked",{exit_code:1}),alias]
    for(const pair of [[1,2],[2,3],[3,3]]) {
      const selection=await range(rows,pair[0],pair[1])
      const skeleton=await run("extract-skeleton.py",rows,selection),errors=await run("extract-errors.py",rows,selection)
      expect(skeleton.meta.tool).toBe(1)
      expect(skeleton.meta.unmatched_calls+skeleton.meta.unmatched_results).toBe(0)
      expect(skeleton.meta.selection.closure_records).toBeGreaterThan(0)
      expect(skeleton.meta.tool_observations[0].status).toBe("error")
      expect(errors.meta.errors_found).toBe(1)
      expect(errors.meta.selection).toEqual(skeleton.meta.selection)
    }
  })
  test("outside same-ID failure and identity disagreements can never be excluded",async()=>{
    const rows=[meta,call("same"),result("same",{exit_code:0}),user("selected")],selection=await range(rows,1,2)
    for(const reader of ["extract-skeleton.py","extract-errors.py"]) {
      const out=await run(reader,[...rows,result("same",{exit_code:1})],selection)
      expect(out.meta.global.identity_conflicts).toBeGreaterThan(0)
      expect(out.meta.identity_conflicts).toBeGreaterThan(0)
      expect(out.meta.tool_observations[0].status).toBe("error")
      const unlinkedConflict=await run(reader,[...rows,call("other"),result("other",{exit_code:0}),result("other",{exit_code:1})],selection)
      expect(unlinkedConflict.meta.global.identity_conflicts).toBeGreaterThan(0)
      expect(unlinkedConflict.meta.identity_conflicts).toBeGreaterThan(0)
      expect(unlinkedConflict.meta.tool_observations.length).toBe(1)
    }
  })
  test("unknown grammar missing links metadata conflict and every parse error remain global",async()=>{
    const rows=[meta,user("selected")],selection=await range(rows,1)
    const extras=[row("response_item",{type:"future_call",output:"PRIVATE"}),call("unmatched"),row("response_item",{type:"function_call_output",output:{exit_code:0}}),{...meta,payload:{...meta.payload,cwd:"/synthetic/foreign"}}]
    for(const reader of ["extract-skeleton.py","extract-errors.py"]) {
      for(const extra of extras) {
        const out=await run(reader,[...rows,extra],selection)
        expect(out.meta.unknown_records+out.meta.unmatched_calls+out.meta.unmatched_results+out.meta.identity_conflicts).toBeGreaterThan(0)
        expect(JSON.stringify(out)).not.toContain("PRIVATE")
      }
      const malformed=await run(reader,rows,selection,'{"malformed":')
      expect(malformed.meta.global.parse_errors).toBe(1)
      expect(malformed.meta.parse_errors).toBe(1)
    }
  })
  test("Claude whole-record closure is transitive across multiple tool identities",async()=>{
    const start=claude("user","selected correction","u1")
    const a=claude("assistant",[{type:"tool_use",id:"a",name:"Bash",input:{command:"check",cwd}}],"a1")
    const b=claude("user",[{type:"tool_result",tool_use_id:"a",content:"ok"},{type:"tool_use",id:"b",name:"Bash",input:{command:"next",cwd}}],"u2")
    const c=claude("user",[{type:"tool_result",tool_use_id:"b",is_error:true,content:"failed"}],"u3")
    const rows=[claude("assistant","OUTSIDE_".repeat(200),"outside"),start,a,b,c],selection=await range(rows,1,2)
    for(const reader of ["extract-skeleton.py","extract-errors.py"]) {
      const out=await run(reader,rows,selection)
      expect(out.meta.selection).toMatchObject({range_records:2,selected_records:4,closure_records:2,outside_records:1})
      expect(out.meta.truncated).toBe(0)
      expect(out.meta.outside.truncated).toBe(1)
      expect(out.meta.tool_observations.map((v:any)=>v.call_id)).toEqual(["a","b"])
      expect(out.meta.tool_observations[1].status).toBe("error")
      expect(JSON.stringify(out)).not.toContain("OUTSIDE_")
    }
  })
  test("message aliases close by existing native identity or equal timestamp and text",async()=>{
    const canonical=user("same native message"), alias=row("event_msg",{type:"user_message",message:"same native message"})
    const rows=[meta,canonical,alias],selection=await range(rows,1)
    const out=await run("extract-skeleton.py",rows,selection)
    expect(out.meta.user).toBe(1)
    expect(out.meta.selection.closure_records).toBe(1)
    const different=await run("extract-skeleton.py",[meta,canonical,{...alias,timestamp:"2025-01-01T00:00:01Z"}],selection)
    expect(different.meta.selection.closure_records).toBe(0)
    const native=claude("user","outside","reused")
    const selected=claude("user","selected","selected")
    const crows=[native,selected],cselection=await range(crows,1)
    const conflict=await run("extract-skeleton.py",[...crows,{...native,message:{role:"user",content:"changed outside"}}],cselection)
    expect(conflict.meta.global.identity_conflicts).toBeGreaterThan(0)
  })
  test("unsupported platform and unknown message blocks outside cannot produce a ready window",async()=>{
    const rows=[meta,user("selected")],selection=await range(rows,1)
    const unknown=row("response_item",{type:"message",role:"user",content:[{type:"future_text",text:"OUTSIDE_PRIVATE"}]})
    for(const reader of ["extract-skeleton.py","extract-errors.py"]) {
      const out=await run(reader,[...rows,unknown],selection)
      expect(out.meta.global.unknown_records).toBeGreaterThan(0)
      expect(out.meta.unknown_records).toBeGreaterThan(0)
      expect(JSON.stringify(out)).not.toContain("OUTSIDE_PRIVATE")
      const unsupported=await run(reader,[{type:"session",id:"pi",cwd}],selection)
      expect(unsupported.meta.unsupported).toBe(true)
    }
  })
  test("exact known runtime context is omitted; unknown subtype field or container stays global",async()=>{
    const attachment=(value:object)=>({type:"attachment",attachment:value,cwd,entrypoint:"cli",gitBranch:"synthetic",isSidechain:false,parentUuid:null,sessionId:"claude-synthetic",timestamp:ts,userType:"external",uuid:"attachment",version:"synthetic"})
    const examples=[
      {type:"deferred_tools_delta",addedNames:["synthetic"],addedLines:["OUTSIDE_CONTEXT"],removedNames:[],readdedNames:[],pendingMcpServers:[],needsAuthMcpServers:[]},
      {type:"agent_listing_delta",addedTypes:["synthetic"],addedLines:["OUTSIDE_CONTEXT"],removedTypes:[],isInitial:true,showConcurrencyNote:false},
      {type:"mcp_instructions_delta",addedNames:["synthetic"],addedBlocks:["OUTSIDE_CONTEXT"],removedNames:[]},
      {type:"skill_listing",content:"OUTSIDE_CONTEXT",skillCount:1,isInitial:true,names:["synthetic"]},
      {type:"date_change",newDate:"OUTSIDE_CONTEXT"},
    ]
    const selected=claude("user","selected","u1"),selection=await range([selected],0)
    for(const value of examples) {
      const rows=[attachment(value),selected]
      for(const reader of ["extract-skeleton.py","extract-errors.py"]) {
        const whole=await run(reader,rows)
        expect(whole.meta.omitted).toBe(1)
        expect(whole.meta.unknown_records).toBe(0)
        const out=await run(reader,rows,selection)
        expect(out.meta.omitted).toBe(0)
        expect(out.meta.outside.omitted).toBe(1)
        expect(out.meta.unknown_records).toBe(0)
        expect(JSON.stringify(out)).not.toContain("OUTSIDE_CONTEXT")
        const included=await run(reader,rows,await range(rows,0,1))
        expect(included.meta.omitted).toBe(1)
      }
    }
    for(const unknown of [attachment({type:"future_context",content:"PRIVATE"}),{...attachment(examples[0]),call_id:"hidden"},attachment({...examples[0],result:{exit_code:1}}),attachment({...examples[0],addedLines:[{type:"tool_result",tool_use_id:"hidden"}]}),attachment({...examples[0],removedNames:["unobserved-element-type"]})]) {
      const out=await run("extract-skeleton.py",[unknown,selected],selection)
      expect(out.meta.global.unknown_records).toBeGreaterThan(0)
      expect(out.meta.unknown_records).toBeGreaterThan(0)
      expect(JSON.stringify(out)).not.toContain("PRIVATE")
    }
  })
  test("Claude duplicate tool results outside the range remain globally contradictory",async()=>{
    const rows=[claude("assistant",[{type:"tool_use",id:"a",name:"Bash",input:{command:"check",cwd}}],"a1"),claude("user",[{type:"tool_result",tool_use_id:"a",is_error:false,content:"ok"}],"u1"),claude("user","selected","u2")]
    const selection=await range(rows,2)
    const out=await run("extract-skeleton.py",[...rows,claude("user",[{type:"tool_result",tool_use_id:"a",is_error:true,content:"failed"}],"u3")],selection)
    expect(out.meta.global.identity_conflicts).toBeGreaterThan(0)
    expect(out.meta.identity_conflicts).toBeGreaterThan(0)
  })
})
