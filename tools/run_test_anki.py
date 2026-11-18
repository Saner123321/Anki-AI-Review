"""Run a physically separate Anki base with its own instance lock and no account.

Uses the user's already installed Anki runtime. Never loads their normal profiles.
"""
import argparse
import json
import hashlib
import msvcrt
import subprocess
import sys
import traceback
import wave
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser()
parser.add_argument('--smoke', action='store_true')
parser.add_argument('--snapshot-smoke', action='store_true')
parser.add_argument('--base', default=str(ROOT / '.test-anki-user'))
args = parser.parse_args()
BASE = Path(args.base).resolve()
if args.snapshot_smoke and BASE.name != "Anki2":
    parser.error("--snapshot-smoke expects an isolated --base ending in Anki2.")
BASE.mkdir(parents=True, exist_ok=True)
if not BASE.is_relative_to(ROOT.resolve()):
    raise RuntimeError('Test base must be inside this project; normal Anki profile forbidden.')
launcher_lock = open(BASE / '.launcher.lock', 'a+b')
launcher_lock.seek(0)
if not launcher_lock.read(1):
    launcher_lock.write(b'0')
    launcher_lock.flush()
launcher_lock.seek(0)
try:
    msvcrt.locking(launcher_lock.fileno(), msvcrt.LK_NBLCK, 1)
except OSError:
    print('This independent test environment is already running. Use its existing window.')
    sys.exit(0)

import anki.lang
anki.lang.set_lang('en_US')
import aqt
from aqt.profiles import ProfileManager
from aqt.qt import QTimer

pm = ProfileManager(BASE)
pm.setupMeta()
pm.meta['defaultLang'] = 'zh_CN'
pm.meta['firstRun'] = False
pm.set_update_check(False)
pm.create('AI Review Test')
pm.load('AI Review Test')
pm.profile['syncKey'] = None
pm.profile['autoSync'] = False
pm.save()
pm.db.close()

version = json.loads((ROOT / 'src/anki_ai_review/manifest.json').read_text(encoding='utf-8'))['human_version']
package = ROOT / f'dist/Anki-AI-Review-{version}.ankiaddon'
artifacts = ROOT / 'artifacts' / version
artifacts.mkdir(parents=True, exist_ok=True)
with zipfile.ZipFile(package) as z:
    target = BASE / 'addons21/anki_ai_review'
    target.mkdir(parents=True, exist_ok=True)
    z.extractall(target)

# A separate local IPC name ensures a second process never forwards a file or
# action to the user's open, normal Anki. This is only a developer/test launcher.
aqt.AnkiApp.KEY = 'AnkiAIReview-Dev-' + hashlib.sha256(str(BASE).casefold().encode()).hexdigest()[:16]
aqt.AnkiApp.TMOUT = 1000


def start():
    try:
        if not aqt.mw.col:
            QTimer.singleShot(300, start)
            return
        from anki_ai_review.core import DEFAULT_NOTETYPE, SCHEMA, read_document
        from anki_ai_review.adapter import snapshot
        from anki_ai_review.store import Store
        from anki_ai_review.ui import open_center
        from aqt.addons import InstallOk
        install = aqt.mw.addonManager.install(str(package), force_enable=True)
        assert isinstance(install, InstallOk) and install.compatible, repr(install)
        col = aqt.mw.col
        store = Store(Path(aqt.mw.pm.profileFolder()) / 'anki_ai_review/history.sqlite3')
        deck = col.decks.id('AI Review 测试牌组')
        model = col.models.by_name('AI Review Demo Basic')
        if not model:
            model = col.models.new('AI Review Demo Basic')
            col.models.add_field(model, col.models.new_field('问题'))
            col.models.add_field(model, col.models.new_field('答案'))
            template = col.models.new_template('知识卡')
            template['qfmt'] = '{{问题}}'
            template['afmt'] = '{{FrontSide}}<hr id=answer>{{答案}}'
            col.models.add_template(model, template)
            col.models.add(model)
        front, back = [f['name'] for f in model['flds']][:2]
        if not col.models.by_name(DEFAULT_NOTETYPE):
            target = col.models.new(DEFAULT_NOTETYPE)
            for field in ('问题', '答案', '引用'):
                col.models.add_field(target, col.models.new_field(field))
            template = col.models.new_template('问答测试')
            template['qfmt'] = '{{问题}}'
            template['afmt'] = '{{FrontSide}}<hr>{{答案}}{{引用}}'
            col.models.add_template(target, template)
            col.models.add(target)
        # Demo media is synthetic and belongs only to this test collection.
        from aqt.qt import QImage, QColor
        image = QImage(340, 100, QImage.Format.Format_RGB32)
        image.fill(QColor('#cfe7ff'))
        image.save(str(Path(col.media.dir()) / 'ai-review-demo.png'))
        with wave.open(str(Path(col.media.dir()) / 'ai-review-demo.wav'), 'w') as wav:
            wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(8000)
            import math, struct
            wav.writeframes(b''.join(struct.pack('<h', int(4000 * math.sin(2*math.pi*440*i/8000))) for i in range(4000)))
        note = col.new_note(model)
        note[front] = '<div>苹果是红色。香蕉是黄色。</div>'
        note[back] = '<div>初始答案：\\(a^2+b^2=c^2\\)</div><img src="ai-review-demo.png"><br>[sound:ai-review-demo.wav]'
        col.add_note(note, deck)
        note.cards()[0].start_timer()
        col.set_config('fsrs', True)
        card = note.cards()[0]
        card.start_timer()
        col.sched.answerCard(card, 3)
        if args.snapshot_smoke:
            verify_background_snapshots(col, store, note.id)
            return
        proposals = [dict(id='demo-update-' + str(note.id), action='update', reason='演示：只接受苹果颜色变化，保留香蕉原文；公式、图片和音频保持完整。',
                          base=snapshot(col, note.id, store.identity),
                          fields={front:'<div>苹果是绿色。香蕉是蓝色。</div>', back:note[back].replace('初始答案', '勾股定理')}),
                     dict(id='demo-add-' + str(note.id), action='add', reason='演示新增笔记：先逐字段审阅，再写入。', notetype=model['name'], deck='AI Review 测试牌组', fields={front:'新增知识点：什么是间隔重复？', back:'根据记忆状态安排复习。'}),
                     dict(id='demo-default-' + str(note.id), action='add', reason='演示采用默认笔记类型。',
                          deck='AI Review 测试牌组', fields={'问题':'新增提案默认采用哪种笔记类型？','答案':DEFAULT_NOTETYPE})]
        document = {'schema': SCHEMA, 'proposals': proposals}
        read_document(document)
        (artifacts/'demo-proposals.json').write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding='utf-8')
        open_center()
        import anki_ai_review.ui as ui
        dialog = ui._dialog
        # Exercise the actual file-import handler with a deterministic test
        # file picker. No direct collection/API shortcut is used for import.
        from unittest.mock import patch
        def import_when_ready():
            if dialog.busy:
                QTimer.singleShot(250, import_when_ready)
                return
            with patch('anki_ai_review.ui.QFileDialog.getOpenFileName', return_value=(str(artifacts/'demo-proposals.json'), 'JSON')):
                dialog.import_file()
        QTimer.singleShot(250, import_when_ready)
        aqt.mw.setWindowTitle('Anki · AI Review 独立测试环境')
        if not args.smoke:
            return
        state = {'step':0, 'ticks':0, 'checks':[], 'original':dict(note.items()), 'cards':col.db.all('select * from cards where nid=?', note.id), 'revlog':col.db.all('select * from revlog')}
        def check(name, condition):
            assert condition, name
            state['checks'].append(name)

        def pick_test_type():
            from anki_ai_review.notetype_picker import NoteTypePicker
            picker = dialog.findChild(NoteTypePicker)
            try:
                check('note type picker opened for pending add', picker is not None)
                picker.type_choice.setCurrentText(DEFAULT_NOTETYPE)
                check('picker suggested mappings for existing fields',
                      picker.inputs['问题'].currentData() == front
                      and picker.inputs['答案'].currentData() == back
                      and picker.inputs['引用'].currentData() is None)
                picker.confirm()
            except Exception:
                if picker is not None:
                    picker.reject()
                failure(traceback.format_exc())

        def edit_pending_note():
            from anki_ai_review.draft_editor import DraftDialog
            draft = dialog.findChild(DraftDialog)
            if draft is None:
                failure("Draft editor did not open")
                return

            def on_source_edited(result):
                try:
                    check("native HTML mode saved the field's markup", result == "<b>人工修改的问题</b>")
                    draft.save_button.click()
                except Exception:
                    draft.reject()
                    failure(traceback.format_exc())

            def on_edited(result):
                if not isinstance(result, dict) or "edited" not in result:
                    attempts = state.get("editor_probe_attempts", 0) + 1
                    state["editor_probe_attempts"] = attempts
                    if attempts <= 20:
                        QTimer.singleShot(250, edit_pending_note)
                        return
                try:
                    check("native rich text field was editable",
                          isinstance(result, dict) and result.get("edited") == "人工修改的问题")
                    check("Anki HTML mode toggle was available", result.get("toggled"))
                    QTimer.singleShot(300, lambda: draft.editor.web.evalWithCallback("""
                        (() => {
                            const all = (node) => Array.from(node.querySelectorAll("*")).flatMap(
                                element => [element, ...(element.shadowRoot ? all(element.shadowRoot) : [])]
                            );
                            const source = all(document).map(element => element.CodeMirror)
                                .find(cm => cm && cm.getValue().includes('人工修改的问题'));
                            if (!source) return null;
                            source.focus();
                            source.setValue('<b>人工修改的问题</b>');
                            return source.getValue();
                        })()
                    """, on_source_edited))
                except Exception:
                    draft.reject()
                    failure(traceback.format_exc())

            draft.editor.web.evalWithCallback("""
                (() => {
                    const all = (node) => Array.from(node.querySelectorAll("*")).flatMap(
                        element => [element, ...(element.shadowRoot ? all(element.shadowRoot) : [])]
                    );
                    const fields = all(document).filter(element => element.isContentEditable &&
                        element.hasAttribute('contenteditable'));
                    const field = fields.find(element =>
                        element.textContent.includes('间隔重复')
                    );
                    if (!field) return {fields:fields.map(el => [el.className, el.textContent]),
                        body: document.body.innerText.slice(0,400), saveNow: typeof saveNow};
                    field.focus();
                    const range = document.createRange();
                    range.selectNodeContents(field);
                    const selection = field.getRootNode().getSelection?.() || window.getSelection();
                    selection.removeAllRanges();
                    selection.addRange(range);
                    document.execCommand('insertText', false, '人工修改的问题');
                    if (field.textContent !== '人工修改的问题') {
                        field.textContent = '人工修改的问题';
                        field.dispatchEvent(new InputEvent('input', {bubbles: true, composed: true}));
                    }
                    const edited = field.textContent;
                    const toggle = all(document).find(el => el.matches('.plain-text-badge'));
                    toggle?.click();
                    return {edited, toggled: !!toggle};
                })()
            """, on_edited)

        def tick():
            try:
                state['ticks'] += 1
                (artifacts/'ui-progress.json').write_text(json.dumps({'step':state['step'], 'ticks':state['ticks'], 'busy':dialog.busy}), encoding='utf-8')
                if state['ticks'] > 240:
                    raise TimeoutError(f"UI test timeout at step {state['step']}")
                if dialog.busy or (state['step'] < 7 and not dialog.current):
                    QTimer.singleShot(250, tick); return
                step = state['step']
                if step == 0:
                    index = next(i for i,r in enumerate(dialog.rows) if json.loads(r['payload'])['id'] == proposals[0]['id'])
                    dialog.list.setCurrentRow(index)
                    dialog.level.setCurrentIndex(dialog.level.findData('fine'))
                    state['step'] = 1
                elif step == 1:
                    if not dialog.last_preview:
                        QTimer.singleShot(250, tick); return
                    check('real package installer accepted manifest', install.compatible)
                    check('UI loaded a pending proposal', dialog.current['status'] == 'pending')
                    dialog.diff.eval("document.querySelector('button.accept').click()")
                    state['step'] = 2
                elif step == 2:
                    if not any(c.decision == 'accepted' for c in dialog.changes):
                        QTimer.singleShot(250, tick); return
                    check('web button crossed native bridge and saved decision', True)
                    # Reset and then exercise individual changes through UI bridge.
                    for c in dialog.changes:
                        decision = 'accepted' if c.field == front and '绿' in c.new else 'rejected'
                        dialog.command(f'decide:{c.id}:{decision}')
                    state['step'] = 3
                elif step == 3:
                    if dialog.last_preview['fields'][front] != '<div>苹果是绿色。香蕉是黄色。</div>':
                        QTimer.singleShot(250, tick); return
                    check('live preview reflects mixed decisions in same field', True)
                    check('live preview preserved media and math HTML', dialog.last_preview['fields'][back] == note[back])
                    dialog.grab().save(str(artifacts/'approval-center.png'))
                    dialog.preview_kind.setCurrentIndex(2)
                    state['step'] = 4
                elif step == 4:
                    # Inspect our own preview DOM, including a script-free
                    # iframe, MathJax output, local image and playable audio.
                    def report(value):
                        state['preview_dom'] = value
                    dialog.preview.page().runJavaScript("(()=>{let f=document.querySelector('iframe');let d=f?.contentDocument;return {sandbox:f?.getAttribute('sandbox'),math:d?.querySelectorAll('mjx-container').length,image:d?.querySelector('img')?.naturalWidth,audioDuration:d?.querySelector('audio')?.duration,ready:f?.dataset.ready}})()", report)
                    dialog.grab().save(str(artifacts/'card-preview.png'))
                    dialog.apply_btn.click()
                    state['step'] = 5
                elif step == 5:
                    if dialog.current['status'] != 'applied':
                        QTimer.singleShot(250, tick); return
                    check('UI wrote only the individually accepted change', col.get_note(note.id)[front] == '<div>苹果是绿色。香蕉是黄色。</div>')
                    check('UI write kept all existing card rows', col.db.all('select * from cards where nid=?', note.id) == state['cards'])
                    check('UI write kept review log', col.db.all('select * from revlog') == state['revlog'])
                    dom = state.get('preview_dom', {})
                    check('card preview rendered MathJax', bool(dom.get('math')))
                    check('card preview loaded local image', bool(dom.get('image')))
                    check('card preview decoded local audio metadata', dom.get('audioDuration',0) > 0)
                    check('untrusted preview has no script permission', dom.get('sandbox') == 'allow-same-origin')
                    dialog.undo_btn.click()
                    state['step'] = 6
                elif step == 6:
                    if dialog.current['status'] != 'undone':
                        QTimer.singleShot(250, tick); return
                    check('UI native undo restored original fields', dict(col.get_note(note.id).items()) == state['original'])
                    check('UI undo was recorded in SQLite history', store.history()[0]['state'] == 'undone')
                    dialog.history_btn.click()
                    state['step'] = 7
                elif step == 7:
                    if not dialog.history_mode:
                        QTimer.singleShot(250, tick); return
                    dialog.grab().save(str(artifacts/'history.png'))
                    dialog.history_btn.click()
                    state['step'] = 8
                elif step == 8:
                    from aqt.qt import QApplication, QRect, Qt
                    implicit = next(json.loads(row['payload']) for row in dialog.rows
                                    if json.loads(row['payload'])['id'] == proposals[2]['id'])
                    check('GUI import defaulted to XXHK and kept reference blank',
                          implicit['notetype'] == DEFAULT_NOTETYPE and implicit['fields']['引用'] == '')
                    index = next(i for i,r in enumerate(dialog.rows) if json.loads(r['payload'])['id'] == proposals[1]['id'])
                    dialog.list.setCurrentRow(index)
                    check('new note shows target deck and explicit Add to Deck action',
                          dialog.deck_label.text() == '目标牌组：AI Review 测试牌组' and dialog.apply_btn.text() == '加入牌组')
                    flags = dialog.windowFlags()
                    controls = Qt.WindowType.WindowMinMaxButtonsHint | Qt.WindowType.WindowCloseButtonHint
                    check('review window requests native minimize, maximize and close buttons',
                          (flags & controls) == controls)
                    work_area = QRect(dialog.screen().availableGeometry().topLeft(), dialog.size())
                    work_area.setWidth(760)
                    work_area.setHeight(480)
                    dialog.fit_to_work_area(work_area)
                    QApplication.processEvents()
                    check('window fits a 760 x 480 work area with its native frame',
                          work_area.contains(dialog.frameGeometry()))
                    check('edit and Add to Deck buttons fit inside the window',
                          all(button.isVisible() and dialog.rect().contains(button.geometry())
                              for button in (dialog.edit_btn, dialog.apply_btn)))
                    dialog.fit_to_work_area(dialog.screen().availableGeometry())
                    state['count_before_draft'] = len(col.find_notes(''))
                    QTimer.singleShot(400, pick_test_type)
                    dialog.type_btn.click()
                    state['step'] = 11
                elif step == 11:
                    check('selected note type updated without creating a note',
                          dialog.type_label.text() == '笔记类型：' + DEFAULT_NOTETYPE
                          and len(col.find_notes('')) == state['count_before_draft'])
                    QTimer.singleShot(1000, edit_pending_note)
                    dialog.edit_btn.click()
                    state['step'] = 9
                elif step == 9:
                    edits = json.loads(dialog.current['manual_fields'])
                    check('native editor saved only the changed field to the pending proposal',
                          list(edits) == [front] and '人工修改的问题' in edits[front])
                    check('saving the draft did not add a note', len(col.find_notes('')) == state['count_before_draft'])
                    check('manual edits immediately appear in card preview',
                          '人工修改的问题' in dialog.last_preview['question'])
                    for change in dialog.changes:
                        if change.field == back:
                            dialog.command(f'decide:{change.id}:accepted')
                    dialog.apply_btn.click()
                    state['step'] = 10
                elif step == 10:
                    if dialog.current['status'] != 'applied':
                        QTimer.singleShot(250, tick); return
                    history = store.history()[0]
                    created = col.get_note(json.loads(history['after_json'])['note_id'])
                    check('Add to Deck wrote edited fields and used displayed deck',
                          created[front] == '<b>人工修改的问题</b>' and [c.did for c in created.cards()] == [deck])
                    check('Add to Deck used the selected note type',
                          created.note_type()['name'] == DEFAULT_NOTETYPE)
                    (artifacts/'ui-smoke.json').write_text(json.dumps({'result':'passed', 'checks':state['checks'], 'preview_dom':state.get('preview_dom'), 'base':str(BASE)}, ensure_ascii=False, indent=2), encoding='utf-8')
                    dialog.reject()
                    aqt.mw.unloadProfileAndExit()
                    return
                QTimer.singleShot(1000, tick)
            except Exception:
                failure(traceback.format_exc())
        QTimer.singleShot(2000, tick)
    except Exception:
        failure(traceback.format_exc())


def failure(message):
    (artifacts/'ui-error.txt').write_text(message, encoding='utf-8')
    print(message, flush=True)
    if args.smoke or args.snapshot_smoke:
        aqt.mw.app.exit(1)


def verify_background_snapshots(col, store, note_id):
    from anki_ai_review.adapter import export_snapshot
    from anki_ai_review.ui import _dialog

    assert _dialog is None, "The review window must stay closed for this test."
    expected = export_snapshot(col, store, f"nid:{note_id}")["notes"][0]
    script = ROOT / "mcp-runtime" / "verify-review-snapshots.mjs"
    process = subprocess.Popen(
        ["node.exe", str(script), str(BASE), "AI Review Test", str(note_id)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )

    def complete():
        if process.poll() is None:
            QTimer.singleShot(100, complete)
            return
        try:
            stdout, stderr = process.communicate()
            assert process.returncode == 0, stderr.decode("utf-8", errors="replace")
            result = json.loads(stdout)
            assert result["snapshot"] == expected, "MCP and Anki produced different base snapshots."
            assert result["queued"]["proposals"][0]["status"] == "pending"
            assert col.get_note(note_id)["问题"] == expected["fields"]["问题"]
            report = {
                "result": "passed",
                "checks": ["read-only MCP inventory", "search and ID export agree",
                           "wrong profile and missing note rejected", "exact snapshot from native Anki",
                           "update queued without a write", "approval window stayed closed"],
            }
            (artifacts / "snapshot-smoke.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
            print(json.dumps(report), flush=True)
            aqt.mw.unloadProfileAndExit()
        except Exception:
            failure(traceback.format_exc())

    QTimer.singleShot(100, complete)


aqt.gui_hooks.profile_did_open.append(lambda: QTimer.singleShot(1000, start))
aqt._run(['Anki AI Review Test', '-b', str(BASE), '-p', 'AI Review Test', '-l', 'zh_CN'])
