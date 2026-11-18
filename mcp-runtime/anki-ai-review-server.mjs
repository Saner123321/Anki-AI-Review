import { execFile } from "node:child_process";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { z } from "zod/v4";

const python = join(process.env.LOCALAPPDATA, "AnkiProgramFiles", ".venv", "Scripts", "python.exe");
const backend = fileURLToPath(new URL("../tools/submit_proposals.py", import.meta.url));
const server = new McpServer({ name: "anki-ai-review", version: "1.1.0" });

function runBackend(request) {
  const input = JSON.stringify(request);
  if (Buffer.byteLength(input, "utf8") > 10_000_000) {
    throw new Error("提案数据超过 10MB，请拆成多批。");
  }
  return new Promise((resolve, reject) => {
    const child = execFile(
      python, ["-X", "utf8", "-B", backend, "--stdio-request"],
      { windowsHide: true, encoding: "utf8", timeout: 30_000, maxBuffer: 12_000_000 },
      (error, stdout, stderr) => {
        if (error) {
          const message = error.code === "ENOENT"
            ? "找不到本机 Anki Python 运行环境。"
            : stderr.trim() || (error.killed ? "Anki 审批队列请求超时。" : "Anki 审批队列请求失败。");
          reject(new Error(message));
          return;
        }
        try {
          resolve(JSON.parse(stdout));
        } catch {
          reject(new Error("审批队列返回的数据格式无效。"));
        }
      },
    );
    // An early child exit is reported by execFile even if stdin closes first.
    child.stdin.on("error", () => {});
    child.stdin.end(input, "utf8");
  });
}

async function resultFor(request) {
  const result = await runBackend(request);
  return { content: [{ type: "text", text: JSON.stringify(result) }], structuredContent: result };
}

const fields = z.record(z.string().min(1), z.string().max(200_000));
const base = z.object({
  note_id: z.number().int().positive(),
  guid: z.string(),
  mid: z.number().int().positive(),
  fields,
  tags: z.array(z.string()),
  model_hash: z.string(),
  profile_id: z.string(),
  fingerprint: z.string(),
}).describe("get_note_snapshots 返回的单条原文快照。使用时整份保持不变。");

server.registerTool("get_review_context", {
  title: "查询 Anki 审批目标",
  description: "只读查询当前本机 Anki Profile、笔记类型的实际字段，以及可选目标牌组。可以用关键字缩小牌组范围。",
  inputSchema: {
    deck_query: z.string().max(150).optional().describe("牌组名称包含的关键字。返回前 30 个匹配名称。"),
    note_type: z.string().min(1).max(150).optional().describe("要查询的笔记类型。默认 XXHK - 问答。"),
  },
  annotations: { readOnlyHint: true, openWorldHint: false },
}, args => resultFor({ operation: "get_review_context", ...args }));

server.registerTool("get_note_snapshots", {
  title: "读取 Anki 原文快照",
  description: "后台读取当前 Profile 的原文快照，供修改或回收站提案用作 base。按 Anki 搜索式或笔记 ID 查询，二选一，一次最多 500 条。此工具只读取卡片内容。",
  inputSchema: {
    expected_profile: z.string().min(1).max(150).describe("get_review_context 核对的当前 Profile。"),
    search: z.string().trim().min(1).max(4_000).optional().describe("Anki 搜索式，例如 deck:\"某牌组\"。"),
    note_ids: z.array(z.number().int().positive()).min(1).max(500).optional().describe("笔记 ID 列表，非卡片 ID。"),
  },
  annotations: { readOnlyHint: true, openWorldHint: false },
}, args => resultFor({ operation: "get_note_snapshots", ...args }));

server.registerTool("submit_proposals", {
  title: "提交 Anki 待审提案",
  description: "把新增、修改或移入回收站提案存进 Anki AI Review 的本地待审队列。只有用户在审批中心确认后，卡片才会生效。",
  inputSchema: {
    expected_profile: z.string().min(1).max(150).describe("get_review_context 确认的当前 Profile 名称。"),
    proposals: z.array(z.object({
      id: z.string().min(1).max(150),
      action: z.enum(["add", "update", "delete"]),
      reason: z.string().max(4_000).optional(),
      deck: z.string().min(1).max(200).optional().describe("新增必填，使用已有牌组的完整名称。"),
      notetype: z.string().min(1).max(150).optional().describe("新增默认 XXHK - 问答。"),
      fields: fields.optional().describe("新增或更新的字段。XXHK 字段为问题、答案、引用。挖空写 <u>待回忆内容</u>。"),
      base: base.optional().describe("修改或移入回收站必填，使用当前 Profile 的完整原文快照。"),
    })).min(1).max(500),
  },
  annotations: { readOnlyHint: false, destructiveHint: false, idempotentHint: true, openWorldHint: false },
}, ({ expected_profile, proposals }) => resultFor({
  operation: "submit_proposals",
  expected_profile,
  document: { schema: "anki-ai-review/1", proposals },
}));

await server.connect(new StdioServerTransport());
