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
