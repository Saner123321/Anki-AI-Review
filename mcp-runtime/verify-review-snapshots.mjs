// Run against the isolated Anki profile created by tools/run_test_anki.py.
import assert from "node:assert/strict";
import { createServer } from "node:http";
import { basename, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StdioClientTransport } from "@modelcontextprotocol/sdk/client/stdio.js";

const [base, profile, id] = process.argv.slice(2);
assert.equal(basename(base), "Anki2");
assert.equal(profile, "AI Review Test", "This test must never submit into a real profile.");
const ankiConnect = createServer(async (request, response) => {
  const chunks = [];
  for await (const chunk of request) chunks.push(chunk);
  const { action } = JSON.parse(Buffer.concat(chunks));
  response.setHeader("Content-Type", "application/json");
  response.end(JSON.stringify(action === "getActiveProfile"
    ? { result: profile, error: null }
    : { result: null, error: "Unexpected AnkiConnect action" }));
});
const client = new Client({ name: "anki-ai-review-snapshot-test", version: "1.0.0" });

async function tool(name, args) {
  const result = await client.callTool({ name, arguments: args });
  assert.equal(Boolean(result.isError), false, result.content?.[0]?.text);
  return result.structuredContent;
}

try {
  await new Promise((resolve, reject) => {
    ankiConnect.once("error", reject);
    ankiConnect.listen(0, "127.0.0.1", resolve);
  });
  await client.connect(new StdioClientTransport({
    command: process.execPath,
    args: [fileURLToPath(new URL("./anki-ai-review-server.mjs", import.meta.url))],
    env: {
      ...process.env,
      APPDATA: dirname(base),
      ANKI_AI_REVIEW_CONNECT_PORT: String(ankiConnect.address().port),
    },
  }));
  const { tools } = await client.listTools();
  assert.equal(tools.find(t => t.name === "get_note_snapshots")?.annotations?.readOnlyHint, true);
  const exact = await tool("get_note_snapshots", { expected_profile: profile, note_ids: [Number(id), Number(id)] });
  const searched = await tool("get_note_snapshots", { expected_profile: profile, search: `nid:${id}` });
  assert.equal(exact.profile, profile);
  assert.equal(exact.notes.length, 1);
  assert.deepEqual(exact, searched);
  const wrong = await client.callTool({
    name: "get_note_snapshots",
    arguments: { expected_profile: "Other Profile", note_ids: [Number(id)] },
  });
  assert.equal(wrong.isError, true);
  const missing = await client.callTool({
    name: "get_note_snapshots",
    arguments: { expected_profile: profile, note_ids: [1] },
  });
  assert.equal(missing.isError, true);
  const queued = await tool("submit_proposals", {
    expected_profile: profile,
    proposals: [{
      id: `snapshot-mcp-test-${id}`,
      action: "update",
      base: exact.notes[0],
      fields: { 问题: "测试提案等待审批" },
    }],
  });
  assert.equal(queued.proposals[0].status, "pending");
  const after = await tool("get_note_snapshots", { expected_profile: profile, note_ids: [Number(id)] });
  assert.deepEqual(after, exact, "The queued edit must not change the collection.");
  console.log(JSON.stringify({ snapshot: exact.notes[0], queued }));
} finally {
  await client.close();
  await new Promise(resolve => ankiConnect.close(resolve));
}
