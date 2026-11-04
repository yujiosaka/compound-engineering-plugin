import { expect, test } from "bun:test"
import { join } from "node:path"

// Synthetic wire records only; never discover or read a host's session files.
const scripts = process.env.SESSION_HISTORY_SCRIPT_DIR ?? join(import.meta.dir, "../skills/ce-compound/scripts/session-history")
const timestamp = "2025-01-01T00:00:00Z"
const meta = { type: "session_meta", timestamp, payload: { id: "session", cwd: "/synthetic", source: "cli", cli_version: "synthetic", timestamp } }
const completed = (item: unknown) => ({ type: "event_msg", timestamp, payload: { type: "item_completed", thread_id: "session", turn_id: "turn", item, started_at_ms: 1, completed_at_ms: 2 } })
const user = (text = "Keep the saved value", id = "user-1") => completed({ type: "UserMessage", id, content: [{ type: "text", text, text_elements: [] }] })
const message = (text = "Keep the saved value", id = "user-1") => ({ type: "response_item", timestamp, payload: { type: "message", role: "user", id, content: [{ type: "input_text", text }] } })
const extension = () => completed({ type: "Extension", kind: "web.search", id: "search-1", query: "synthetic", action: { type: "openPage", url: "https://example.test" }, results: [{ type: "text_result", domain: "example.test", ref_id: "ref", snippet: "synthetic", title: "synthetic", url: "https://example.test" }] })
async function run(reader: string, rows: unknown[], args: string[] = []) {
  const proc = Bun.spawn(["python3", join(scripts, reader), "--json", ...args], { stdin: new TextEncoder().encode(rows.map(x => JSON.stringify(x)).join("\n")), stdout: "pipe", stderr: "pipe" })
  const [out, err, code] = await Promise.all([new Response(proc.stdout).text(), new Response(proc.stderr).text(), proc.exited])
  expect(err).toBe("")
  expect(code).toBe(0)
  return JSON.parse(out)
}
async function window(reader: string, rows: unknown[], at: number) {
  const listed = await run("extract-skeleton.py", rows, ["--boundaries-json"])
  const fp = listed.boundaries[at].fingerprint
  return run(reader, rows, ["--start-record", fp, "--end-record", fp])
}
for (const reader of ["extract-skeleton.py", "extract-errors.py"]) {
  test(`${reader}: completed user messages render alone and reconcile canonical aliases`, async () => {
    for (const rows of [[meta, user()], [meta, message(), user()], [meta, user(), message()], [meta, user(), user()]]) {
      const out = await run(reader, rows)
      expect(out.meta.unknown_records).toBe(0)
      expect(out.meta.omitted).toBe(0)
      expect(out.meta.identity_conflicts).toBe(0)
      if (reader === "extract-skeleton.py") expect(out.meta.user).toBe(1)
    }
    const out = await window(reader, [meta, user(), message()], 1)
    expect(out.meta.selection.selected_records).toBe(2)
    expect(out.meta.global.unknown_links).toBe(0)
    expect(out.meta.unknown_records).toBe(0)
    const conflicting = await window(reader, [meta, message(), user("Changed text")], 1)
    expect(conflicting.meta.global.identity_conflicts).toBeGreaterThan(0)
    const long = await run(reader, [meta, user("x".repeat(801))])
    expect(long.meta.truncated).toBeGreaterThan(0)
    expect((await run(reader, [meta, user("[REDACTED]")])).meta.omitted).toBeGreaterThan(0)
  })
  test(`${reader}: unknown completed user shapes remain global failures`, async () => {
    const base = user() as any
    const mutations = [
      (x: any) => { x.payload.extra = true },
      (x: any) => { x.payload.item.extra = true },
      (x: any) => { x.payload.thread_id = 1 },
      (x: any) => { x.payload.started_at_ms = true },
      (x: any) => { delete x.payload.item.id },
      (x: any) => { x.payload.item.content = "unsupported" },
      (x: any) => { x.payload.item.content[0].text_elements = ["unknown"] },
      (x: any) => { x.payload.item.content[0].type = "future" },
      (x: any) => { x.payload.item.content[0].text = 1 },
      (x: any) => { x.payload.item.content[0].extra = true },
    ]
    for (const mutate of mutations) {
      const value = structuredClone(base); mutate(value)
      const out = await window(reader, [meta, message(), value], 1)
      expect(out.meta.global.unknown_records).toBeGreaterThan(0)
    }
  })
  test(`${reader}: known web extension is omitted only outside the selected window`, async () => {
    for (const thumbnail of [false, true]) {
      const value = extension() as any
      if (thumbnail) value.payload.item.results[0].thumbnail_url = "https://example.test/image"
      const rows = [meta, message(), value]
      expect((await run(reader, rows)).meta.omitted).toBe(1)
      const outside = await window(reader, rows, 1)
      expect(outside.meta.omitted).toBe(0)
      expect(outside.meta.outside.omitted).toBe(1)
      expect(outside.meta.global.unknown_records).toBe(0)
      const inside = await window(reader, rows, 2)
      expect(inside.meta.omitted).toBe(1)
      expect(inside.meta.tool_observations).toEqual([])
    }
  })
  test(`${reader}: web extension shape drift cannot hide outside a window`, async () => {
    const mutations = [
      (x: any) => { x.payload.extra = true },
      (x: any) => { x.payload.item.kind = "future" },
      (x: any) => { x.payload.item.extra = true },
      (x: any) => { x.payload.item.id = 7 },
      (x: any) => { x.payload.item.query = [] },
      (x: any) => { x.payload.item.action.type = "search" },
      (x: any) => { x.payload.item.action.extra = true },
      (x: any) => { x.payload.item.results = {} },
      (x: any) => { x.payload.item.results[0].type = "future" },
      (x: any) => { x.payload.item.results[0].extra = true },
      (x: any) => { x.payload.item.results[0].snippet = [] },
      (x: any) => { x.payload.item.results[0].thumbnail_url = null },
      (x: any) => { delete x.payload.item.results[0].ref_id },
    ]
    for (const mutate of mutations) {
      const value = extension() as any; mutate(value)
      const out = await window(reader, [meta, message(), value], 1)
      expect(out.meta.global.unknown_records).toBeGreaterThan(0)
    }
  })
}

const nativeExtras = [
  { type: "UserMessage", id: "u-client", client_id: "client", content: [{ type: "text", text: "saved value", text_elements: [] }] },
  { type: "CollabAgentToolCall", id: "wait", tool: "wait", status: "completed", sender_thread_id: "session", receiver_thread_ids: [], receiver_agents: [], agents_states: {} },
  { type: "Extension", id: "sleep", kind: "clock.sleep", durationMs: 10 },
  { type: "ImageView", id: "image", path: "/synthetic/image.png" },
  { type: "FunctionCallOutput", id: "native-output", name: "synthetic", namespace: "synthetic", output: '{"isError":false,"content":[{"type":"text","text":"done"}]}' },
  ...[
    { type: "search", query: "synthetic", queries: null },
    { type: "findInPage", pattern: "synthetic", url: null },
    { type: "openPage", url: "https://example.test" },
  ].map((action, i) => ({ type: "Extension", kind: "web.search", id: `web-${i}`, query: "synthetic", action, results: [{ type: "text_result", domain: "example.test", ref_id: "ref", snippet: "synthetic", url: "https://example.test" }] })),
]

async function parity(rows: unknown[], at: number) {
  const { mkdtemp, writeFile, rm } = await import("node:fs/promises")
  const { tmpdir } = await import("node:os")
  const { createHash } = await import("node:crypto")
  const root = await mkdtemp(join(tmpdir(), "native-grammar-"))
  const file = join(root, "history.jsonl")
  const content = Buffer.from(rows.map(x => JSON.stringify(x)).join("\n") + "\n")
  await writeFile(file, content)
  const headers = await run("extract-skeleton.py", rows, ["--boundaries-json"])
  const select = ["--start-record", headers.boundaries[at].fingerprint, "--end-record", headers.boundaries[at].fingerprint]
  try {
    const outputs = []
    for (const reader of ["extract-skeleton.py", "extract-errors.py"]) {
      const buffered = await run(reader, rows, select)
      const streamed = await run(reader, [], [...select, "--file-input", file, "--prefix-bytes", String(content.length), "--prefix-sha256", createHash("sha256").update(content).digest("hex"), "--deadline-ms", String(Date.now() + 30000)])
      expect(streamed.text).toBe(buffered.text)
      expect(streamed.meta.global).toEqual(buffered.meta.global)
      expect(streamed.meta.selection).toEqual(buffered.meta.selection)
      expect(streamed.meta.tool_observations).toEqual(buffered.meta.tool_observations)
      outputs.push(streamed)
    }
    return outputs
  } finally { await rm(root, { recursive: true, force: true }) }
}

for (const item of nativeExtras) {
  test(`typed native ${item.type}/${"kind" in item ? item.kind : item.id} has buffered/streamed parity`, async () => {
    for (const out of await parity([meta, message(), completed(item)], 2)) {
      expect(out.meta.global.unknown_records).toBe(0)
      expect(out.meta.global.unknown_links).toBe(0)
      expect(out.meta.global.unmatched_results).toBe(0)
      if (["Extension", "ImageView", "CollabAgentToolCall"].includes(item.type)) expect(out.meta.omitted).toBeGreaterThan(0)
      if (item.type === "FunctionCallOutput") expect(out.meta.tool_observations[0]).toMatchObject({ call_id: null, native_item_id: item.id, executed_cwd: null, status: "ok" })
    }
  })
  test(`unknown ${item.type}/${item.id} fields and identity stay global`, async () => {
    for (const changed of [{ ...item, future: true }, { ...item, id: 7 }]) {
      for (const out of await parity([meta, message(), completed(changed)], 1)) expect(out.meta.global.unknown_records).toBeGreaterThan(0)
    }
  })
}
test("native grammar rejects changed nested types, unknown child identities and extension kinds", async () => {
  const mutations = [
    { ...nativeExtras[0], client_id: null },
    { ...nativeExtras[1], sender_thread_id: null },
    { ...nativeExtras[1], receiver_thread_ids: ["unknown-child"] },
    { ...nativeExtras[1], agents_states: { child: { status: "completed" } } },
    { ...nativeExtras[2], durationMs: true },
    { ...nativeExtras[2], kind: "future.extension" },
    { ...nativeExtras[3], path: {} },
    { ...nativeExtras[4], namespace: null },
    { ...nativeExtras[4], output: 42 },
  ]
  for (const changed of mutations) {
    for (const out of await parity([meta, message(), completed(changed)], 1)) expect(out.meta.global.unknown_records).toBeGreaterThan(0)
  }
})
test("native item IDs do not close over identically named outer calls", async () => {
  const call = { type: "response_item", timestamp, payload: { type: "function_call", call_id: "same", name: "exec", arguments: JSON.stringify({ cmd: "check", workdir: "/synthetic" }) } }
  const result = { type: "response_item", timestamp, payload: { type: "function_call_output", call_id: "same", output: { exit_code: 0 } } }
  const native = completed({ type: "CommandExecution", id: "same", status: "failed", exit_code: 1, cwd: "/synthetic/other", command: ["check"] })
  for (const out of await parity([meta, call, result, native], 3)) {
    expect(out.meta.selection.selected_records).toBe(1)
    expect(out.meta.global.identity_conflicts).toBe(0)
    expect(out.meta.global.unmatched_results).toBe(0)
    expect(out.meta.tool_observations).toMatchObject([{ call_id: null, native_item_id: "same", executed_cwd: "/synthetic/other", status: "error" }])
  }
  for (const out of await parity([meta, call, native], 2)) expect(out.meta.global.unmatched_calls).toBe(1)
})
test("native output aliases use only proven native item IDs and preserve conflicting results", async () => {
  const item = nativeExtras[4]
  const alias = { type: "response_item", timestamp, payload: { ...item, type: "function_call_output", internal_chat_message_metadata_passthrough: null } }
  for (const out of await parity([meta, completed(item), alias], 1)) {
    expect(out.meta.selection.selected_records).toBe(2)
    expect(out.meta.global.unknown_links + out.meta.global.identity_conflicts).toBe(0)
    expect(out.meta.tool_observations).toHaveLength(1)
  }
  for (const out of await parity([meta, completed(item), { ...alias, payload: { ...alias.payload, output: '{"isError":true}' } }], 1)) expect(out.meta.global.identity_conflicts).toBeGreaterThan(0)
})

test("MCP wrapper and execution-header text arrays compare only known equivalent content", async () => {
  const call = { type: "response_item", timestamp, payload: { type: "function_call", call_id: "mcp", name: "mcp", arguments: "{}" } }
  const wrapper = { type: "event_msg", timestamp, payload: { type: "exec_command_end", call_id: "mcp", output: { content: [{ type: "text", text: "result" }], isError: false, _meta: { observation: "one" } } } }
  const output = [{ type: "input_text", text: "Wall time: 0.125 seconds\nOutput:" }, { type: "input_text", text: "result" }]
  const result = (value: unknown) => ({ type: "response_item", timestamp, payload: { type: "function_call_output", call_id: "mcp", output: value } })
  for (const out of await parity([meta, call, wrapper, result(output)], 3)) {
    expect(out.meta.global.identity_conflicts).toBe(0)
    expect(out.meta.tool_observations[0].status).toBe("ok")
  }
  for (const changed of [
    [{ ...output[0], text: "unknown header" }, output[1]],
    [output[0], { ...output[1], text: "different" }],
    [output[0], { ...output[1], extra: true }],
    { ...wrapper.payload.output, isError: true },
    { ...wrapper.payload.output, _meta: { observation: "different" } },
    { ...wrapper.payload.output, future: true },
  ]) {
    for (const out of await parity([meta, call, wrapper, result(changed)], 3)) expect(out.meta.global.identity_conflicts).toBeGreaterThan(0)
  }
})

test("native passthrough metadata is typed, compared, and unknown values stay global", async () => {
  const item = nativeExtras[4]
  const output = (metadata: unknown) => ({ type: "response_item", timestamp, payload: { ...item, type: "function_call_output", internal_chat_message_metadata_passthrough: metadata } })
  const observed = { turn_id: "turn", create_time: 1700000000.125 }
  for (const out of await parity([meta, completed(item), output(observed)], 1)) expect(out.meta.global.unknown_records + out.meta.global.identity_conflicts).toBe(0)
  for (const invalid of [42, { future_field: { isError: true } }, { ...observed, create_time: true }, { ...observed, turn_id: null }, { ...observed, extra: true }]) {
    for (const out of await parity([meta, message(), output(invalid)], 1)) expect(out.meta.global.unknown_records).toBeGreaterThan(0)
  }
  for (const out of await parity([meta, output(observed), output({ ...observed, turn_id: "other" })], 1)) expect(out.meta.global.identity_conflicts).toBeGreaterThan(0)
})

test("native envelope and sender identities cannot impersonate the admitted session", async () => {
  const foreign = completed(nativeExtras[2]); foreign.payload.thread_id = "foreign"
  for (const out of await parity([meta, message(), foreign], 1)) expect(out.meta.global.identity_conflicts).toBeGreaterThan(0)
  for (const out of await parity([meta, message(), completed({ ...nativeExtras[1], sender_thread_id: "foreign" })], 1)) expect(out.meta.global.unknown_records).toBeGreaterThan(0)
})

test("native turn contradictions and malformed thread/status values remain diagnostic", async () => {
  const item = nativeExtras[4]
  const alias = { type: "response_item", timestamp, payload: { ...item, type: "function_call_output", internal_chat_message_metadata_passthrough: { turn_id: "other", create_time: 1.25 } } }
  for (const out of await parity([meta, completed(item), alias], 1)) expect(out.meta.global.identity_conflicts).toBeGreaterThan(0)
  const other = completed(item); other.payload.turn_id = "other"
  for (const out of await parity([meta, completed(item), other], 1)) expect(out.meta.global.identity_conflicts).toBeGreaterThan(0)
  for (const thread of [null, "", 42]) {
    const malformed: any = completed({ type: "CommandExecution", id: "native", status: "completed", exit_code: 0, cwd: "/synthetic" }); malformed.payload.thread_id = thread
    for (const out of await parity([meta, message(), malformed], 1)) expect(out.meta.global.unknown_links).toBeGreaterThan(0)
  }
  for (const status of [{}, []]) {
    for (const out of await parity([meta, message(), completed({ type: "CommandExecution", id: "native", status, exit_code: 0 })], 1)) expect(out.meta.global.unknown_records).toBeGreaterThan(0)
  }
  const huge = JSON.stringify({ ...alias, payload: { ...alias.payload, internal_chat_message_metadata_passthrough: { turn_id: "turn", create_time: "HUGE" } } }).replace('"HUGE"', "1" + "0".repeat(310))
  const proc = Bun.spawn(["python3", join(scripts, "extract-skeleton.py"), "--json"], { stdin: new TextEncoder().encode(JSON.stringify(meta) + "\n" + huge), stdout: "pipe", stderr: "pipe" })
  const parsed = JSON.parse(await new Response(proc.stdout).text())
  expect(await proc.exited).toBe(0)
  expect(parsed.meta.unknown_records).toBeGreaterThan(0)
})

test("native turn and namespace identities compare exact strings without JSON coercion", async () => {
  for (const key of ["turn_id", "namespace"]) {
    const first: any = completed({ ...nativeExtras[4] }), second: any = completed({ ...nativeExtras[4] })
    if (key === "turn_id") { first.payload.turn_id = "1e0"; second.payload.turn_id = "1.0" }
    else { first.payload.item.namespace = "1e0"; second.payload.item.namespace = "1.0" }
    for (const out of await parity([meta, first, second], 1)) expect(out.meta.global.identity_conflicts).toBeGreaterThan(0)
  }
})
