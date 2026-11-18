"""The only collection-writing boundary. All writes use public undoable APIs."""
from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, quote
from html.parser import HTMLParser
from anki.notes import NoteFieldsCheckResult
from anki.errors import NotFoundError
from anki.notetypes_pb2 import StockNotetype

from .core import (ContentError, DEFAULT_NOTETYPE, TRASH_TAG, change_dicts, digest,
                   fingerprint, make_changes, materialize, unresolved_changes,
                   validate_content)


def model_hash(model):
    return digest({k: model.get(k) for k in ("id", "type", "flds", "tmpls", "css", "latexPre", "latexPost")})


def snapshot(col, nid, profile_id):
    note = col.get_note(nid)
    result = dict(note_id=int(note.id), guid=note.guid, mid=int(note.mid),
                  fields=dict(note.items()), tags=sorted(note.tags),
                  model_hash=model_hash(note.note_type()), profile_id=profile_id)
    result["fingerprint"] = fingerprint(result)
    return result


def check_base(col, proposal, store):
    base = proposal["base"]
    if base["profile_id"] != store.identity:
        raise ContentError("此提案来自另一 Profile，已阻止写入。请从当前 Profile 重新导出。")
    try:
        current = snapshot(col, base["note_id"], store.identity)
    except Exception as exc:
        raise ContentError("原笔记已不存在，已阻止写入。") from exc
    if current["fingerprint"] != base["fingerprint"]:
        raise ContentError("版本冲突：原文字段、标签或模板已变化。请重新导出后让 AI 重新提案；不会覆盖现有内容。")
    return current


def media_check(col, fields):
    # Files are referenced only; this version never writes or replaces media.
    for value in fields.values():
        refs = re.findall(r"\[sound:([^\]]+)\]", value)
        class MediaParser(HTMLParser):
            def handle_starttag(self, tag, attrs):
                if tag in ("img", "audio", "source", "video"):
                    refs.extend(v for k,v in attrs if k in ("src", "poster") and v)
        MediaParser().feed(value)
        for ref in refs:
            name = unquote(ref)
            if any(c in name for c in ("/", "\\", ":", "\x00")) or name in (".", ".."):
                raise ContentError(f"媒体引用必须是集合媒体文件名：{name}")
            if not (Path(col.media.dir()) / name).is_file():
                raise ContentError(f"集合中缺少媒体：{name}。请先用 Anki 添加媒体，再重新审批。")


def load_review(row):
    proposal = json.loads(row["payload"])
    if row.get("note_type_edit"):
        proposal.update(json.loads(row["note_type_edit"]))
    changes = make_changes(proposal, row["level"])
    decisions = {c["id"]: c["decision"] for c in json.loads(row["decisions"])}
    for c in changes:
        c.decision = decisions.get(c.id, "pending")
    return proposal, changes


def manual_fields(row):
    return json.loads(row.get("manual_fields", "{}"))


def supports_note_type_picker(model):
    return model.get("originalStockKind") != StockNotetype.OriginalStockKind.ORIGINAL_STOCK_KIND_IMAGE_OCCLUSION


def field_mapping(source_fields, target_fields):
    aliases = [
        {"front", "question", "text", "问题", "题目", "正面"},
        {"back", "answer", "back extra", "答案", "背面", "附加"},
        {"reference", "references", "source", "引用", "来源"},
    ]
    remaining = dict.fromkeys(source_fields)
    mapping = {}
    for target in target_fields:
        for source in remaining:
            if target.casefold() == source.casefold():
                mapping[target] = source
                break
        if target in mapping:
            del remaining[mapping[target]]
    for target in target_fields:
        if target in mapping:
            continue
        group = next((group for group in aliases if target.casefold() in group), ())
        matching = [source for source in remaining if source.casefold() in group]
        if len(matching) == 1:
            mapping[target] = matching[0]
            del remaining[matching[0]]
    return mapping


def change_add_notetype(col, store, row, target_name, mapping):
    proposal, previous_changes = load_review(row)
    if proposal["action"] != "add" or row["status"] != "pending":
        raise ContentError("只能切换待审新增提案的笔记类型。")
    target = col.models.by_name(target_name)
    if not target or not supports_note_type_picker(target):
        raise ContentError("目标笔记类型不存在或不支持在审批中切换。")
    targets = [field["name"] for field in target["flds"]]
    sources = proposal["fields"]
    if not set(mapping).issubset(targets) or not set(mapping.values()).issubset(sources):
        raise ContentError("字段设置已变化，请重新选择字段对应关系。")
    if len(set(mapping.values())) != len(mapping):
        raise ContentError("每个原字段只能对应一个目标字段。")

    note_format = {
        "notetype": target["name"],
        "fields": {name: sources[mapping[name]] if name in mapping else "" for name in targets},
    }
    existing_edits = manual_fields(row)
    edits = {name: existing_edits[source] for name, source in mapping.items()
             if source in existing_edits}
    previous = {(c.field, c.start, c.end, c.old, c.new): c.decision for c in previous_changes}
    changes = make_changes({**proposal, **note_format}, row["level"])
    for change in changes:
        original = mapping.get(change.field)
        change.decision = previous.get(
            (original, change.start, change.end, change.old, change.new), "pending"
        )
    store.save_note_type(
        row["key"], note_format, change_dicts(changes), edits,
        {"from": proposal["notetype"], "to": target["name"], "mapping": mapping},
    )


def proposal_decks(col, proposal):
    if proposal["action"] == "add":
        return [proposal["deck"]]
    return list(dict.fromkeys(
        col.decks.name(card.current_deck_id())
        for card in col.get_note(proposal["base"]["note_id"]).cards()
    ))


def draft_note(col, proposal, fields):
    if proposal["action"] == "add":
        model = col.models.by_name(proposal["notetype"])
    else:
        model = col.models.get(proposal["base"]["mid"])
    if not model:
        raise ContentError("笔记类型不存在。")
    note = col.new_note(model)
    if not set(fields).issubset(note.keys()):
        raise ContentError("字段名与当前笔记类型不一致。")
    for field, value in fields.items():
        note[field] = value
    return note


@dataclass
class Applied:
    changes: object
    history_id: str
    note_id: int


def apply_review(col, store, key):
    # Reload durable decisions, never trust an AI-supplied 'approved' flag.
    row = next((r for r in store.proposals() if r["key"] == key), None)
    if not row or row["status"] != "pending":
        raise ContentError("提案已处理或不可写入。")
    p, changes = load_review(row)
    edits = manual_fields(row)
    if unresolved_changes(changes, edits):
        raise ContentError("请先接受或拒绝每一处修改。")
    if not edits and not any(c.decision == "accepted" for c in changes):
        raise ContentError("没有已接受的修改。可将整条提案标记为拒绝。")
    action = p["action"]
    before = None if action == "add" else check_base(col, p, store)
    if action == "add":
        model = col.models.by_name(p["notetype"])
        deck = col.decks.by_name(p["deck"])
        if not model or not deck:
            raise ContentError("笔记类型或牌组不存在；插件不会自动创建它们。")
        if deck.get("dyn"):
            raise ContentError("新增笔记需要普通牌组，不能加入筛选牌组。")
        if set(p["fields"]) != {f["name"] for f in model["flds"]}:
            raise ContentError("新增必须包含该笔记类型的全部字段。")
        note = col.new_note(model)
    else:
        note = col.get_note(before["note_id"])
        model = note.note_type()
    fields = materialize(p, changes, edits)
    if action in ("add", "update"):
        if not set(fields).issubset(set(note.keys())):
            raise ContentError("字段名与当前笔记类型不一致。")
        changed_fields = {name: value for name, value in fields.items()
                          if before is None or before["fields"][name] != value}
        for value in changed_fields.values():
            validate_content(value)
        media_check(col, changed_fields)
        for name, value in fields.items():
            note[name] = value
        if note.fields_check() == NoteFieldsCheckResult.EMPTY:
            raise ContentError("首字段不能为空，请接受或手动填写首字段。")
        if model["type"] == 1 and not note.cloze_numbers_in_fields():
            raise ContentError("Cloze 笔记至少需要一个有效的挖空。")
    cards_to_suspend = []
    cards_to_restore = []
    if action == "delete":
        if TRASH_TAG in note.tags:
            raise ContentError("笔记已在回收站。")
        # Buried/filtered states need special handling; fail before writing.
        cards = note.cards()
        if any(c.odid or c.queue < -1 for c in cards):
            raise ContentError("请先将卡片移出筛选牌组并取消搁置，再移入回收站。")
        cards_to_suspend = [c.id for c in cards if c.queue >= 0]
        before["suspended_by_review"] = [int(cid) for cid in cards_to_suspend]
        note.add_tag(TRASH_TAG)
    if action == "restore_deleted":
        if TRASH_TAG not in note.tags:
            raise ContentError("笔记已不在回收站。")
        cards_to_restore = p.get("restore_cards", [])
        available = {c.id: c for c in note.cards()}
        if any(cid not in available or available[cid].queue != -1 for cid in cards_to_restore):
            raise ContentError("回收站卡片状态已变化；请人工检查，避免覆盖调度状态。")
        note.remove_tag(TRASH_TAG)
    planned = dict(note_id=int(note.id), guid=note.guid, mid=int(note.mid), fields=dict(note.items()),
                   tags=sorted(note.tags), model_hash=model_hash(model), profile_id=store.identity)
    planned["fingerprint"] = fingerprint(planned)
    approved = change_dicts(changes)
    for item in approved:
        if item["field"] in edits:
            item["decision"] = "overridden"
    ai_result = materialize(p, changes)
    for field, value in edits.items():
        approved.append(dict(field=field, old=ai_result[field], new=value,
                             decision="accepted", source="manual"))
    hid, undo_name = store.prepare(key, before, planned, approved, p["reason"])
    marker = None
    try:
        marker = col.add_custom_undo_entry(undo_name)
        if action == "add":
            col.add_note(note, deck["id"])
            # Anki templates can override a deck during generation. Place only
            # these new cards in the displayed target as part of the same undo.
            elsewhere = [card.id for card in note.cards() if card.did != deck["id"]]
            if elsewhere:
                col.set_deck(elsewhere, deck["id"])
        else:
            col.update_note(note)
        if cards_to_suspend:
            col.sched.suspend_cards(cards_to_suspend)
        if cards_to_restore:
            col.sched.unsuspend_cards(cards_to_restore)
        result = col.merge_undo_entries(marker)
        after = snapshot(col, note.id, store.identity)
        store.finish(hid, after)
        return Applied(result, hid, int(note.id))
    except Exception:
        if marker is not None:
            # Operations are serialized by CollectionOp. Roll back only our own entry.
            col.merge_undo_entries(marker)
            if col.undo_status().undo == undo_name:
                col.undo()
        store.finish(hid, planned, "failed")
        raise


def reconcile(col, store):
    """Track native undo/redo and recover a journal interrupted between DB commits."""
    for h in store.history():
        if h["state"] in ("failed", "superseded"):
            continue
        before, after = json.loads(h["before_json"]), json.loads(h["after_json"])
        nid = after.get("note_id", 0)
        if not nid:
            nid = col.db.scalar("SELECT id FROM notes WHERE guid=?", after["guid"])
        try:
            current = snapshot(col, nid, store.identity) if nid else None
        except Exception:
            current = None
        if current and current["fingerprint"] == after["fingerprint"]:
            state = "applied"
        elif before and current and current["fingerprint"] == before["fingerprint"] or before is None and current is None:
            state = "undone" if h["state"] != "prepared" else "interrupted"
        elif h["state"] == "prepared" and current and current["guid"] == after["guid"] and current["fields"] == after["fields"] and current["tags"] == after["tags"]:
            after = current
            state = "applied"
        else:
            state = "diverged"
        if state != h["state"]:
            store.finish(h["id"], after, state)
            # Never automatically retry a write after crash or native undo.
            store.status(h["proposal_key"], "applied" if state == "applied" else state)


def restoration_proposal(col, store, h):
    before, after = json.loads(h["before_json"]), json.loads(h["after_json"])
    current = snapshot(col, after["note_id"], store.identity)
    if current["fingerprint"] != after["fingerprint"]:
        raise ContentError("历史之后笔记已有其他改动，不能直接恢复。请重新导出并提交新的合并提案。")
    if before is None:
        p = dict(action="delete", base=current)
    elif TRASH_TAG not in before["tags"] and TRASH_TAG in after["tags"]:
        p = dict(action="restore_deleted", base=current, restore_cards=before.get("suspended_by_review", []))
    elif TRASH_TAG in before["tags"] and TRASH_TAG not in after["tags"]:
        p = dict(action="delete", base=current)
    else:
        p = dict(action="update", base=current, fields=before["fields"])
    p.update(id="restore-" + h["id"], reason="恢复历史版本：" + h["time"])
    p["key"] = digest(p)
    return p


def note_snapshots(col, store, *, search=None, note_ids=None):
    ids = col.find_notes(search) if note_ids is None else list(dict.fromkeys(note_ids))
    if len(ids) > 500:
        raise ContentError("一次最多导出 500 条笔记，请缩小搜索范围。")
    notes = []
    for nid in ids:
        try:
            notes.append(snapshot(col, nid, store.identity))
        except NotFoundError as error:
            raise ContentError(f"当前 Profile 中找不到笔记 {nid}，请核对后重试。") from error
    return {"schema": "anki-ai-review/1", "profile_id": store.identity, "notes": notes}


def export_snapshot(col, store, search):
    return {**note_snapshots(col, store, search=search),
            "default_notetype": DEFAULT_NOTETYPE,
            "instructions": "保留 base 不变；修改 fields 后生成 proposals。id 使用唯一字符串，action 为 update/add/delete。",
            "notetypes": [{"name": m["name"], "fields": [f["name"] for f in m["flds"]]} for m in col.models.all()],
            "decks": [d.name for d in col.decks.all_names_and_ids()]}


def render_preview(col, proposal, changes, ordinal=0, edits=None):
    fields = materialize(proposal, changes, edits)
    note = draft_note(col, proposal, fields)
    model = note.note_type()
    if model["type"] == 1:
        ordinals = [n - 1 for n in note.cloze_numbers_in_fields()] or [0]
    else:
        ordinals = list(range(len(model["tmpls"])))
    ordinal = ordinal if ordinal in ordinals else ordinals[0]
    card = note.ephemeral_card(ordinal, fill_empty=False)
    av = {"q":card.question_av_tags(), "a":card.answer_av_tags()}
    def audio_html(text):
        def replace(match):
            side, index = match.group(1), int(match.group(2))
            tags = av[side]
            if index < len(tags) and hasattr(tags[index], "filename"):
                return '<audio controls preload="metadata" src="' + quote(tags[index].filename, safe="") + '"></audio>'
            return '<span>语音合成预览请在 Anki 正常复习时播放。</span>'
        return re.sub(r"\[anki:play:([qa]):(\d+)\]", replace, text)
    return dict(fields=fields, question=audio_html(card.question()), answer=audio_html(card.answer()),
                ordinals=ordinals, ordinal=ordinal, notetype=model["name"])
