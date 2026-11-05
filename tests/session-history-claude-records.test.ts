import { expect, test } from "bun:test"
import { createHash } from "node:crypto"
import { mkdtemp, rm, writeFile } from "node:fs/promises"
import { tmpdir } from "node:os"
import { join } from "node:path"

// Synthetic wire records only; never discover or read a host's session files.
const scripts = process.env.SESSION_HISTORY_SCRIPT_DIR ?? join(import.meta.dir, "../skills/ce-compound/scripts/session-history")
const readers = ["extract-skeleton.py", "extract-errors.py"]
const ts = "2025-01-01T00:00:00Z"
const session = "claude-synthetic"
const base = { cwd: "/synthetic", entrypoint: "cli", gitBranch: "synthetic", isSidechain: false, parentUuid: null, sessionId: session, timestamp: ts, userType: "external", version: "synthetic" }
const user = (content: unknown, uuid = "u1") => ({ type: "user", ...base, uuid, message: { role: "user", content } })
const assistant = (content: unknown, uuid = "a1") => ({ type: "assistant", ...base, uuid, message: { role: "assistant", content } })
const call = (id: string, command = "check") => ({ type: "tool_use", id, name: "Bash", input: { command } })
const result = (id: string, content = "done") => ({ type: "tool_result", tool_use_id: id, content })
const copy = (role: string, content: unknown, hash = "hash") => ({ type: "api-request-blob", hash, sessionId: session, message: { role, content } })
const attachment = (value: object, extra: object = {}) => ({ type: "attachment", attachment: value, ...base, uuid: "attachment", ...extra })
const prompt = user("Keep the saved value")
const turn = [prompt, assistant([{ type: "text", text: "Checking" }, call("c1")]), user([result("c1")], "u2")]
const journal: Record<string, any> = {
  "api-request": { type: "api-request", base: "b", cacheControl: [{}], id: "r", keep: 1, params: {}, querySource: "q", requestDigest: "d", sessionId: session, shapeHash: "s", tail: ["t"], timestamp: ts, version: "synthetic" },
  "api-request-shape": { type: "api-request-shape", sessionId: session, shape: { system: [], tools: [] }, shapeHash: "s", timestamp: ts, version: "synthetic" },
  "atis-latch": { type: "atis-latch", atis: "a", sessionId: session },
  "bridge-session": { type: "bridge-session", bridgeSessionId: "b", lastSequenceNum: 1, ownerAccountUuid: "a", ownerOrganizationUuid: "o", sessionId: session },
  "cost-state": { type: "cost-state", hasUnknownModelCost: false, modelUsage: {}, sessionId: session, startTime: 1, totalAPIDuration: 1, totalAPIDurationWithoutRetries: 1, totalCostUSD: 0.5, totalDuration: 1, totalLinesAdded: 0, totalLinesRemoved: 0, totalToolDuration: 1 },
}
// Each entry: the keys every record of the type carries, then the keys only some carry.
const attachments: [object, object][] = [
  [{ type: "deferred_tools_delta", addedNames: ["n"], addedLines: ["CONTEXT"], removedNames: [], readdedNames: [] }, { pendingMcpServers: [], needsAuthMcpServers: [], failedMcpServers: [{}], surfacedDefinitions: [{}], surfacedNames: ["n"], wireHiddenNames: [] }],
  [{ type: "agent_listing_delta", addedTypes: ["t"], addedLines: ["CONTEXT"], removedTypes: [], isInitial: true, showConcurrencyNote: false }, { builtInPluginTypes: [], builtInTypes: ["t"] }],
  [{ type: "mcp_instructions_delta", addedNames: ["n"], addedBlocks: ["CONTEXT"], removedNames: [] }, {}],
  [{ type: "skill_listing", content: "CONTEXT", skillCount: 1, isInitial: true, names: ["n"] }, { digests: ["d"] }],
  [{ type: "date_change", newDate: "CONTEXT" }, {}],
  [{ type: "date", date: "CONTEXT" }, { changed: true }],
  [{ type: "hook_success", command: "c", content: "CONTEXT", durationMs: 1, exitCode: 0, hookEvent: "e", hookName: "n", stderr: "", stdout: "CONTEXT", toolUseID: "c1" }, {}],
  [{ type: "hook_additional_context", content: ["CONTEXT"], hookEvent: "e", hookName: "n", toolUseID: "c1" }, {}],
  [{ type: "total_tokens_reminder", text: "CONTEXT" }, {}],
  [{ type: "silent_turn_reminder", text: "CONTEXT" }, {}],
  [{ type: "batching_reminder_sent", clearAt: ts, model: "m", text: "CONTEXT" }, {}],
  [{ type: "deferred_tools_record", entries: [{}] }, { toolInputCopies: [{}] }],
  [{ type: "file", content: { text: "CONTEXT" }, displayPath: "p", filename: "f" }, {}],
  [{ type: "compact_file_reference", displayPath: "p", filename: "f" }, {}],
  [{ type: "prompt_snapshot", contextRendering: "r", echoWireToolInputs: false, reminderFold: false, systemPrompt: ["CONTEXT"] }, { cliPrefix: "p", inlineTools: false, keptReminders: false, systemTurns: false, toolChangeHeader: false, tools: [{}] }],
  [{ type: "remote_session_change", commit: "c", managedCommit: false, managedPr: false, pr: "p", sendUserFileHint: false, url: null }, {}],
  [{ type: "model", identity: {}, text: "CONTEXT" }, {}],
  [{ type: "instructions", files: [{}] }, { changed: true, reason: "r" }],
  [{ type: "session_context", context: {} }, { changed: true, reason: "r" }],
  [{ type: "environment", snapshot: {} }, {}],
  [{ type: "auto_mode", autoModeConsentFlow: false, bashFirst: false, bashFirstSteer: "s", bypass: false, finishExposedWork: false, steerOnly: false }, {}],
  [{ type: "command_permissions", allowedTools: [] }, {}],
  [{ type: "credential_org", organizationUuid: "o" }, {}],
  [{ type: "queued_command", commandMode: "prompt", delivery_id: "d", origin: { kind: "human" }, prompt: "CONTEXT", runId: "r", source_uuid: "s", timestamp: ts }, { usage: {} }],
  [{ type: "invoked_skills", skills: [{}] }, {}],
  [{ type: "task_status", canContinueAgent: false, deltaSummary: null, description: "CONTEXT", outputFilePath: "p", status: "s", taskId: "t", taskType: "t" }, {}],
]
const rendering = { slug: "s", rendered: [{ type: "text", text: "CONTEXT" }], renderedRole: "user", renderedBesideToolResult: true, renderedInHumanTurn: [{ type: "text", text: "CONTEXT" }] }
const bytes = (rows: unknown[]) => Buffer.from(rows.map(x => JSON.stringify(x)).join("\n") + "\n")
async function execute(argv: string[], input?: Uint8Array) {
  const proc = Bun.spawn(["python3", ...argv], { stdin: input ?? "ignore", stdout: "pipe", stderr: "pipe" })
  const [out, err, code] = await Promise.all([new Response(proc.stdout).text(), new Response(proc.stderr).text(), proc.exited])
  expect(err).toBe("")
  expect(code).toBe(0)
  return JSON.parse(out)
}
const whole = (reader: string, rows: unknown[], args: string[] = []) => execute([join(scripts, reader), "--json", ...args], bytes(rows))
async function streamed(reader: string, rows: unknown[], args: string[]) {
  const root = await mkdtemp(join(tmpdir(), "claude-records-"))
  const file = join(root, "session.jsonl"), content = bytes(rows)
  try {
    await writeFile(file, content)
    return await execute([join(scripts, reader), "--json", "--file-input", file, "--prefix-bytes", String(content.length), "--prefix-sha256", createHash("sha256").update(content).digest("hex"), "--deadline-ms", String(Date.now() + 170000), ...args])
  } finally { await rm(root, { recursive: true, force: true }) }
}
// One explicit window read through both paths; the streamed result must equal the buffered one.
async function window(reader: string, rows: unknown[], from: number, to = from) {
  const listed = await whole("extract-skeleton.py", rows, ["--boundaries-json"])
  const select = ["--start-record", listed.boundaries[from].fingerprint, "--end-record", listed.boundaries[to].fingerprint]
  const [old, out] = [await whole(reader, rows, select), await streamed(reader, rows, select)]
  expect(out.meta.incomplete_reason).toBeUndefined()
  expect(out.text).toBe(old.text)
  for (const key of ["selection", "global", "full_identity", "unknown_records", "omitted", "truncated", "identity_conflicts", "unmatched_calls", "unmatched_results", "tool_observations"]) expect(out.meta[key]).toEqual(old.meta[key])
  expect(out.meta.outside.omitted).toBe(old.meta.outside.omitted)
  return out
}
const clear = (meta: any) => expect([meta.unknown_records, meta.identity_conflicts, meta.unmatched_calls, meta.unmatched_results]).toEqual([0, 0, 0, 0])

for (const reader of readers) {
  test(`${reader}: wire copies repeat, differ from and outnumber their records without a conflict`, async () => {
    const copies = [
      copy("assistant", [{ type: "thinking", thinking: "COPY", signature: "s" }, { ...call("c1", "COPY changed"), cache_control: { type: "ephemeral" } }], "h1"),
      copy("assistant", [{ type: "text", text: "COPY" }, call("c1", "COPY changed again")], "h2"),
      copy("assistant", [call("c1")], "h3"),
      copy("user", [result("c1", "COPY changed"), { type: "tool_addition", tools: ["COPY"] }], "h4"),
      copy("user", [result("c1")], "h5"),
      { ...copy("system", "COPY".repeat(300), "h6"), message: { role: "system", content: "COPY".repeat(300), clear_at: ts, output_config: {} } },
    ]
    // A streamed boundary needs a timestamp, so prompts bracket the copies.
    const rows = [copies[5], ...turn, user("Before the copies", "u3"), ...copies.slice(0, 5), user("After the copies", "u4")]
    const out = await whole(reader, rows)
    clear(out.meta)
    expect([out.meta.omitted, out.meta.truncated]).toEqual([0, 0])
    expect(out.meta.tool_observations.map((o: any) => o.call_id)).toEqual(["c1"])
    expect(JSON.stringify(out)).not.toContain("COPY")
    expect(out.meta.tool).toBe(reader === "extract-skeleton.py" ? 1 : undefined)
    // The prompt alone, the result with its call's closure, and a range around every copy.
    for (const [from, to] of [[1, 1], [3, 3], [4, 10]]) {
      const selected = await window(reader, rows, from, to)
      clear(selected.meta)
      clear(selected.meta.global)
      expect(selected.meta.global.unknown_links).toBe(0)
      expect([selected.meta.omitted, selected.meta.truncated]).toEqual([0, 0])
      expect(JSON.stringify(selected)).not.toContain("COPY")
    }
    expect((await window(reader, rows, 3)).meta.selection.closure_records).toBe(1)
    const around = await window(reader, rows, 4, 10)
    expect([around.meta.selection.selected_records, around.meta.selection.closure_records, around.meta.tool_observations]).toEqual([7, 0, []])
  })
  test(`${reader}: a copied identity without its conversation record is a global unknown link`, async () => {
    const cases = [
      [...turn, copy("assistant", [call("absent")])],
      [copy("user", [result("absent")]), ...turn],
      // A copied result does not resolve through the call alone.
      [prompt, assistant([call("c1")]), copy("user", [result("c1")])],
      [prompt, copy("assistant", [call("c1")]), user([result("c1")], "u2")],
    ]
    for (const rows of cases) {
      const at = rows.indexOf(prompt)
      expect((await whole(reader, rows)).meta.unknown_records).toBeGreaterThan(0)
      const out = await window(reader, rows, at)
      expect(out.meta.global.unknown_links).toBeGreaterThan(0)
      expect(out.meta.unknown_records).toBeGreaterThan(0)
    }
    // Resolution reads the whole input, wherever the window puts the copy and its record.
    const rows = [...turn, copy("assistant", [call("c1")]), copy("user", [result("c1")], "h2"), user("After the copies", "u3")]
    for (const [from, to] of [[0, 0], [1, 2], [2, 5], [5, 5]]) expect((await window(reader, rows, from, to)).meta.global.unknown_links).toBe(0)
  })
  test(`${reader}: a message on any other record type is never a call, result or link`, async () => {
    const system = { type: "system", ...base, uuid: "s1", message: { role: "assistant", content: [call("ghost"), result("c1", "GHOST")] } }
    const rows = [...turn, system]
    const out = await whole(reader, rows)
    clear(out.meta)
    expect(out.meta.tool_observations.map((o: any) => o.call_id)).toEqual(["c1"])
    const selected = await window(reader, rows, 3)
    clear(selected.meta.global)
    expect(selected.meta.selection.closure_records).toBe(0)
    expect(JSON.stringify(selected)).not.toContain("GHOST")
    // A message that is not an object stays unknown on every record type.
    expect((await whole(reader, [...turn, { ...system, message: "GHOST" }])).meta.unknown_records).toBeGreaterThan(0)
  })
  test(`${reader}: copy shape drift stays an unknown record`, async () => {
    const good = () => copy("assistant", [{ type: "text", text: "COPY" }, call("c1")]) as any
    const mutations = [
      (x: any) => { x.extra = true },
      (x: any) => { delete x.hash },
      (x: any) => { x.hash = 1 },
      (x: any) => { x.sessionId = null },
      (x: any) => { x.message = "COPY" },
      (x: any) => { x.message.role = "developer" },
      (x: any) => { x.message.role = 1 },
      (x: any) => { x.message.extra = true },
      (x: any) => { delete x.message.content },
      (x: any) => { x.message.content = 1 },
      (x: any) => { x.message.content[0] = "COPY" },
      (x: any) => { x.message.content[0].type = "future" },
      (x: any) => { x.message.content[0].type = ["text"] },
      (x: any) => { delete x.message.content[1].id },
      (x: any) => { x.message.content[1].id = "" },
      (x: any) => { x.message.content[1] = { type: "tool_result", tool_use_id: 1 } },
      (x: any) => { x.message.clear_at = 1 },
      (x: any) => { x.message.output_config = [] },
    ]
    clear((await window(reader, [...turn, good()], 0)).meta)
    for (const mutate of mutations) {
      const value = good(); mutate(value)
      const out = await window(reader, [...turn, value], 0)
      expect(out.meta.global.unknown_records).toBeGreaterThan(0)
      expect(out.meta.unknown_records).toBeGreaterThan(0)
      expect(JSON.stringify(out)).not.toContain("COPY")
    }
  })
  test(`${reader}: request journal and bookkeeping records are neither evidence nor omissions`, async () => {
    const rows = [...Object.values(journal), ...turn]
    const out = await whole(reader, rows)
    clear(out.meta)
    expect(out.meta.omitted).toBe(0)
    expect(out.text).toBe((await whole(reader, turn)).text)
    const { base: _, ...unbased } = journal["api-request"]
    const selected = await window(reader, [...rows, unbased], 0, rows.length)
    clear(selected.meta)
    expect([selected.meta.omitted, selected.meta.outside.omitted]).toEqual([0, 0])
    const drift: [string, (x: any) => void][] = [
      ["api-request", x => { x.extra = true }], ["api-request", x => { delete x.requestDigest }], ["api-request", x => { x.keep = "1" }],
      ["api-request", x => { x.tail = [1] }], ["api-request", x => { x.cacheControl = {} }], ["api-request", x => { x.base = null }],
      ["api-request-shape", x => { x.shape = [] }], ["api-request-shape", x => { delete x.version }],
      ["atis-latch", x => { x.atis = 1 }], ["atis-latch", x => { x.message = { role: "user", content: [] } }],
      ["bridge-session", x => { x.lastSequenceNum = true }], ["bridge-session", x => { delete x.ownerAccountUuid }],
      ["cost-state", x => { x.totalCostUSD = "0" }], ["cost-state", x => { x.modelUsage = null }], ["cost-state", x => { x.totalDuration = 1.5 }],
      ["atis-latch", x => { x.type = "atis-future" }], ["api-request", x => { x.type = "api-request-future" }],
    ]
    for (const [kind, mutate] of drift) {
      const value = structuredClone(journal[kind]); mutate(value)
      const held = await window(reader, [...turn, value], 0)
      expect(held.meta.global.unknown_records).toBeGreaterThan(0)
      expect(held.meta.unknown_records).toBeGreaterThan(0)
    }
  })
  test(`${reader}: every recognized attachment is unread evidence that holds only when selected`, async () => {
    // Each type twice: its required keys alone, then every optional body and root key.
    const context = attachments.flatMap(([required, optional], i) => [
      attachment(required, { uuid: `required-${i}` }),
      attachment({ ...required, ...optional }, { ...rendering, uuid: `optional-${i}` }),
    ])
    const rows = [...context, prompt]
    const out = await whole(reader, rows)
    expect([out.meta.omitted, out.meta.unknown_records]).toEqual([context.length, 0])
    const outside = await window(reader, rows, context.length)
    clear(outside.meta)
    expect([outside.meta.omitted, outside.meta.outside.omitted]).toEqual([0, context.length])
    expect(JSON.stringify(outside)).not.toContain("CONTEXT")
    const one = await window(reader, rows, context.length - 1, context.length)
    expect([one.meta.omitted, one.meta.outside.omitted]).toEqual([1, context.length - 1])
    // One unknown member is counted alone; its recognized siblings stay omissions.
    const mixed = await whole(reader, [attachment({ type: "future_context" }, { uuid: "future" }), ...rows])
    expect([mixed.meta.omitted, mixed.meta.unknown_records]).toEqual([context.length, 1])
  })
  test(`${reader}: attachment type, key and value-kind drift stays an unknown record`, async () => {
    const [required, optional] = attachments[0] as [any, any]
    const unknown = [
      attachment({ type: "future_context", text: "CONTEXT" }),
      attachment({ ...required, extra: true }),
      attachment({ type: required.type, addedNames: required.addedNames }),
      attachment({ ...required, ...optional, failedMcpServers: ["CONTEXT"] }),
      attachment({ ...required, ...optional, wireHiddenNames: ["CONTEXT"] }),
      attachment({ type: "remote_session_change", commit: "c", managedCommit: false, managedPr: false, pr: "p", sendUserFileHint: false, url: "CONTEXT" }),
      attachment({ type: "hook_success", command: "c", content: "CONTEXT", durationMs: "1", exitCode: 0, hookEvent: "e", hookName: "n", stderr: "", stdout: "", toolUseID: "c1" }),
      attachment({ type: ["date"], date: "CONTEXT" }),
      attachment(required, { slug: 1 }),
      attachment(required, { rendered: "CONTEXT" }),
      attachment(required, { rendered: ["CONTEXT"] }),
      attachment(required, { renderedRole: 1 }),
      attachment(required, { renderedBesideToolResult: "yes" }),
      attachment(required, { renderedInHumanTurn: [1] }),
      attachment(required, { message: { role: "user", content: [result("c1")] } }),
      attachment(required, { parentUuid: 1 }),
      attachment(required, { isSidechain: "false" }),
    ]
    for (const value of unknown) {
      const out = await window(reader, [value, prompt], 1)
      expect(out.meta.global.unknown_records).toBeGreaterThan(0)
      expect(out.meta.unknown_records).toBeGreaterThan(0)
      expect(JSON.stringify(out)).not.toContain("CONTEXT")
    }
  })
}

test("a queued prompt, a copied user message and a message on another record type are never offered as a user boundary", async () => {
  const queued = attachment(attachments.find(([value]) => (value as any).type === "queued_command")![0], { uuid: "queued" })
  const noted = { type: "system", ...base, uuid: "s1", message: { role: "user", content: "COPY" } }
  const rows = [queued, copy("user", "COPY"), copy("user", [{ type: "text", text: "COPY" }], "h2"), noted, prompt]
  const listed = await whole("extract-skeleton.py", rows, ["--boundaries-json"])
  expect(listed.boundaries.map((row: any) => row.role)).toEqual([undefined, undefined, undefined, "system", "user"])
  const filtered = await streamed("extract-skeleton.py", rows, ["--boundaries-json", "--boundary-role", "user"])
  expect(filtered.boundaries).toEqual([listed.boundaries[4]])
  expect(filtered.meta.boundaries.total).toBe(1)
  expect(filtered.meta.omitted).toBe(1)
  clear(filtered.meta.global)
})

test("copied identities are charged to the unchanged streamed index", async () => {
  const one = copy("assistant", [call("c1")]), two = copy("user", [result("c1")], "h2")
  const listed = await whole("extract-skeleton.py", turn, ["--boundaries-json"])
  const select = ["--start-record", listed.boundaries[0].fingerprint, "--end-record", listed.boundaries[0].fingerprint]
  const scans = []
  for (const rows of [turn, [...turn, one], [...turn, one, one, two]]) scans.push((await streamed("extract-skeleton.py", rows, select)).meta.scan)
  expect(scans.map(scan => scan.index_entries - scans[0].index_entries)).toEqual([0, 1, 2])
  expect(scans.map(scan => scan.index_bytes - scans[0].index_bytes)).toEqual([0, 160, 320])
})
