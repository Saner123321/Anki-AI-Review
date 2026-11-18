import itertools
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from anki.collection import Collection
from anki_ai_review.core import (ContentError, DEFAULT_NOTETYPE, SCHEMA, TRASH_TAG, change_dicts, field_diff,
                                 fingerprint, make_changes, materialize, read_document, validate_content)
from anki_ai_review.store import Store
from anki_ai_review.adapter import (apply_review, change_add_notetype, export_snapshot,
                                    field_mapping, load_review, manual_fields, reconcile,
                                    render_preview, restoration_proposal, snapshot)


class DiffTests(unittest.TestCase):
    def test_every_partial_combination_preserves_markup_and_atoms(self):
        pairs = [
            ("<div>甲是红色。乙是蓝色。</div>", "<div>甲是绿色。乙是黄色。</div>"),
            ("x {{c1::old::hint}} y \\(a+b\\) z [sound:a.mp3]", "q {{c1::new::help}} y \\(a-b\\) z [sound:b.mp3]"),
            ("<b>原文</b>", "<i>新文</i>"),
            ("{{c1::A {{c2::B}} C}} extra", "{{c1::D {{c2::E}} C}} other"),
        ]
        for old, new in pairs:
            for level in ("fine", "normal", "coarse"):
                cs = field_diff("F", old, new, level)
                self.assertLess(len(cs), 12)
                p = {"action":"update", "base":{"fields":{"F":old}}, "fields":{"F":new}}
                for accepted in itertools.product((False, True), repeat=len(cs)):
                    for c, yes in zip(cs, accepted):
                        c.decision = "accepted" if yes else "rejected"
                    result = materialize(p, cs)["F"]
                    validate_content(result)
                    for atom in ("{{c1::", "\\(", "[sound:"):
                        self.assertEqual(result.count(atom), old.count(atom))
                for c in cs:
                    c.decision = "accepted"
                self.assertEqual(materialize(p, cs)["F"], new)
                for c in cs:
                    c.decision = "rejected"
                self.assertEqual(materialize(p, cs)["F"], old)

    def test_safety_rejects_active_content_and_invalid_markup(self):
        for text in ('<script>alert(1)</script>', '<img src="https://x/x">', '<img onerror="pycmd(1)">', '<b>x</i>', '{{c1::x', '\\(a', '<div>x', '<iframe srcdoc="x"></iframe>'):
            with self.subTest(text=text), self.assertRaises(ContentError):
                validate_content(text)

    def test_three_granularities(self):
        old, new = '苹果是红色。香蕉是黄色。\n水是液体。', '苹果是绿色。香蕉是蓝色。\n水是气体。'
        self.assertGreaterEqual(len(field_diff('F', old, new, 'fine')), len(field_diff('F', old, new, 'normal')))
        self.assertGreaterEqual(len(field_diff('F', old, new, 'normal')), len(field_diff('F', old, new, 'coarse')))

    def test_default_note_type_and_explicit_cloze(self):
        items = [
            dict(id='a', action='add', deck='Default', fields={'问题': '题目', '答案': '答案'}),
            dict(id='b', action='add', deck='Default', notetype='Cloze',
                 fields={'Text': '{{c1::题目}}', 'Back Extra': ''}),
        ]
        default, explicit = read_document({'schema': SCHEMA, 'proposals': items})
        self.assertEqual(default['notetype'], DEFAULT_NOTETYPE)
        self.assertEqual(default['fields'], {'问题': '题目', '答案': '答案', '引用': ''})
        self.assertEqual(explicit['notetype'], 'Cloze')


class RealAnkiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.col = Collection(str(self.root / 'collection.anki2'))
        self.store = Store(self.root / 'review.sqlite3')
        self.model = self.col.models.by_name('Basic')
        self.n = self.col.new_note(self.model)
        self.n['Front'] = '苹果是红色。香蕉是黄色。'
        self.n['Back'] = '初始答案。'
        self.col.add_note(self.n, 1)

    def tearDown(self):
        self.col.close()
        self.tmp.cleanup()

    def proposal(self, action='update', **extra):
        p = dict(id='case-' + str(len(self.store.proposals())), action=action, reason='测试原因')
        if action != 'add':
            p['base'] = snapshot(self.col, self.n.id, self.store.identity)
        if action == 'update':
            p['fields'] = {'Front':'苹果是绿色。香蕉是蓝色。', 'Back':'更新答案。'}
        if action == 'add':
            p.update(notetype='Basic', deck='Default', fields={'Front':'新笔记', 'Back':'新答案'})
        p.update(extra)
        p = read_document({'schema':SCHEMA,'proposals':[p]})[0]
        self.store.import_proposals([p])
        return p

    def approve(self, p, decision='accepted', level='fine'):
        cs = make_changes(p, level)
        for c in cs:
            c.decision = decision
        self.store.save_decisions(p['key'], level, change_dicts(cs), {'test':True})
        return cs

    def test_import_never_writes_and_ai_approval_ignored(self):
        before = snapshot(self.col, self.n.id, self.store.identity)
        p = self.proposal(approved=True, status='approved', decisions=[{'decision':'accepted'}])
        self.assertEqual(snapshot(self.col, self.n.id, self.store.identity), before)
        with self.assertRaises(ContentError):
            apply_review(self.col, self.store, p['key'])
        self.assertEqual(self.store.import_proposals([p]), 0)

    def test_partial_update_native_undo_redo_and_history(self):
        p = self.proposal()
        cs = self.approve(p)
        for c in cs:
            c.decision = 'accepted' if c.field == 'Front' and '绿' in c.new else 'rejected'
        self.store.save_decisions(p['key'], 'fine', change_dicts(cs), {'partial':True})
        expected = materialize(p, cs)
        out = apply_review(self.col, self.store, p['key'])
        self.assertEqual(dict(self.col.get_note(self.n.id).items()), expected)
        self.assertEqual(expected['Back'], '初始答案。')
        self.assertIn('黄色', expected['Front'])
        with self.assertRaises(ContentError):
            apply_review(self.col, self.store, p['key'])
        self.col.undo()
        reconcile(self.col, self.store)
        self.assertEqual(self.store.history()[0]['state'], 'undone')
        self.assertEqual(self.col.get_note(self.n.id)['Front'], p['base']['fields']['Front'])
        self.col.redo()
        reconcile(self.col, self.store)
        self.assertEqual(self.store.history()[0]['state'], 'applied')
        self.assertEqual(out.note_id, self.n.id)

    def test_conflict_and_cross_profile_block(self):
        p = self.proposal()
        self.approve(p)
        self.n['Back'] = '用户随后修改'
        self.col.update_note(self.n)
        with self.assertRaisesRegex(ContentError, '版本冲突'):
            apply_review(self.col, self.store, p['key'])
        self.assertEqual(self.col.get_note(self.n.id)['Back'], '用户随后修改')
        self.store.identity = 'different'
        with self.assertRaisesRegex(ContentError, '另一 Profile'):
            apply_review(self.col, self.store, p['key'])

    def test_schema_conflict(self):
        p = self.proposal()
        self.approve(p)
        self.model['css'] += '\n.card{color:red}'
        self.col.models.save(self.model)
        with self.assertRaisesRegex(ContentError, '版本冲突'):
            apply_review(self.col, self.store, p['key'])

    def test_review_fsrs_and_card_rows_unchanged_by_content_edit(self):
        self.col.set_config('fsrs', True)
        card = self.n.cards()[0]
        card.start_timer()
        self.col.sched.answerCard(card, 3)
        self.assertIsNotNone(card.memory_state)
        self.assertGreater(self.col.db.scalar('SELECT count(*) FROM revlog'), 0)
        cards = self.col.db.all('SELECT * FROM cards')
        reviews = self.col.db.all('SELECT * FROM revlog')
        p = self.proposal()
        self.approve(p)
        apply_review(self.col, self.store, p['key'])
        self.assertEqual(self.col.db.all('SELECT * FROM cards'), cards)
        self.assertEqual(self.col.db.all('SELECT * FROM revlog'), reviews)
        self.col.undo()
        self.assertEqual(self.col.db.all('SELECT * FROM cards'), cards)
        self.assertEqual(self.col.db.all('SELECT * FROM revlog'), reviews)

    def test_add_native_undo_and_crash_reconciliation(self):
        p = self.proposal('add')
        self.approve(p)
        out = apply_review(self.col, self.store, p['key'])
        self.assertEqual(self.col.get_note(out.note_id)['Front'], '新笔记')
        self.col.undo()
        reconcile(self.col, self.store)
        self.assertEqual(self.store.history()[0]['state'], 'undone')
        self.col.redo()
        reconcile(self.col, self.store)
        self.assertEqual(self.store.history()[0]['state'], 'applied')

    def test_manual_draft_persists_and_overrides_only_edited_fields(self):
        p = self.proposal(fields={'Back': 'AI 提议的回答。'})
        row = self.store.proposals()[0]
        _, changes = load_review(row)
        self.assertEqual(materialize(p, changes, include_pending=True)['Back'], p['fields']['Back'])
        self.store.save_manual_edits(p['key'], {'Front': '<b>人工改写的问题</b>'})
        self.assertEqual(self.col.get_note(self.n.id)['Front'], p['base']['fields']['Front'])
        reopened = Store(self.store.path)
        with self.assertRaisesRegex(ContentError, '每一处'):
            apply_review(self.col, reopened, p['key'])
        self.approve(p, 'rejected')
        apply_review(self.col, reopened, p['key'])
        self.assertEqual(self.col.get_note(self.n.id)['Front'], '<b>人工改写的问题</b>')
        self.assertEqual(self.col.get_note(self.n.id)['Back'], p['base']['fields']['Back'])
        decisions = json.loads(reopened.history()[0]['decisions'])
        self.assertEqual(decisions[-1]['source'], 'manual')
        self.col.undo()
        self.assertEqual(self.col.get_note(self.n.id)['Front'], p['base']['fields']['Front'])

    def test_discarding_manual_version_restores_pending_ai_changes(self):
        p = self.proposal()
        self.store.save_manual_edits(p['key'], {'Front': '手动问题', 'Back': '手动答案'})
        self.store.save_manual_edits(p['key'], {})
        with self.assertRaisesRegex(ContentError, '每一处'):
            apply_review(self.col, self.store, p['key'])
        self.assertEqual(self.col.get_note(self.n.id)['Front'], p['base']['fields']['Front'])

    def test_add_uses_displayed_target_even_if_template_overrides_deck(self):
        intended = self.col.decks.id('Display target')
        override = self.col.decks.id('Override')
        self.model['tmpls'][0]['did'] = override
        self.col.models.update_dict(self.model)
        p = self.proposal('add', deck='Display target')
        self.approve(p)
        out = apply_review(self.col, self.store, p['key'])
        self.assertEqual([card.did for card in self.col.get_note(out.note_id).cards()], [intended])
        self.assertEqual(self.col.models.get(self.model['id'])['tmpls'][0]['did'], override)
        self.col.undo()
        self.assertEqual(self.col.find_notes('deck:"Display target"'), [])

    def test_change_add_note_type_remaps_approval_and_edits_without_writing(self):
        model = self.col.models.new(DEFAULT_NOTETYPE)
        for field in ('问题', '答案', '引用'):
            self.col.models.add_field(model, self.col.models.new_field(field))
        template = self.col.models.new_template('问答测试')
        template['qfmt'] = '{{问题}}'
        template['afmt'] = '{{答案}}<hr>{{引用}}'
        self.col.models.add_template(model, template)
        self.col.models.add(model)

        proposal = self.proposal('add')
        changes = self.approve(proposal)
        changes[1].decision = 'rejected'
        self.store.save_decisions(proposal['key'], 'fine', change_dicts(changes), {})
        self.store.save_manual_edits(proposal['key'], {'Back': '手动修改答案'})
        row = self.store.proposals()[0]
        mapping = field_mapping(proposal['fields'], ('问题', '答案', '引用'))
        self.assertEqual(mapping, {'问题': 'Front', '答案': 'Back'})
        change_add_notetype(self.col, self.store, row, DEFAULT_NOTETYPE, mapping)
        self.assertEqual(len(self.col.find_notes('')), 1)
        reopened = Store(self.store.path)
        row = reopened.proposals()[0]
        chosen, changes = load_review(row)
        self.assertEqual(json.loads(row['payload'])['notetype'], 'Basic')
        self.assertEqual(chosen['notetype'], DEFAULT_NOTETYPE)
        self.assertEqual({c.field: c.decision for c in changes}, {'问题': 'accepted', '答案': 'rejected'})
        self.assertEqual(manual_fields(row), {'答案': '手动修改答案'})
        preview = render_preview(self.col, chosen, changes, edits=manual_fields(row))
        self.assertIn('手动修改答案', preview['answer'])
        result = apply_review(self.col, reopened, proposal['key'])
        note = self.col.get_note(result.note_id)
        self.assertEqual(note.note_type()['name'], DEFAULT_NOTETYPE)
        self.assertEqual(dict(note.items()), {'问题': '新笔记', '答案': '手动修改答案', '引用': ''})

    def test_cannot_switch_type_of_existing_note_update(self):
        proposal = self.proposal()
        row = self.store.proposals()[0]
        with self.assertRaisesRegex(ContentError, '新增'):
            change_add_notetype(self.col, self.store, row, 'Cloze', {})
        self.assertEqual(self.col.get_note(self.n.id).mid, proposal['base']['mid'])

    def test_delete_recycle_and_restoration_retains_review_ids(self):
        card = self.n.cards()[0]
        card.start_timer()
        self.col.sched.answerCard(card, 3)
        reviews = self.col.db.all('SELECT * FROM revlog')
        original = self.col.get_card(card.id)
        p = self.proposal('delete')
        self.approve(p)
        apply_review(self.col, self.store, p['key'])
        self.assertIn(TRASH_TAG, self.col.get_note(self.n.id).tags)
        self.assertEqual(self.col.get_card(card.id).queue, -1)
        restore = restoration_proposal(self.col, self.store, self.store.history()[0])
        self.store.import_proposals([restore])
        self.approve(restore)
        apply_review(self.col, self.store, restore['key'])
        restored = self.col.get_card(card.id)
        for attr in ('id', 'nid', 'queue', 'due', 'ivl', 'reps', 'lapses', 'factor', 'custom_data', 'memory_state'):
            self.assertEqual(getattr(restored, attr), getattr(original, attr), attr)
        self.assertEqual(self.col.db.all('SELECT * FROM revlog'), reviews)
        self.assertNotIn(TRASH_TAG, self.col.get_note(self.n.id).tags)

    def test_restoration_is_pending_not_direct_write(self):
        p = self.proposal()
        self.approve(p)
        apply_review(self.col, self.store, p['key'])
        h = self.store.history()[0]
        restore = restoration_proposal(self.col, self.store, h)
        self.store.import_proposals([restore])
        with self.assertRaises(ContentError):
            apply_review(self.col, self.store, restore['key'])
        self.approve(restore)
        apply_review(self.col, self.store, restore['key'])
        self.assertEqual(self.col.get_note(self.n.id)['Front'], p['base']['fields']['Front'])

    def test_missing_media_and_unknown_fields_are_blocked(self):
        for fields in ({'Front':'<img src="missing.png">'}, {'Unknown':'x'}, {'Front':'[sound:../a.mp3]'}):
            p = self.proposal(fields=fields)
            self.approve(p)
            with self.assertRaises(ContentError):
                apply_review(self.col, self.store, p['key'])

    def test_cloze_update_preserves_old_cards_and_previews_new_card(self):
        n = self.col.new_note(self.col.models.by_name('Cloze'))
        n['Text'] = 'A {{c1::one}} B {{c2::two}}'
        self.col.add_note(n, 1)
        self.n = n
        old_ids = n.card_ids()
        p = self.proposal(fields={'Text':'A {{c1::new}} B {{c2::two}} C {{c3::three}}'})
        cs = self.approve(p)
        rendered = render_preview(self.col, p, cs, 2)
        self.assertIn('three', rendered['answer'])
        self.assertEqual(len(n.card_ids()), 2)
        apply_review(self.col, self.store, p['key'])
        self.assertTrue(set(old_ids).issubset(n.card_ids()))
        self.assertEqual(len(n.card_ids()), 3)
        self.col.undo()
        self.assertEqual(n.card_ids(), old_ids)

    def test_export_and_store_reopen(self):
        exported = export_snapshot(self.col, self.store, '')
        self.assertEqual(len(exported['notes']), 1)
        self.assertEqual(exported['notes'][0]['fingerprint'], fingerprint(exported['notes'][0]))
        self.assertEqual(Store(self.store.path).identity, self.store.identity)

    def test_history_restore_after_anki_restart(self):
        p = self.proposal()
        self.approve(p)
        apply_review(self.col, self.store, p['key'])
        nid = self.n.id
        self.col.close()
        self.col = Collection(str(self.root/'collection.anki2'))
        self.store = Store(self.store.path)
        restore = restoration_proposal(self.col, self.store, self.store.history()[0])
        self.store.import_proposals([restore])
        self.approve(restore)
        apply_review(self.col, self.store, restore['key'])
        self.assertEqual(dict(self.col.get_note(nid).items()), p['base']['fields'])

    def test_failure_rolls_back_collection_and_is_recorded(self):
        from unittest.mock import patch
        p = self.proposal()
        self.approve(p)
        original = snapshot(self.col, self.n.id, self.store.identity)
        finish = self.store.finish
        count = [0]
        def flaky(*args, **kwargs):
            count[0] += 1
            if count[0] == 1:
                raise OSError('simulated history disk error')
            return finish(*args, **kwargs)
        with patch.object(self.store, 'finish', side_effect=flaky):
            with self.assertRaises(OSError):
                apply_review(self.col, self.store, p['key'])
        self.assertEqual(snapshot(self.col, self.n.id, self.store.identity), original)
        self.assertEqual(self.store.history()[0]['state'], 'failed')

    def test_interrupted_add_journal_recovers_identity_without_retry(self):
        p = self.proposal('add')
        note = self.col.new_note(self.model)
        note['Front'] = p['fields']['Front']
        note['Back'] = p['fields']['Back']
        self.col.add_note(note, 1)
        planned = snapshot(self.col, note.id, self.store.identity)
        planned['note_id'] = 0
        planned['fingerprint'] = fingerprint(planned)
        self.store.prepare(p['key'], None, planned, [], '模拟写入后中断')
        reconcile(self.col, self.store)
        self.assertEqual(self.store.history()[0]['state'], 'applied')
        self.assertEqual(json.loads(self.store.history()[0]['after_json'])['note_id'], note.id)
        with self.assertRaises(ContentError):
            apply_review(self.col, self.store, p['key'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
