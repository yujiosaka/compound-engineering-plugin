import { afterAll, expect, test } from "bun:test"
import { createHash } from "node:crypto"
import { mkdtemp, writeFile, appendFile, readdir, rm } from "node:fs/promises"
import { tmpdir } from "node:os"
import { basename, join } from "node:path"

const dir = process.env.SESSION_HISTORY_SCRIPT_DIR ?? join(import.meta.dir, "../skills/ce-compound/scripts/session-history")
const stamp = "2025-01-01T00:00:00Z"
const meta = { type: "session_meta", timestamp: stamp, payload: { id: "synthetic", cwd: "/synthetic", source: "cli", cli_version: "synthetic" } }
const message = (text: string, id = "m") => ({ type: "response_item", timestamp: stamp, payload: { type: "message", role: "user", id, content: [{ type: "input_text", text }] } })
const stable = (x: any): any => Array.isArray(x) ? x.map(stable) : x && typeof x === "object" ? Object.fromEntries(Object.keys(x).sort().map(k => [k, stable(x[k])])) : x
const fp = (x: any) => createHash("sha256").update(JSON.stringify(stable(x))).digest("hex")
const bytes = (rows: unknown[]) => Buffer.from(rows.map(x => JSON.stringify(x)).join("\n") + "\n")
const measurements: unknown[] = []
afterAll(() => console.log("Synthetic stream resource measurements:", JSON.stringify(measurements)))
async function execute(argv: string[], input?: Uint8Array) {
  const proc = Bun.spawn(argv, { stdin: input ?? "ignore", stdout: "pipe", stderr: "pipe" })
  const [out, err, code] = await Promise.all([new Response(proc.stdout).text(), new Response(proc.stderr).text(), proc.exited])
  expect(err).toBe(""); expect(code).toBe(0)
  const parsed = JSON.parse(out)
  if (parsed.meta?.scan) measurements.push({ reader: basename(argv[1]), outcome: parsed.meta.incomplete_reason ?? "complete", ...parsed.meta.scan })
  return parsed
}
async function fixture(rows: unknown[], body: (f: any) => Promise<void>) {
  const root = await mkdtemp(join(tmpdir(), "stream-history-"))
  const file = join(root, "session.jsonl"), content = bytes(rows)
  await writeFile(file, content)
  const args = ["--json", "--file-input", file, "--prefix-bytes", String(content.length), "--prefix-sha256", createHash("sha256").update(content).digest("hex"), "--deadline-ms", String(Date.now() + 170000)]
  try { await body({ root, file, content, args, run: (name: string, extra: string[] = []) => execute(["python3", join(dir, name), ...args, ...extra]) }) }
  finally { await rm(root, { recursive: true, force: true }) }
}
test("streamed explicit windows match legacy rendering, diagnostics and transitive ID closure", async () => {
  const call = { type: "response_item", timestamp: stamp, payload: { type: "function_call", call_id: "c", name: "exec", arguments: JSON.stringify({ cmd: "check", workdir: "/synthetic" }) } }
  const result = { type: "response_item", timestamp: stamp, payload: { type: "function_call_output", call_id: "c", output: { exit_code: 1, output: "failed" } } }
  const selected = message("Use the saved value")
  for (const rows of [[meta, selected], [meta, call, selected, result]]) await fixture(rows, async f => {
    const at = rows.length === 2 ? selected : result
    const select = ["--start-record", fp(at), "--end-record", fp(at)]
    for (const name of ["extract-skeleton.py", "extract-errors.py"]) {
      const old = await execute(["python3", join(dir, name), "--json", ...select], f.content)
      const out = await f.run(name, select)
      expect(out.meta.incomplete_reason).toBeUndefined()
      expect(out.text).toBe(old.text)
      expect(out.meta.selection).toEqual(old.meta.selection)
      expect(out.meta.global).toEqual(old.meta.global)
      expect(out.meta.full_identity).toEqual(old.meta.full_identity)
      expect(out.meta.scan.passes).toBe(2)
      expect(out.meta.scan.bytes).toBe(f.content.length * 2)
      expect(out.meta.scan.additional_disk_bytes).toBe(0)
      expect(await readdir(f.root)).toEqual(["session.jsonl"])
    }
  })
})
test("metadata keeps the fixed prefix while later appends remain outside coverage", async () => {
  const rows = [meta, message("current")]
  await fixture(rows, async f => {
    await appendFile(f.file, bytes([message("later", "later")]))
    const out = await f.run("extract-metadata.py")
    expect(out.sessions[0].session).toBe("synthetic")
    expect(out.meta.coverage.prefix_bytes).toBe(f.content.length)
    expect(out.meta.coverage.appended_bytes).toBeGreaterThan(0)
    expect(out.meta.coverage.complete_source).toBe(false)
    expect(out.meta.scan.passes).toBe(1)
    const window = await f.run("extract-skeleton.py", ["--start-record", fp(rows[1]), "--end-record", fp(rows[1])])
    expect(window.text).toContain("current")
    expect(window.text).not.toContain("later")
  })
})
test("boundary filtering reaches later native user records and caps only matching headers", async () => {
  const rows = [meta, ...Array.from({ length: 1100 }, (_, n) => ({ type: "event_msg", timestamp: stamp, payload: { type: "task_started", n } })), message("selected")]
  await fixture(rows, async f => {
    const out = await f.run("extract-skeleton.py", ["--boundaries-json", "--boundary-role", "user", "--since", stamp, "--until", stamp])
    expect(out.boundaries).toHaveLength(1)
    expect(out.boundaries[0].fingerprint).toBe(fp(rows.at(-1)))
    expect(out.meta.boundaries).toEqual({ total: 1, returned: 1, truncated: 0, source_records: 1102 })
    const all = await f.run("extract-skeleton.py", ["--boundaries-json"])
    expect(all.boundaries).toHaveLength(1000)
    expect(all.meta.boundaries.truncated).toBe(102)
  })
})
test("streamed file admission refuses source size, deadline, partial records and changed prefixes", async () => {
  await fixture([meta, message("selected")], async f => {
    const select = ["--start-record", fp(message("selected")), "--end-record", fp(message("selected"))]
    for (const [extra, reason] of [
      [["--prefix-bytes", String(1024 ** 3 + 1)], "source-limit"],
      [["--deadline-ms", "1"], "scan-timeout"],
      [["--prefix-bytes", String(f.content.length - 2)], "incomplete-record"],
      [["--prefix-sha256", "0".repeat(64)], "prefix-changed"],
      [["--since", "2025-01-02T00:00:00Z"], "boundary-outside-period"],
    ] as const) {
      expect((await f.run("extract-skeleton.py", [...select, ...extra])).meta.incomplete_reason).toBe(reason)
    }
  })
})
test("streamed selected and individual record byte limits never publish partial evidence", async () => {
  const one = message("x".repeat(5 * 1024 ** 2), "a"), two = message("x".repeat(5 * 1024 ** 2), "b")
  await fixture([meta, one, two], async f => {
    const out = await f.run("extract-skeleton.py", ["--start-record", fp(one), "--end-record", fp(two)])
    expect(out.meta.incomplete_reason).toBe("selection-limit")
    expect(out.text).toBe("")
  })
  await fixture([meta, message("x".repeat(8 * 1024 ** 2))], async f => {
    expect((await f.run("extract-metadata.py")).meta.incomplete_reason).toBe("record-limit")
  })
})
test("late identities and unknown records remain global outside a selected window", async () => {
  const selected = message("selected")
  for (const tail of [{ type: "turn_context", payload: { cwd: "/foreign" } }, { type: "future-record" }]) {
    await fixture([meta, selected, tail], async f => {
      const out = await f.run("extract-skeleton.py", ["--start-record", fp(selected), "--end-record", fp(selected)])
      expect(out.meta.global.identity_conflicts + out.meta.global.unknown_records).toBeGreaterThan(0)
    })
  }
})
test("prefix mutation between the two scans is rejected without storing a source copy", async () => {
  const selected = message("selected")
  await fixture([meta, selected], async f => {
    const program = `import runpy,argparse\nm=runpy.run_path(${JSON.stringify(join(dir, "extract-metadata.py"))})\np=argparse.ArgumentParser();m['stream_arguments'](p);p.add_argument('--start-record');p.add_argument('--end-record');p.add_argument('--json',action='store_true');a=p.parse_args()\nold=m['PrefixSource'].rows\ndef changed(self):\n yield from old(self)\n if self.passes==1:\n  with open(self.path,'r+b') as f:\n   b=f.read();f.seek(0);f.write(b.replace(b'selected',b'modified'))\nm['PrefixSource'].rows=changed\nprint(m['bounded_json'](m['stream_window'](a)))`
    const out = await execute(["python3", "-c", program, ...f.args, "--start-record", fp(selected), "--end-record", fp(selected)])
    expect(out.meta.incomplete_reason).toBe("prefix-changed")
    expect(out.meta.coverage.complete_source).toBe(false)
    expect(out.meta.coverage.appended_bytes).toBeNull()
  })
})
test("streaming bounds a large body while keeping only a small selected record", async () => {
  const selected = message("selected")
  const rows = [meta, ...Array.from({ length: 20 }, () => ({ type: "world_state", payload: { synthetic: "x".repeat(1024 ** 2) } })), selected]
  await fixture(rows, async f => {
    const out = await f.run("extract-skeleton.py", ["--start-record", fp(selected), "--end-record", fp(selected)])
    expect(out.meta.incomplete_reason).toBeUndefined()
    expect(out.meta.scan.bytes).toBe(f.content.length * 2)
    expect(out.meta.scan.index_bytes).toBeLessThan(10000)
    expect(out.meta.scan.peak_rss_bytes).toBeLessThan(256 * 1024 ** 2)
    expect(out.text).toContain("selected")
  })
})
test("many distinct small native records stop at the index bound", async () => {
  const rows = [meta, ...Array.from({ length: 120000 }, (_, n) => message("synthetic", `m-${n}`))]
  await fixture(rows, async f => {
    const out = await f.run("extract-skeleton.py", ["--boundaries-json"])
    expect(out.meta.incomplete_reason).toBe("index-limit")
    expect(out.boundaries).toEqual([])
    expect(out.meta.scan.index_bytes).toBeLessThanOrEqual(48 * 1024 ** 2)
    expect(out.meta.scan.index_bytes).toBeGreaterThan(48 * 1024 ** 2 - 1024)
    expect(out.meta.scan.index_entries).toBeLessThan(400000)
  })
}, 30000)
test("native closure record count stops independently of the byte allowance", async () => {
  const selected = message("selected")
  const call = { type: "response_item", timestamp: stamp, payload: { type: "function_call", call_id: "same", name: "exec", arguments: "{}" } }
  const result = { type: "event_msg", timestamp: stamp, payload: { type: "exec_command_end", call_id: "same", exit_code: 0 } }
  await fixture([meta, call, ...Array(400001).fill(result), selected], async f => {
    const out = await f.run("extract-skeleton.py", ["--start-record", fp(selected), "--end-record", fp(selected)])
    expect(out.meta.incomplete_reason).toBe("index-limit")
    expect(out.meta.scan.index_entries).toBe(400000)
    expect(out.meta.scan.index_bytes).toBeLessThan(48 * 1024 ** 2)
    expect(out.meta.coverage.complete_source).toBe(false)
  })
}, 90000)
test("same-ID conflicting outputs and native aliases cannot be hidden outside the selected range", async () => {
  const selected = message("selected")
  const call = { type: "response_item", timestamp: stamp, payload: { type: "function_call", call_id: "same", name: "exec", arguments: JSON.stringify({ cmd: "check", workdir: "/synthetic" }) } }
  const result = (code: number) => ({ type: "response_item", timestamp: stamp, payload: { type: "function_call_output", call_id: "same", output: { exit_code: code } } })
  await fixture([meta, selected, call, result(1), result(0)], async f => {
    const out = await f.run("extract-errors.py", ["--start-record", fp(selected), "--end-record", fp(selected)])
    expect(out.meta.global.identity_conflicts).toBeGreaterThan(0)
  })
})
test("Claude records close transitively over multiple native IDs without retaining unrelated bodies", async () => {
  const row = (type: string, content: unknown[], uuid: string) => ({ type, timestamp: stamp, uuid, sessionId: "claude-synthetic", cwd: "/synthetic", version: "synthetic", message: { content } })
  const first = row("assistant", [{ type: "tool_use", id: "first", name: "Check", input: { cwd: "/synthetic" } }], "a")
  const middle = row("user", [{ type: "tool_result", tool_use_id: "first", is_error: false, content: "ok" }, { type: "tool_use", id: "second", name: "Check", input: { cwd: "/synthetic" } }], "b")
  const last = row("user", [{ type: "tool_result", tool_use_id: "second", is_error: false, content: "ok" }], "c")
  const rows = [first, middle, last]
  await fixture(rows, async f => {
    const select = ["--start-record", fp(last), "--end-record", fp(last)]
    for (const name of ["extract-skeleton.py", "extract-errors.py"]) {
      const old = await execute(["python3", join(dir, name), "--json", ...select], f.content)
      const out = await f.run(name, select)
      expect(out.meta.incomplete_reason).toBeUndefined()
      expect(out.meta.selection).toEqual(old.meta.selection)
      expect(out.meta.selection.selected_records).toBe(3)
      expect(out.meta.global).toEqual(old.meta.global)
      expect(out.text).toBe(old.text)
    }
  })
})
test("the JSON output boundary refuses oversized rendered evidence", async () => {
  const program = `import runpy\nm=runpy.run_path(${JSON.stringify(join(dir, "extract-metadata.py"))})\nprint(m['bounded_json']({'text':'x'*(8*1024*1024+1),'meta':{'scan':{'peak_rss_bytes':0,'additional_disk_bytes':0}}}))`
  const out = await execute(["python3", "-c", program])
  expect(out.text).toBe("")
  expect(out.meta.incomplete_reason).toBe("output-limit")
})
test("a supported record just below the byte ceiling keeps truncation explicit", async () => {
  const value = message("x".repeat(8 * 1024 ** 2 - 1024))
  await fixture([meta, value], async f => {
    const out = await f.run("extract-skeleton.py", ["--start-record", fp(value), "--end-record", fp(value)])
    expect(out.meta.incomplete_reason).toBeUndefined()
    expect(out.meta.truncated).toBeGreaterThan(0)
    expect(out.text.length).toBeLessThan(1000)
  })
})
test("final-pass rewrites fail closed while verified append-only changes stay outside coverage", async () => {
  const selected = message("selected")
  for (const scenario of ["rewrite", "rewrite-append", "append", "replace", "verification-rewrite", "verification-append", "verification-replace"]) {
    await fixture([meta, selected], async f => {
      const program = `import runpy,argparse,os
m=runpy.run_path(${JSON.stringify(join(dir, "extract-metadata.py"))})
p=argparse.ArgumentParser();m['stream_arguments'](p);p.add_argument('--start-record');p.add_argument('--end-record');p.add_argument('--json',action='store_true');a=p.parse_args()
scenario=${JSON.stringify(scenario)}
def replace(path):
 with open(path,'rb') as f: raw=f.read()
 with open(path+'.replacement','wb') as f: f.write(raw)
 os.replace(path+'.replacement',path)
def rewrite(path):
 with open(path,'r+b') as f:
  raw=f.read();f.seek(0);f.write(raw.replace(b'synthetic',b'synthetiX'))
old=m['PrefixSource'].rows
def modified(self):
 for row in old(self):
  yield row
  if self.passes==2 and row[0]==0:
   if scenario.startswith('rewrite'): rewrite(self.path)
   if scenario=='replace': replace(self.path)
   if scenario not in ('rewrite','replace'):
    with open(self.path,'ab') as f: f.write(b'{}\\n')
m['PrefixSource'].rows=modified
verify=m['PrefixSource'].verify_prefix
class ChangedRead:
 def __init__(self,source,path): self.source,self.path,self.changed=source,path,False
 def __getattr__(self,name): return getattr(self.source,name)
 def read(self,n):
  raw=self.source.read(n)
  if not self.changed:
   self.changed=True
   if scenario=='verification-rewrite': rewrite(self.path)
   elif scenario=='verification-replace': replace(self.path)
   else:
    with open(self.path,'ab') as f: f.write(b'{}\\n')
  return raw
def verification(self,source,expected):
 return verify(self,ChangedRead(source,self.path) if scenario.startswith('verification-') else source,expected)
m['PrefixSource'].verify_prefix=verification
print(m['bounded_json'](m['stream_window'](a)))`
      const out = await execute(["python3", "-c", program, ...f.args, "--start-record", fp(selected), "--end-record", fp(selected)])
      const passes = scenario === "replace" ? 2 : 3
      expect(out.meta.scan.passes).toBe(passes)
      expect(out.meta.scan.bytes).toBe(f.content.length * passes)
      expect(out.meta.scan.additional_disk_bytes).toBe(0)
      expect(out.meta.coverage.complete_source).toBe(false)
      if (scenario === "append") {
        expect(out.meta.incomplete_reason).toBeUndefined()
        expect(out.meta.coverage.appended_bytes).toBe(3)
        expect(out.meta.selection.valid).toBe(true)
        expect(out.meta.global.unknown_records).toBe(0)
      } else {
        expect(out.meta.incomplete_reason).toBe(scenario.startsWith("verification-") ? "prefix-unstable" : scenario === "replace" ? "source-replaced" : "prefix-changed")
        expect(out.meta.coverage.appended_bytes).toBeNull()
        expect(out.objects).toEqual([])
        expect(out.meta.global.selection_errors).toBeGreaterThan(0)
      }
    })
  }
})
test("leading metadata is replayed through the shared platform detector and global diagnostics", async () => {
  const claude = { type: "user", timestamp: stamp, sessionId: "synthetic", cwd: "/synthetic", version: "synthetic", uuid: "u", message: { content: [{ type: "text", text: "selected" }] } }
  const codex = message("selected")
  const cases = [
    [{ type: "file-history-snapshot", snapshot: {} }, claude],
    [{ type: "world_state", payload: {} }, meta, codex],
    [{ type: "future-record" }, meta, codex],
  ]
  for (const rows of cases) await fixture(rows, async f => {
    const select = ["--start-record", fp(rows.at(-1)), "--end-record", fp(rows.at(-1))]
    for (const name of ["extract-skeleton.py", "extract-errors.py"]) {
      const old = await execute(["python3", join(dir, name), "--json", ...select], f.content)
      const out = await f.run(name, select)
      expect(out.meta.incomplete_reason).toBeUndefined()
      expect(out.text).toBe(old.text)
      expect(out.meta.global).toEqual(old.meta.global)
    }
  })
})
test("leading lookahead stops at independent aggregate byte and record bounds", async () => {
  const selected = message("selected")
  for (const leading of [Array.from({ length: 9 }, () => ({ type: "world_state", payload: "x".repeat(1024 ** 2) })), Array(400001).fill({})]) {
    await fixture([...leading, meta, selected], async f => {
      const out = await f.run("extract-skeleton.py", ["--start-record", fp(selected), "--end-record", fp(selected)])
      expect(out.meta.incomplete_reason).toBe("leading-record-limit")
      expect(out.text).toBe("")
      expect(out.meta.coverage.complete_source).toBe(false)
      expect(out.meta.scan.additional_disk_bytes).toBe(0)
      expect(out.meta.scan.passes).toBe(1)
    })
  }
}, 30000)
test("Claude consumes preceding calls once while Codex retains order-independent association", async () => {
  const row = (type: string, content: unknown[], uuid: string) => ({ type, timestamp: stamp, uuid, sessionId: "synthetic", cwd: "/synthetic", version: "synthetic", message: { content } })
  const selected = row("user", [{ type: "text", text: "selected" }], "selected")
  const callBlock = { type: "tool_use", id: "c", name: "Check", input: {} }
  const resultBlock = { type: "tool_result", tool_use_id: "c", is_error: false, content: "ok" }
  const call = row("assistant", [callBlock], "call"), result = row("user", [resultBlock], "result")
  const changedCall = row("assistant", [{ ...callBlock, name: "Other" }], "changed-call")
  const changedResult = row("user", [{ ...resultBlock, is_error: true, content: "different" }], "changed-result")
  const sequences = [[result, call], [call, result, result], [call, call, result], [call, result, call, result], [row("assistant", [callBlock, callBlock], "double"), result], [call, result, changedCall, changedResult], [call, result, changedCall, changedResult, changedCall, changedResult]]
  for (const sequence of sequences) await fixture([selected, ...sequence], async f => {
    const select = ["--start-record", fp(selected), "--end-record", fp(selected)]
    for (const name of ["extract-skeleton.py", "extract-errors.py"]) {
      const old = await execute(["python3", join(dir, name), "--json", ...select], f.content)
      const out = await f.run(name, select)
      expect(out.meta.global).toEqual(old.meta.global)
      expect(out.text).toBe(old.text)
    }
  })
  const codex = message("selected")
  await fixture([meta, codex,
    { type: "response_item", timestamp: stamp, payload: { type: "function_call_output", call_id: "c", output: { exit_code: 0 } } },
    { type: "response_item", timestamp: stamp, payload: { type: "function_call", call_id: "c", name: "exec", arguments: "{}" } },
  ], async f => {
    const out = await f.run("extract-skeleton.py", ["--start-record", fp(codex), "--end-record", fp(codex)])
    expect(out.meta.global.unmatched_calls).toBe(0)
    expect(out.meta.global.unmatched_results).toBe(0)
  })
})
test("bounded Claude call-result sequences preserve legacy diagnostics inside and outside windows", async () => {
  const row = (content: unknown[], uuid: string) => ({ type: "user", timestamp: stamp, uuid, sessionId: "synthetic", cwd: "/synthetic", version: "synthetic", message: { content } })
  const selected = row([{ type: "text", text: "selected" }], "selected")
  const alphabet = [
    { type: "tool_use", id: "c", name: "Check", input: {} },
    { type: "tool_use", id: "c", name: "Other", input: {} },
    { type: "tool_result", tool_use_id: "c", is_error: false, content: "ok" },
    { type: "tool_result", tool_use_id: "c", is_error: true, content: "different" },
  ]
  const sequences: number[][] = [[]]
  for (let length = 1; length <= 3; length++) {
    for (const previous of sequences.filter(s => s.length === length - 1)) {
      for (let index = 0; index < alphabet.length; index++) sequences.push([...previous, index])
    }
  }
  expect(sequences).toHaveLength(85)
  for (const sequence of sequences) {
    const rows = [selected, ...sequence.map((index, ordinal) => row([alphabet[index]], `record-${ordinal}`))]
    await fixture(rows, async f => {
      for (const chosen of rows.length === 1 ? [selected] : [selected, rows.at(-1)!]) {
        const select = ["--start-record", fp(chosen), "--end-record", fp(chosen)]
        for (const name of ["extract-skeleton.py", "extract-errors.py"]) {
          const old = await execute(["python3", join(dir, name), "--json", ...select], f.content)
          const out = await f.run(name, select)
          expect(out.meta.global).toEqual(old.meta.global)
          expect(out.meta.selection).toEqual(old.meta.selection)
          expect(out.text).toBe(old.text)
        }
      }
    })
  }
}, 120000)
test("bounded unknown grammar buckets retain every overflow count", async () => {
  const unknown = Array.from({ length: 35 }, (_, index) => ({ type: `future_${index}` }))
  await fixture([meta, ...unknown, unknown[0], unknown[34], message("selected")], async f => {
    const old = await execute(["python3", join(dir, "extract-skeleton.py"), "--boundaries-json"], f.content)
    const out = await f.run("extract-skeleton.py", ["--boundaries-json"])
    expect(out.meta.unknown_record_types).toEqual(old.meta.unknown_record_types)
    expect(out.meta.unknown_record_types.other).toBe(5)
    expect(out.meta.global.unknown_records).toBe(37)
    expect(out.meta.global).toEqual(old.meta.global)
  })
})
