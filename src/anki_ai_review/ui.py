from __future__ import annotations

import html
import copy
import json
import re
import secrets
from pathlib import Path
from datetime import datetime
from urllib.parse import quote

from aqt import mw, gui_hooks
from aqt.operations import CollectionOp, QueryOp
from aqt.qt import (QAbstractItemView, QComboBox, QDialog, QFileDialog, QHBoxLayout,
                    QLabel, QLineEdit, QListWidget, QListWidgetItem, QPushButton,
                    QSplitter, QVBoxLayout, QWidget, Qt)
from aqt.utils import askUser, getText, showInfo, showWarning
from aqt.webview import AnkiWebView

from .adapter import (apply_review, change_add_notetype, draft_note, export_snapshot,
                      load_review, manual_fields, proposal_decks, reconcile, render_preview,
                      restoration_proposal, supports_note_type_picker)
from .core import (SCHEMA, change_dicts, make_changes, materialize,
                   read_document, unresolved_changes)
from .draft_editor import DraftDialog
from .notetype_picker import NoteTypePicker
from .store import Store

ESC = html.escape
STATUS_LABELS = {"pending":"待审", "applied":"已写入", "rejected":"已拒绝", "undone":"已撤销", "diverged":"后续已改动", "interrupted":"写入中断", "prepared":"待核对", "failed":"失败"}


def proposal_label(row):
    p, changes = load_review(row)
    title = next(iter(p.get("fields", p.get("base", {}).get("fields", {})).values()), "")
    title = re.sub('<[^>]+>', '', title).replace('\n', ' ')[:45]
    identifier = p["id"] if len(p["id"]) <= 28 else p["id"][:27] + "…"
    edits = manual_fields(row)
    deck = f'\n目标：{p["deck"]}' if p["action"] == "add" else ""
    return (f'{STATUS_LABELS.get(row["status"], row["status"])} · {identifier}\n{title}'
            f'{deck}\n待审 {len(unresolved_changes(changes, edits))} 处'
            + (f' · 手动修改 {len(edits)} 字段' if edits else ""))


def local_time(value):
    return datetime.fromisoformat(value).astimezone().strftime("%Y-%m-%d %H:%M:%S")


CSS = """
body { font: 15px 'Segoe UI','Microsoft YaHei',sans-serif; color:#24334a; background:#f5f7fb; margin:0; padding:22px; }
* { box-sizing:border-box; } h2 {font-size:23px;margin:0 0 8px} h3{margin:0;font-size:17px}
.muted {color:#65758a;font-size:13px} .card{background:white;border:1px solid #dce2ec;border-radius:12px;padding:18px;margin:14px 0}
.bar{display:flex;gap:8px;align-items:center;justify-content:space-between;margin-bottom:12px}.actions{white-space:nowrap}
button {border:1px solid #cad3e2;border-radius:7px;padding:6px 12px;background:#fff;color:#2b405c;cursor:pointer;font:inherit;font-size:13px}
button:hover {background:#eaf0fa} .accept{color:#116a42;border-color:#8ec9aa}.reject{color:#b22b42;border-color:#e8a9b4}
.accepted {background:#e8f7ef}.rejected {background:#fff0f2}.pending {background:#fff8df}
del{color:#a91e39;background:#ffe4ea;text-decoration:line-through} ins{color:#096738;background:#d9f6e6;text-decoration:none}
.text{white-space:pre-wrap;overflow-wrap:anywhere;line-height:1.85}.change{border:1px solid #dce2ec;border-radius:9px;padding:12px;margin-top:10px}
.badge{font-size:12px;padding:3px 7px;border-radius:5px} .unchanged{color:#7a8799}
iframe {width:100%;height:calc(100vh - 175px);border:0;background:white;border-radius:10px} pre{white-space:pre-wrap;overflow-wrap:anywhere}
"""


def button(text, command, cls=""):
    return f'<button class="{cls}" onclick="pycmd({ESC(json.dumps(command), quote=True)})">{ESC(text)}</button>'


def diff_markup(p, changes, edits=None, editable=True):
    edits = edits or {}
    active = [change for change in changes if change.field not in edits]
    counts = {d: sum(c.decision == d for c in active) for d in ("pending", "accepted", "rejected")}
    title = {"add": "新增笔记", "update": "修改笔记", "delete": "移入回收站", "restore_deleted": "恢复笔记"}[p["action"]]
    out = [f'<h2>{title}</h2><div class="muted">待审 {counts["pending"]} · 接受 {counts["accepted"]} · 拒绝 {counts["rejected"]}</div>',
           f'<div class="card"><b>修改理由</b><div class="text">{ESC(p["reason"] or "未提供")}</div></div>']
    if p["action"] in ("delete", "restore_deleted"):
        out.append('<div class="card">回收站保留笔记、卡片 ID、复习历史及 FSRS 数据。删除时仅添加回收站标签并暂停卡片；可从历史生成恢复提案。</div>')
    fields = review_fields(changes, edits)
    for idx, field in enumerate(fields):
        subset = [c for c in changes if c.field == field]
        if field in edits:
            reset = button("撤回手动修改", f"clear-edit:{idx}") if editable else ""
            out.append(f'<section class="card"><div class="bar"><h3>{ESC(field)}</h3>{reset}</div>'
                       '<div class="muted">采用手动版本</div>'
                       f'<div class="text"><ins>{ESC(edits[field]) or "（清空字段）"}</ins></div></section>')
            continue
        out.append(f'<section class="card"><div class="bar"><h3>{ESC(field if field != "@action" else "整条操作")}</h3><div class="actions">' + button("接受字段", f"field:{idx}:accepted", "accept") + " " + button("拒绝字段", f"field:{idx}:rejected", "reject") + '</div></div>')
        if field != "@action":
            old = p.get("base", {}).get("fields", {}).get(field, "")
            cursor = 0
            inline = []
            for c in subset:
                inline.append(f'<span class="unchanged">{ESC(old[cursor:c.start])}</span><del>{ESC(c.old)}</del><ins>{ESC(c.new)}</ins>')
                cursor = c.end
            inline.append(f'<span class="unchanged">{ESC(old[cursor:])}</span>')
            out.append('<div class="text">' + "".join(inline) + '</div>')
        for n, c in enumerate(subset, 1):
            label = {"accepted": "已接受", "rejected": "已拒绝", "pending": "待审批"}[c.decision]
            out.append(f'<div id="c-{c.id}" class="change {c.decision}"><div class="bar"><span>修改 {n} <span class="badge">{label}</span></span><span>' + button("✓ 接受", f"decide:{c.id}:accepted", "accept") + " " + button("✕ 拒绝", f"decide:{c.id}:rejected", "reject") + " " + button("重审", f"decide:{c.id}:pending") + '</span></div>')
            # Show anchors so repeated words can be distinguished.
            if field != "@action":
                out.append(f'<div class="muted">原文位置 {c.start + 1}–{max(c.start + 1, c.end)} · 前文 {ESC(old[max(0,c.start-30):c.start])}</div>')
            out.append(f'<div class="text"><del>{ESC(c.old) or "（无）"}</del><br><ins>{ESC(c.new) or "（删除此处）"}</ins></div></div>')
        out.append('</section>')
    if not fields:
        out.append('<div class="card">没有内容差异。</div>')
    result = "".join(out)
    return result if editable else result.replace('<button ', '<button disabled ')


def review_fields(changes, edits):
    return list(dict.fromkeys([c.field for c in changes] + list(edits)))


def sandbox_document(content):
    # Templates and submitted fields are untrusted. The iframe permits NO
    # scripts, forms, popups or top navigation. Trusted parent MathJax renders
    # into its DOM; same-origin allows Anki's fonts without weakening CORS.
    origin = mw.serverURL().rstrip("/")
    content = re.sub(r"<script\b[^>]*>[\s\S]*?</script\s*>", "", content, flags=re.I)
    content = re.sub(r"<\/?(?:iframe|object|embed|base|meta|link|form)\b[^>]*>", "", content, flags=re.I)
    content = re.sub(r"\s+on[a-z]+\s*=\s*(?:\"[^\"]*\"|'[^']*'|[^\s>]+)", "", content, flags=re.I)
    content = re.sub(r"\[sound:([^\]]+)\]", lambda m: '<audio controls preload="metadata" src="' + origin + '/' + quote(m[1]) + '"></audio>', content)
    # Render output AV placeholders: retain a visible explanation if a custom
    # renderer emits a placeholder not mapped by the adapter.
    content = re.sub(r"\[anki:play:[^\]]+\]", '<span>🔊 音频（请在字段预览试听）</span>', content)
    frame_id = "preview-" + secrets.token_hex(8)
    csp = f"default-src 'none'; img-src {origin} data:; media-src {origin}; style-src 'unsafe-inline' {origin}; font-src {origin} data:; script-src 'none'; connect-src 'none'; base-uri {origin}; form-action 'none'"
    inner = f'''<!doctype html><html><head><meta http-equiv="Content-Security-Policy" content="{ESC(csp, quote=True)}"><base href="{origin}/"><style>body{{font:18px 'Microsoft YaHei',sans-serif;padding:20px;color:#253249;background:white;overflow-wrap:anywhere}}img{{max-width:100%}} audio{{max-width:100%}} .cloze{{color:#1565c0;font-weight:bold}}</style></head><body class="card">{content}</body></html>'''
    frame = '<iframe id="' + frame_id + '" sandbox="allow-same-origin" referrerpolicy="no-referrer" srcdoc="' + ESC(inner, quote=True) + '"></iframe>'
    script = '''<script>(()=>{const f=document.getElementById(FRAME);let done=false;
    const render=()=>{if(done||!f.contentDocument?.body||f.contentDocument.readyState!=="complete")return;
      done=true;MathJax.startup.promise.then(()=>MathJax.typesetPromise([f.contentDocument.body])).then(()=>{
        document.querySelectorAll('style[id^="MJX"],style[id^="MathJax"]').forEach(s=>f.contentDocument.head.appendChild(s.cloneNode(true)));
        f.dataset.ready="true";
      }).catch(e=>{f.dataset.error=String(e)});};f.addEventListener('load',render);setTimeout(render,200);})();</script>'''.replace('FRAME',json.dumps(frame_id))
    return frame + script


class ReviewDialog(QDialog):
    def __init__(self, store=None):
        flags = Qt.WindowType
        super().__init__(mw, flags.Dialog | flags.CustomizeWindowHint | flags.WindowTitleHint
                         | flags.WindowSystemMenuHint | flags.WindowCloseButtonHint
                         | flags.WindowMinMaxButtonsHint)
        self.store = store or Store(Path(mw.pm.profileFolder()) / "anki_ai_review" / "history.sqlite3")
        self.setWindowTitle("Anki AI Review · 内容审批中心")
        self.rows = []
        self.current = None
        self.changes = []
        self.deck_label = QLabel()
        self.deck_label.setTextFormat(Qt.TextFormat.PlainText)
        self.deck_label.setWordWrap(True)
        self.deck_label.setStyleSheet("background:#e7eefb;color:#183f79;padding:8px;border-radius:7px")
        self.type_label = QLabel()
        self.type_label.setTextFormat(Qt.TextFormat.PlainText)
        self.type_label.setWordWrap(True)
        self.busy = False
        self.closed = False
        self.preview_generation = 0
        self.last_preview = None
        self.history_mode = False
        self.history_rows = []
        self.setStyleSheet("QDialog{background:#f5f7fb} QPushButton{padding:7px 12px;background:white;color:#24334a;border:1px solid #c5cfdf;border-radius:6px} QPushButton:hover{background:#e7eefb} QPushButton:disabled{color:#8c97a8;background:#e5e9f0} QListWidget{background:white;color:#263850;border:1px solid #dce2ec;font-size:14px} QListWidget::item{padding:10px;border-bottom:1px solid #edf0f5} QListWidget::item:selected{background:#dceafd;color:#183f79} QLineEdit,QComboBox{padding:6px;background:white;color:#24334a;border:1px solid #c5cfdf;border-radius:6px} QComboBox QAbstractItemView{background:white;color:#24334a} QLabel{color:#34465f}")
        layout = QVBoxLayout(self)
        top = QHBoxLayout()
        profile_label = QLabel(f"Profile：{mw.pm.name}")
        profile_label.setTextFormat(Qt.TextFormat.PlainText)
        profile_label.setWordWrap(True)
        top.addWidget(profile_label)
        top.addStretch()
        self.add_button(top, "导入提案 JSON", self.import_file)
        self.add_button(top, "导出原文给 AI", self.export_notes)
        self.add_button(top, "导出提案 JSON", self.export_proposals)
        self.add_button(top, "导出审批记录", self.export_audit)
        self.history_btn = self.add_button(top, "历史与恢复", self.toggle_history)
        layout.addLayout(top)
        tools = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("筛选提案：标题、编号、理由")
        self.search.textChanged.connect(self.filter_list)
        tools.addWidget(self.search, 1)
        tools.addWidget(QLabel("审批粒度"))
        self.level = QComboBox()
        for label, value in (("精细 · 字词", "fine"), ("普通 · 句子", "normal"), ("粗略 · 段落", "coarse")):
            self.level.addItem(label, value)
        self.level.setCurrentIndex(1)
        self.level.currentIndexChanged.connect(self.change_level)
        tools.addWidget(self.level)
        self.add_button(tools, "接受选中笔记", lambda: self.decide_selected("accepted"))
        self.add_button(tools, "拒绝选中笔记", lambda: self.decide_selected("rejected"))
        layout.addLayout(tools)
        destination = QHBoxLayout()
        destination.addWidget(self.deck_label, 2)
        destination.addWidget(self.type_label, 1)
        self.type_btn = self.add_button(destination, "切换笔记类型…", self.change_note_type)
        self.type_btn.hide()
        layout.addLayout(destination)
        splitter = QSplitter(Qt.Orientation.Horizontal)
        self.list = QListWidget()
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.list.currentRowChanged.connect(self.select_row)
        splitter.addWidget(self.list)
        self.diff = AnkiWebView(self)
        self.diff.set_bridge_command(self.command, self)
        splitter.addWidget(self.diff)
        right = QWidget(self)
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        preview_top = QHBoxLayout()
        preview_top.addWidget(QLabel("最终生效预览"))
        self.preview_kind = QComboBox()
        self.preview_kind.addItems(["笔记字段", "卡片正面", "卡片背面", "字段 HTML"])
        self.preview_kind.currentIndexChanged.connect(self.show_preview)
        preview_top.addWidget(self.preview_kind)
        self.ordinal = QComboBox()
        self.ordinal.currentIndexChanged.connect(self.refresh_preview)
        preview_top.addWidget(self.ordinal)
        right_layout.addLayout(preview_top)
        self.preview = AnkiWebView(self)
        self.preview.set_bridge_command(lambda _: None, self)
        right_layout.addWidget(self.preview)
        splitter.addWidget(right)
        splitter.setSizes([250, 710, 540])
        layout.addWidget(splitter, 1)
        bottom = QHBoxLayout()
        self.status = QLabel("所有导入先待审；审批不会立即写入。")
        self.status.setWordWrap(True)
        bottom.addWidget(self.status, 1)
        self.undo_btn = self.add_button(bottom, "撤销最近写入", self.undo_last)
        self.edit_btn = self.add_button(bottom, "修改内容…", self.edit_draft)
        self.edit_btn.setEnabled(False)
        self.apply_btn = self.add_button(bottom, "写入当前笔记", self.apply_current)
        self.apply_btn.setEnabled(False)
        self.apply_btn.setStyleSheet("background:#2162ce;color:white;font-weight:bold;padding:10px 22px;border-radius:7px")
        self.restore_btn = self.add_button(bottom, "生成恢复提案", self.restore_history)
        self.restore_btn.hide()
        layout.addLayout(bottom)
        self.hook = self.operation_done
        gui_hooks.operation_did_execute.append(self.hook)
        self.finished.connect(self.cleanup)
        self.fit_to_work_area(mw.screen().availableGeometry())
        self.refresh()

    def fit_to_work_area(self, work_area):
        # Widget size excludes the native frame and title bar.
        usable = work_area.adjusted(12, 8, -12, -52)
        self.resize(min(1500, usable.width()), min(950, usable.height()))
        self.move(usable.x() + max(0, (usable.width() - self.width()) // 2),
                  usable.y() + max(0, (usable.height() - self.height()) // 2))

    def add_button(self, layout, text, callback):
        b = QPushButton(text)
        b.clicked.connect(callback)
        layout.addWidget(b)
        return b

    def cleanup(self, *_):
        if self.closed:
            return
        self.closed = True
        gui_hooks.operation_did_execute.remove(self.hook)
        self.preview_generation += 1
        self.diff.cleanup()
        self.preview.cleanup()

    def reject(self):
        if not self.busy:
            super().reject()

    def closeEvent(self, event):
        if self.busy:
            event.ignore()
        else:
            super().closeEvent(event)

    def error(self, exc):
        self.busy = False
        self.status.setText(str(exc))
        self.update_actions()
        showWarning(str(exc), parent=self)

    def operation_done(self, _changes, _initiator):
        if not self.busy and not self.closed:
            self.refresh()

    def refresh(self, *_):
        if self.busy or self.closed:
            return
        key = self.current["key"] if self.current else None
        self.busy = True
        def done(undo_name):
            if self.closed:
                return
            self.busy = False
            self.rows = self.store.proposals()
            self.history_rows = self.store.history()
            self.list.blockSignals(True)
            self.list.clear()
            data = self.history_rows if self.history_mode else self.rows
            target = 0
            states = STATUS_LABELS
            for i, row in enumerate(data):
                if self.history_mode:
                    label = f'{local_time(row["time"])}\n{states.get(row["state"], row["state"])} · {row["reason"][:55]}'
                else:
                    p, cs = load_review(row)
                    label = proposal_label(row)
                    if row["key"] == key:
                        target = i
                item = QListWidgetItem(label)
                item.setToolTip(row.get("reason", "") if self.history_mode else p["reason"])
                self.list.addItem(item)
            self.list.blockSignals(False)
            self.filter_list()
            if data:
                self.list.setCurrentRow(target)
            else:
                self.current = None
                self.update_actions()
                self.deck_label.clear()
                self.type_label.clear()
                self.set_diff('<h2>逐处审阅，按你的选择生效。</h2><div class="card">① 导出原文给 AI<br><br>② 导入 AI 的 JSON 提案<br><br>③ 逐处接受或拒绝，右侧预览<br><br>④ 写入当前笔记；可立即原生撤销</div>')
                self.preview.stdHtml('<p>尚未选择提案。</p>', css=["css/webview.css"], context=self)
            self.undo_btn.setEnabled(bool((undo_name or "").startswith("AI Review ")))
        def query(col):
            reconcile(col, self.store)
            return col.undo_status().undo
        QueryOp(parent=self, op=query, success=done).failure(self.error).run_in_background()

    def filter_list(self, *_):
        value = self.search.text().casefold()
        for i in range(self.list.count()):
            self.list.item(i).setHidden(value not in self.list.item(i).text().casefold())

    def select_row(self, index):
        if index < 0:
            return
        if self.history_mode:
            self.preview_generation += 1
            self.current = None
            self.update_actions()
            self.deck_label.clear()
            self.type_label.clear()
            h = self.history_rows[index]
            before = json.loads(h["before_json"])
            after = json.loads(h["after_json"])
            p = dict(action="update", reason=h["reason"], base=before or {"fields": {}}, fields=after["fields"])
            history_changes = make_changes(p, "fine")
            for change in history_changes:
                change.decision = "accepted"
            readonly = diff_markup(p, history_changes, editable=False)
            self.set_diff('<div class="muted">历史只读 · ' + ESC(STATUS_LABELS.get(h["state"], h["state"])) + '</div>' + readonly)
            approvals = ''.join('<div class="card"><b>' + ESC(c['field']) + ' · ' + ("手动修改" if c.get("source") == "manual" else {"accepted":"已接受", "rejected":"已拒绝", "pending":"待审", "overridden":"已采用手动版本"}.get(c['decision'], c['decision'])) + '</b><div class="text"><del>' + ESC(c['old']) + '</del><br><ins>' + ESC(c['new']) + '</ins></div></div>' for c in json.loads(h['decisions']))
            self.preview.stdHtml('<style>' + CSS + '</style><h2>逐处审批记录</h2>' + approvals, context=self)
            self.restore_btn.setEnabled(h["state"] == "applied")
            return
        self.current = self.rows[index]
        self.proposal, self.changes = load_review(self.current)
        self.level.blockSignals(True)
        self.level.setCurrentIndex(self.level.findData(self.current["level"]))
        self.level.blockSignals(False)
        self.sync_review()
        self.refresh_preview()

    def update_actions(self):
        editable = bool(self.current and self.current["status"] == "pending" and not self.history_mode)
        self.level.setEnabled(editable)
        self.edit_btn.setEnabled(editable and self.proposal["action"] in ("add", "update"))
        self.apply_btn.setEnabled(editable)
        self.type_btn.setVisible(editable and self.proposal["action"] == "add")
        if not self.current:
            return
        action = self.proposal["action"]
        label = {"add": "加入牌组", "update": "保存到原笔记", "delete": "移入回收站",
                 "restore_deleted": "恢复笔记"}[action]
        edits = manual_fields(self.current)
        if editable and not unresolved_changes(self.changes, edits) and not (
            edits or any(c.decision == "accepted" for c in self.changes)
        ):
            label = "结束审阅（全部拒绝）"
        self.apply_btn.setText(label)

    def sync_review(self, anchor=None):
        self.update_actions()
        edits = manual_fields(self.current)
        self.set_diff(diff_markup(self.proposal, self.changes, edits,
                                 editable=self.current["status"] == "pending"), anchor)
        if self.proposal["action"] == "add":
            self.deck_label.setText(f'目标牌组：{self.proposal["deck"]}')
            self.type_label.setText(f'笔记类型：{self.proposal["notetype"]}')
        else:
            self.deck_label.setText("现有卡片的牌组：正在读取…")
            self.type_label.clear()
        count = len(unresolved_changes(self.changes, edits))
        state = self.current["status"]
        if state == "applied" and self.proposal["action"] == "add":
            self.status.setText(f'已加入牌组：{self.proposal["deck"]}')
        elif state != "pending":
            self.status.setText(STATUS_LABELS.get(state, state))
        elif count:
            self.status.setText(f"还有 {count} 处待审。可接受、拒绝，或修改对应字段。")
        else:
            self.status.setText(f'审阅完成。点击「{self.apply_btn.text()}」确认。')

    def set_diff(self, content, anchor=None):
        if anchor:
            content += '<script>document.getElementById(' + json.dumps("c-" + anchor) + ')?.scrollIntoView({block:"center"})</script>'
        self.diff.stdHtml('<style>' + CSS + '</style>' + content, context=self)

    def save(self, detail, anchor=None):
        self.store.save_decisions(self.current["key"], self.current["level"], change_dicts(self.changes), detail)
        self.current["decisions"] = json.dumps(change_dicts(self.changes))
        self.sync_review(anchor)
        self.refresh_preview()
        i = self.list.currentRow()
        if i >= 0:
            self.list.item(i).setText(proposal_label(self.current))

    def command(self, command):
        if self.history_mode or self.busy or not self.current or self.current["status"] != "pending":
            return
        parts = command.split(":")
        edits = manual_fields(self.current)
        fields = review_fields(self.changes, edits)
        if len(parts) == 2 and parts[0] == "clear-edit" and parts[1].isdigit():
            index = int(parts[1])
            if index < len(fields) and fields[index] in edits:
                del edits[fields[index]]
                self.update_manual_fields(edits)
            return
        if len(parts) != 3 or parts[2] not in ("accepted", "rejected", "pending"):
            return
        if parts[0] == "decide":
            subset = [c for c in self.changes if c.id == parts[1]]
        elif parts[0] == "field" and parts[1].isdigit():
            if int(parts[1]) >= len(fields):
                return
            subset = [c for c in self.changes if c.field == fields[int(parts[1])]]
        else:
            return
        subset = [c for c in subset if c.field not in edits]
        if not subset:
            return
        for c in subset:
            c.decision = parts[2]
        self.save({"scope": parts[0], "changes": [c.id for c in subset], "decision": parts[2]}, subset[0].id if subset else None)

    def decide_selected(self, decision):
        if self.history_mode or self.busy:
            return
        indices = [self.list.row(item) for item in self.list.selectedItems()]
        for index in indices:
            row = self.rows[index]
            if row["status"] != "pending":
                continue
            p, changes = load_review(row)
            edits = manual_fields(row)
            for c in changes:
                if c.field not in edits:
                    c.decision = decision
            self.store.save_decisions(row["key"], row["level"], change_dicts(changes), {"scope": "selected_notes", "decision": decision})
        self.refresh()

    def change_level(self, *_):
        if not self.current or self.history_mode or self.busy:
            return
        level = self.level.currentData()
        if level == self.current["level"]:
            return
        if any(c.decision != "pending" for c in self.changes) and not askUser("调整粒度会清除当前笔记已有的审批决定，并重新拆分全部修改。继续？", parent=self):
            self.level.blockSignals(True)
            self.level.setCurrentIndex(self.level.findData(self.current["level"]))
            self.level.blockSignals(False)
            return
        self.current["level"] = level
        self.changes = make_changes(self.proposal, level)
        self.save({"scope": "granularity", "level": level, "reset": True})

    def update_manual_fields(self, edits):
        self.store.save_manual_edits(self.current["key"], edits)
        self.current["manual_fields"] = json.dumps(edits, ensure_ascii=False)
        self.sync_review()
        index = self.list.currentRow()
        if index >= 0:
            self.list.item(index).setText(proposal_label(self.current))
        self.refresh_preview()

    def edit_draft(self):
        if self.busy or not self.edit_btn.isEnabled():
            return
        proposal = self.proposal
        edits = manual_fields(self.current)
        fields = materialize(proposal, self.changes, edits, include_pending=True)
        try:
            note = draft_note(mw.col, proposal, fields)
            if not supports_note_type_picker(note.note_type()):
                showInfo("当前的提案编辑器不支持原生图片遮盖笔记。", parent=self)
                return

            def save(changed):
                if changed:
                    self.update_manual_fields({**edits, **changed})

            self.busy = True
            DraftDialog(self, note, save).exec()
        except (OSError, ValueError) as exc:
            self.error(exc)
        finally:
            self.busy = False

    def change_note_type(self):
        if self.busy or self.history_mode or not self.current:
            return
        proposal, row = self.proposal, self.current
        if row["status"] != "pending" or proposal["action"] != "add":
            return
        col = mw.col
        available = {
            model["name"]: [field["name"] for field in model["flds"]]
            for model in col.models.all() if supports_note_type_picker(model)
        }
        if not available:
            showInfo("当前集合没有可选的笔记类型。", parent=self)
            return
        self.busy = True
        try:
            picker = NoteTypePicker(self, proposal["notetype"], proposal["fields"],
                                    manual_fields(row), available)
            if not picker.exec():
                return
            selected = picker.type_choice.currentText()
            if selected == proposal["notetype"]:
                return
            change_add_notetype(col, self.store, row, selected, picker.mapping)
        except Exception as exc:
            self.error(exc)
            return
        finally:
            self.busy = False
        self.refresh()

    def refresh_preview(self, *_):
        if not self.current or self.history_mode:
            return
        self.preview_generation += 1
        generation = self.preview_generation
        proposal = json.loads(json.dumps(self.proposal))
        changes = copy.deepcopy(self.changes)
        edits = manual_fields(self.current)
        ordinal = self.ordinal.currentData() or 0
        def done(data):
            if generation != self.preview_generation:
                return
            decks, result = data
            if proposal["action"] != "add":
                self.deck_label.setText("现有卡片的牌组：" + ("、".join(decks) or "无可用牌组"))
            self.type_label.setText(f'笔记类型：{result["notetype"]}')
            self.last_preview = result
            self.ordinal.blockSignals(True)
            self.ordinal.clear()
            for value in result["ordinals"]:
                self.ordinal.addItem(f"卡片 {value + 1}", value)
            self.ordinal.setCurrentIndex(self.ordinal.findData(result["ordinal"]))
            self.ordinal.blockSignals(False)
            self.show_preview()
        def failed(exc):
            if generation == self.preview_generation:
                if proposal["action"] != "add":
                    self.deck_label.setText("现有卡片的牌组：暂不可用")
                self.last_preview = None
                self.preview.stdHtml('<style>' + CSS + '</style><div class="card">预览提示：' + ESC(str(exc)) + '</div>', context=self)
        def query(col):
            return proposal_decks(col, proposal), render_preview(col, proposal, changes, ordinal, edits)
        QueryOp(parent=self, op=query, success=done).failure(failed).run_in_background()

    def show_preview(self, *_):
        result = self.last_preview
        if not result or self.history_mode:
            return
        kind = self.preview_kind.currentIndex()
        if kind == 0:
            body = ''.join('<h3>' + ESC(name) + '</h3><div>' + value + '</div><hr>' for name, value in result["fields"].items())
        elif kind == 3:
            body = '<pre>' + ESC(json.dumps(result["fields"], ensure_ascii=False, indent=2)) + '</pre>'
        else:
            body = result["question" if kind == 1 else "answer"]
        self.preview.stdHtml('<style>' + CSS + '</style><h3>写入时将使用的内容</h3>' + sandbox_document(body), js=["js/mathjax.js", "js/vendor/mathjax/tex-chtml-full.js"], context=self)

    def import_file(self):
        if self.busy:
            return
        path, _ = QFileDialog.getOpenFileName(self, "导入 AI 提案", "", "JSON (*.json)")
        if not path:
            return
        try:
            if Path(path).stat().st_size > 10_000_000:
                raise ValueError("JSON 文件不能超过 10MB。")
            proposals = read_document(json.loads(Path(path).read_text(encoding="utf-8-sig")))
            count = self.store.import_proposals(proposals)
            self.status.setText(f"已导入 {count} 条新提案；其余重复提案已跳过。")
            if self.history_mode:
                self.toggle_history()
            else:
                self.refresh()
        except Exception as exc:
            self.error(exc)

    def export_notes(self):
        if self.busy:
            return
        search, ok = getText('Anki 搜索表达式（例如 deck:测试；最多 500 条）。\n仅导出原文，不修改集合。', parent=self, default='')
        if not ok:
            return
        path, _ = QFileDialog.getSaveFileName(self, "保存原文快照", "anki-ai-review-snapshot.json", "JSON (*.json)")
        if not path:
            return
        def done(data):
            try:
                Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
                self.status.setText(f'已导出 {len(data["notes"])} 条原文。AI 必须保留 base 快照不变。')
            except Exception as exc:
                self.error(exc)
        QueryOp(parent=self, op=lambda col: export_snapshot(col, self.store, search), success=done).failure(self.error).with_progress().run_in_background()

    def export_audit(self):
        path, _ = QFileDialog.getSaveFileName(self, "保存审批记录（含原文及历史）", "anki-ai-review-audit.json", "JSON (*.json)")
        if path:
            try:
                Path(path).write_text(json.dumps({"schema": SCHEMA, "audit": self.store.bundle()}, ensure_ascii=False, indent=2), encoding="utf-8")
                self.status.setText("审批记录已导出。该文件含笔记原文，请按个人资料保存。")
            except Exception as exc:
                self.error(exc)

    def export_proposals(self):
        path, _ = QFileDialog.getSaveFileName(self, "导出提案（再次导入时仍需审批）", "anki-ai-review-proposals.json", "JSON (*.json)")
        if path:
            try:
                proposals = [json.loads(row["payload"]) for row in self.store.proposals()]
                proposals = [{k:v for k,v in p.items() if k != "key"} for p in proposals if p["action"] in ("add", "update", "delete")]
                Path(path).write_text(json.dumps({"schema":SCHEMA, "proposals":proposals}, ensure_ascii=False, indent=2), encoding="utf-8")
                self.status.setText("已导出提案；审批决定和历史可另用“导出审批记录”保存。")
            except Exception as exc:
                self.error(exc)

    def toggle_history(self):
        if self.busy:
            return
        self.history_mode = not self.history_mode
        self.history_btn.setText("返回待审提案" if self.history_mode else "历史与恢复")
        self.apply_btn.setVisible(not self.history_mode)
        self.edit_btn.setVisible(not self.history_mode)
        self.restore_btn.setVisible(self.history_mode)
        self.refresh()

    def apply_current(self):
        if self.busy or not self.current or self.current["status"] != "pending":
            return
        edits = manual_fields(self.current)
        if unresolved_changes(self.changes, edits):
            showInfo("请先处理其余待审修改。右侧预览显示将写入的内容。", parent=self)
            return
        if not edits and not any(c.decision == "accepted" for c in self.changes):
            self.store.status(self.current["key"], "rejected")
            self.refresh()
            return
        key = self.current["key"]
        self.busy = True
        self.apply_btn.setEnabled(False)
        def done(result):
            self.busy = False
            self.status.setText(f"已写入笔记 {result.note_id}。可用撤销按钮或 Anki 编辑菜单撤销。")
            self.refresh()
        CollectionOp(self, lambda col: apply_review(col, self.store, key)).success(done).failure(self.error).run_in_background(initiator=self)

    def undo_last(self):
        if self.busy:
            return
        self.busy = True
        def undo(col):
            if not (col.undo_status().undo or "").startswith("AI Review "):
                raise ValueError("最近一步不是 AI Review 写入。请使用 Anki 的编辑菜单检查撤销顺序。")
            return col.undo()
        def done(_):
            self.busy = False
            self.status.setText("已通过 Anki 原生撤销恢复；历史记录保留。")
            self.refresh()
        CollectionOp(self, undo).success(done).failure(self.error).run_in_background(initiator=self)

    def restore_history(self):
        i = self.list.currentRow()
        if self.busy or i < 0:
            return
        h = self.history_rows[i]
        def done(p):
            self.store.import_proposals([p])
            self.toggle_history()
            self.status.setText("已生成待审批的恢复提案；请逐处审批后写入。")
        QueryOp(parent=self, op=lambda col: restoration_proposal(col, self.store, h), success=done).failure(self.error).run_in_background()


_dialog = None


def open_center():
    global _dialog
    if not mw.col:
        return
    if _dialog and _dialog.isVisible():
        if _dialog.isMinimized():
            _dialog.setWindowState(_dialog.windowState() & ~Qt.WindowState.WindowMinimized)
        _dialog.raise_()
        _dialog.activateWindow()
        return
    _dialog = ReviewDialog()
    _dialog.show()


def profile_closing():
    global _dialog
    if _dialog:
        _dialog.reject()
        _dialog = None
