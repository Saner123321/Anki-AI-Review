# Anki AI Review

Anki 原生内容审批插件。AI 可以提交新增、修改和移入回收站的提案，你可以在 Anki 中逐处审阅、编辑并决定何时写入。

## 安装

当前插件版本为 0.4.0，最低 Anki 版本为 25.09.4。使用 Python 3 运行 `python build.py`，然后在 Anki 的 **工具 → 插件 → 从文件安装** 中选择 `dist/Anki-AI-Review-0.4.0.ankiaddon` 并重启 Anki。

## 审批

1. 在 **工具 → Anki AI Review · 内容审批中心** 打开窗口，或使用 `Ctrl+Alt+R`。点击 **导入提案 JSON**。
2. 选中提案，查看顶部的目标牌组和笔记类型。用红绿差异逐处接受或拒绝，也可以按字段、笔记或批量处理，并调整差异粒度。右侧显示卡片预览。
3. 点击 **修改内容…** 打开 Anki 原生编辑器。可用字段工具栏的 `</>` 按钮编辑 HTML。点 **保存到提案** 后可预览你的版本。
4. 处理完待审内容后，点 **加入牌组** 或 **保存到原笔记**。需要回退时可点 **撤销最近写入**，或在历史中根据较早的版本生成恢复提案。

修改提案使用原文快照检查冲突。移入回收站会保留卡片及其复习记录。审批和版本历史保存在当前 Profile 的独立 SQLite 数据库中。

## JSON 提案

字段和操作见 [proposal.schema.json](proposal.schema.json)。下面的例子使用 `Default` 牌组和英文 `Basic` 笔记类型，请换成当前 Anki Profile 中实际存在的牌组、类型和字段名称。

```json
{
  "schema": "anki-ai-review/1",
  "proposals": [
    {
      "id": "example-pythagorean-theorem",
      "action": "add",
      "deck": "Default",
      "notetype": "Basic",
      "fields": {
        "Front": "勾股定理的等式是什么？",
        "Back": "\\(a^2+b^2=c^2\\)"
      },
      "reason": "演示公式在审批预览中的排版"
    }
  ]
}
```

新增提案的笔记类型默认是 `XXHK - 问答`。使用该模板时可用 `<u>待回忆内容</u>` 表示挖空，具体渲染由已安装的笔记模板控制。使用其他模板时请显式填写 `notetype`。修改和回收站提案需要从当前 Profile 获取完整的 `base` 快照。

## MCP 后台接口

MCP 支持 Windows 上正在运行的 Anki，并要求启用本机 AnkiConnect。当前启动脚本使用 Anki Windows 启动器安装的 Python 运行环境。将仓库克隆到本机后，在仓库根目录运行以下命令。

```powershell
npm ci --prefix mcp-runtime
$server = (Resolve-Path mcp-runtime/anki-ai-review-server.mjs).Path
codex mcp add anki-ai-review -- node $server
```

MCP 使用标准输入输出传输，也可由其他兼容客户端启动。工具有以下三个。

- `get_review_context` 查询当前 Profile、笔记类型及其字段、目标牌组。
- `get_note_snapshots` 按搜索式或笔记 ID 获取当前 Profile 的原文快照。修改或回收站提案应原样传入快照作为 `base`。
- `submit_proposals` 校验 `expected_profile` 后提交到待审队列。打开审批中心即可审阅。如果窗口已经打开，重新打开可读取新增提案。

## 测试

```powershell
python -B -m unittest discover -s tests
```

可使用 Anki 自带的 Python 运行集成测试。先安装 MCP 依赖并运行 `build.py` 生成插件包。

```powershell
$ankiPython = Join-Path $env:LOCALAPPDATA 'AnkiProgramFiles/.venv/Scripts/python.exe'
& $ankiPython -X utf8 -B tools/run_test_anki.py --snapshot-smoke --base .test-anki-snapshot/Anki2
```

集成测试使用独立的本地测试 Profile 和合成卡片。

许可为 GNU AGPL-3.0-or-later。见 [LICENSE](LICENSE) 和 [NOTICE.md](NOTICE.md)。
