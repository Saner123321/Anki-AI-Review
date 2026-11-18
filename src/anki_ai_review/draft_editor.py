"""Anki's regular field editor, backed by an unsaved Note."""

from html import escape
from sqlite3 import Error as DatabaseError

from aqt import gui_hooks, mw
from aqt.editor import Editor, EditorMode, EditorWebView
from aqt.qt import QDialog, QHBoxLayout, QLabel, QPushButton, Qt, QVBoxLayout, QWidget
from aqt.utils import showWarning

from .core import ContentError, validate_content


class DraftWebView(EditorWebView):
    def _processMime(self, mime, extended=False, drop_event=False):
        # Anki's image/URL paste otherwise imports media before saving the note.
        if mime.hasHtml():
            return mime.html(), False
        if mime.hasText() and not mime.hasUrls():
            return escape(mime.text()).replace("\n", "<br>"), False
        if mime.hasText() and all(not url.isLocalFile() for url in mime.urls()):
            return escape(mime.text()), False
        self.editor.media_notice()
        return "", False


class DraftEditor(Editor):
    def add_webview(self):
        self.web = DraftWebView(self.widget, self)
        self.web.set_bridge_command(self.onBridgeCmd, self)
        self.outerLayout.addWidget(self.web, 1)

    def setupWeb(self):
        origin = self.mw.serverURL().rstrip("/")
        policy = (
            f"default-src 'none'; script-src {origin}/_anki/; "
            f"style-src 'unsafe-inline' {origin}; img-src {origin} data:; "
            f"font-src {origin} data:; media-src {origin}; connect-src {origin}; "
            "object-src 'none'; base-uri 'self'; form-action 'none'"
        )

        def set_policy(content, context):
            if context is self:
                content.head = (
                    '<meta http-equiv="Content-Security-Policy" content="'
                    + escape(policy, quote=True) + '">' + content.head
                )

        gui_hooks.webview_will_set_content.append(set_policy)
        try:
            super().setupWeb()
        finally:
            gui_hooks.webview_will_set_content.remove(set_policy)

    def _save_current_note(self):
        # Even if called by an Anki extension, the draft is never written here.
        pass

    def _pastePreFilter(self, html, internal):
        # Use Anki's internal paste handling, which preserves local media links
        # and never fetches remote media or imports data URLs.
        return super()._pastePreFilter(html, True)

    def media_notice(self):
        showWarning("提案编辑器使用集合中已有的图片和音频。请先用 Anki 添加新媒体，再引用文件名。",
                    parent=self.parentWindow)

    def onBridgeCmd(self, cmd):
        if cmd in ("attach", "record", "addImageForOcclusion", "addImageForOcclusionFromClipboard"):
            self.media_notice()
            return None
        if cmd in ("fields", "cards", "saveTags") or cmd.startswith(("toggleSticky", "saveTags:")):
            return None
        return super().onBridgeCmd(cmd)


class DraftDialog(QDialog):
    def __init__(self, parent, note, save_fields):
        super().__init__(parent)
        self.setWindowTitle("修改提案内容")
        self.resize(900, 680)
        self.original = dict(note.items())
        self.save_fields = save_fields
        self.saving = False

        layout = QVBoxLayout(self)
        message = QLabel(
            "底稿包含已接受和待审建议，已拒绝部分保留原文。保存后，改动过的字段使用你的整段版本，其余字段继续审批。"
            "\n单击字段右侧的 </>，或在字段内按 Ctrl+Shift+X，可编辑 HTML 源码。"
        )
        message.setTextFormat(Qt.TextFormat.PlainText)
        message.setWordWrap(True)
        layout.addWidget(message)

        host = QWidget(self)
        layout.addWidget(host, 1)
        self.editor = DraftEditor(mw, host, self, editor_mode=EditorMode.ADD_CARDS)
        self.editor.set_note(note)

        buttons = QHBoxLayout()
        buttons.addStretch()
        cancel = QPushButton("取消", self)
        cancel.clicked.connect(self.reject)
        buttons.addWidget(cancel)
        self.save_button = QPushButton("保存到提案", self)
        self.save_button.clicked.connect(self.save)
        buttons.addWidget(self.save_button)
        layout.addLayout(buttons)
        self.finished.connect(self.cleanup)

    def reject(self):
        if not self.saving:
            super().reject()

    def save(self):
        if self.saving:
            return
        self.saving = True
        self.save_button.setEnabled(False)
        self.editor.call_after_note_saved(self._save_flushed_fields)

    def _save_flushed_fields(self):
        if self.editor.note is None:
            return
        fields = {name: value for name, value in self.editor.note.items()
                  if value != self.original[name]
                  and not (value == "<br>" and self.original[name] == "")}
        self.saving = False
        try:
            for field, value in fields.items():
                try:
                    validate_content(value)
                except ContentError as exc:
                    raise ContentError(f"{field}：{exc}") from exc
            self.save_fields(fields)
        except (OSError, ValueError, DatabaseError) as exc:
            self.save_button.setEnabled(True)
            showWarning(str(exc), parent=self)
            return
        self.accept()

    def cleanup(self, *_):
        self.editor.cleanup()
