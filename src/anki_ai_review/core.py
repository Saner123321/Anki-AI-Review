"""Content proposal/diff engine. No Anki or Qt dependency."""
from __future__ import annotations

import copy
import difflib
import hashlib
import json
import re
from dataclasses import asdict, dataclass
from html.parser import HTMLParser

SCHEMA = "anki-ai-review/1"
DEFAULT_NOTETYPE = "XXHK - 问答"
DEFAULT_NOTETYPE_FIELDS = ("问题", "答案", "引用")
TRASH_TAG = "AnkiAIReview::Trash"


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def fingerprint(snapshot):
    return digest({k: snapshot[k] for k in ("note_id", "guid", "mid", "fields", "tags", "model_hash", "profile_id")})


class ContentError(ValueError):
    pass


class Structure(HTMLParser):
    VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
    BLOCKED = {"script", "iframe", "object", "embed", "base", "meta", "link", "form", "input", "button", "textarea"}

    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.stack = []
        self.tags = []

    def handle_starttag(self, tag, attrs):
        self.tags.append(self.get_starttag_text())
        if tag in self.BLOCKED:
            raise ContentError(f"不允许主动内容 <{tag}>；请提交静态 HTML。")
        for name, value in attrs:
            if name.lower().startswith("on") or name == "srcdoc":
                raise ContentError("不允许 HTML 事件或 srcdoc。")
            if name in ("src", "href", "poster", "xlink:href") and value:
                value = value.strip()
                if re.match(r"(?i)(?:[a-z][a-z0-9+.-]*:|//)", value):
                    raise ContentError("HTML 仅支持集合中的本地媒体和相对链接。")
            if name == "style" and re.search(r"(?i)url\s*\(|@import|expression\s*\(", value or ""):
                raise ContentError("不允许样式中的外部资源。")
        if tag not in self.VOID:
            self.stack.append(tag)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self.VOID:
            self.stack.pop()

    def handle_endtag(self, tag):
        self.tags.append(f"</{tag}>")
        if tag in self.VOID:
            return
        if not self.stack or self.stack.pop() != tag:
            raise ContentError("HTML 标签不配对；请先修正字段 HTML。")


def validate_content(text):
    if not isinstance(text, str) or len(text) > 200_000:
        raise ContentError("字段必须是长度不超过 200000 的字符串。")
    if "\x00" in text or "\x1f" in text:
        raise ContentError("字段含 Anki 保留分隔符。")
    parser = Structure()
    parser.feed(text)
    parser.close()
    if parser.stack:
        raise ContentError("HTML 标签未闭合。")
    if re.search(r"(?i)@import|url\s*\(\s*['\"]?(?:https?:|//|file:)", text):
        raise ContentError("不允许外部样式资源。")
    # Balanced nested Cloze is kept as one indivisible review atom.
    depth = 0
    for token in re.findall(r"\{\{c\d+::|\{\{|\}\}", text):
        depth += -1 if token == "}}" else 1
        if depth < 0:
            raise ContentError("Cloze 大括号不配对。")
    if depth:
        raise ContentError("Cloze 大括号未闭合。")
    for left, right in ((r"\(", r"\)"), (r"\[", r"\]"), ("[latex]", "[/latex]"), ("[$]", "[/$]"), ("[$$]", "[/$$]")):
        if text.count(left) != text.count(right):
            raise ContentError("LaTeX 定界符不配对。")
    return parser.tags


ATOM = re.compile(r"\[sound:[^\]\r\n]+\]|\[latex\][\s\S]*?\[/latex\]|\[\$\$?\][\s\S]*?\[/\$\$?\]|\\\([\s\S]*?\\\)|\\\[[\s\S]*?\\\]|<[^>]+>|&(?:#\d+|#x[0-9a-fA-F]+|\w+);", re.I)


def cloze_end(text, start):
    depth = 0
    for match in re.finditer(r"\{\{|\}\}", text[start:]):
        depth += 1 if match.group() == "{{" else -1
        if depth == 0:
            return start + match.end()
    return len(text)


def tokens(text, level):
    result = []
    i = 0
    while i < len(text):
        if text.startswith("{{", i):
            end = cloze_end(text, i)
            result.append(text[i:end]); i = end; continue
        match = ATOM.match(text, i)
        if match:
            result.append(match.group()); i = match.end(); continue
        if level == "fine":
            match = re.match(r"[A-Za-z0-9_]+|\s+|[^\x00-\x7f]|.", text[i:], re.S)
        elif level == "normal":
            match = re.match(r"[^。！？.!?\n<\\{&\[]+[。！？.!?]?|.", text[i:], re.S)
        else:
            match = re.match(r"[^\n<\\{&\[]+\n?|.", text[i:], re.S)
        result.append(match.group()); i += len(match.group())
    assert "".join(result) == text
    return result


@dataclass
class Change:
    id: str
    field: str
    start: int
    end: int
    old: str
    new: str
    decision: str = "pending"


def field_diff(field, before, after, level="normal"):
    if before == after:
        return []
    # Structure changes are indivisible: never approve half a tag pair.
    before_tags = re.findall(r"<[^>]+>", before)
    after_tags = re.findall(r"<[^>]+>", after)
    if before_tags != after_tags:
        ranges = [(0, len(before), before, after)]
    else:
        a, b = tokens(before, level), tokens(after, level)
        offsets = [0]
        for token in a:
            offsets.append(offsets[-1] + len(token))
        ranges = [(offsets[i], offsets[j], "".join(a[i:j]), "".join(b[k:l]))
                  for op, i, j, k, l in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes() if op != "equal"]
    return [Change(digest([field, s, e, old, new])[:20], field, s, e, old, new) for s, e, old, new in ranges]


def make_changes(proposal, level="normal"):
    if proposal["action"] in ("delete", "restore_deleted"):
        return [Change("action", "@action", 0, 0, "保留笔记", "移入回收站（暂停卡片）" if proposal["action"] == "delete" else "恢复回收站笔记")]
    old = proposal.get("base", {}).get("fields", {})
    return [as_change for field, value in proposal["fields"].items()
            for as_change in field_diff(field, old.get(field, ""), value, level)]


def unresolved_changes(changes, manual_fields):
    return [change for change in changes
            if change.decision == "pending" and change.field not in manual_fields]


def materialize(proposal, changes, manual_fields=None, *, include_pending=False):
    result = dict(proposal.get("base", {}).get("fields", {}))
    if proposal["action"] == "add":
        result = {name: "" for name in proposal["fields"]}
    for field in proposal.get("fields", {}):
        text = result.get(field, "")
        for change in sorted((c for c in changes if c.field == field), key=lambda c: c.start, reverse=True):
            if change.decision == "accepted" or (include_pending and change.decision == "pending"):
                text = text[:change.start] + change.new + text[change.end:]
        result[field] = text
    result.update(manual_fields or {})
    return result


def change_dicts(changes):
    return [asdict(c) for c in changes]


def read_document(raw):
    if not isinstance(raw, dict) or raw.get("schema") != SCHEMA:
        raise ContentError(f"JSON schema 必须是 {SCHEMA}。")
    proposals = raw.get("proposals")
    if not isinstance(proposals, list) or not 1 <= len(proposals) <= 500:
        raise ContentError("每批须包含 1–500 条提案。")
    clean = []
    seen = set()
    for item in proposals:
        if not isinstance(item, dict):
            raise ContentError("每条提案必须是对象。")
        action = item.get("action")
        if action not in ("add", "update", "delete"):
            raise ContentError("action 只允许 add / update / delete。")
        pid = item.get("id")
        if not isinstance(pid, str) or not pid.strip() or len(pid) > 150 or pid in seen:
            raise ContentError("每条提案需要批内唯一的字符串 id。")
        seen.add(pid)
        p = {"id": pid, "action": action, "reason": str(item.get("reason", ""))[:4000]}
        if action in ("update", "delete"):
            base = item.get("base")
            if not isinstance(base, dict) or not all(k in base for k in ("note_id", "guid", "mid", "fields", "tags", "model_hash", "fingerprint", "profile_id")):
                raise ContentError("修改/删除必须携带插件导出的完整 base 快照。")
            if type(base["note_id"]) is not int or type(base["mid"]) is not int or not isinstance(base["fields"], dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in base["fields"].items()) or not isinstance(base["tags"], list) or not all(isinstance(t, str) for t in base["tags"]) or not all(isinstance(base[k], str) for k in ("guid", "model_hash", "profile_id", "fingerprint")):
                raise ContentError("base 数据格式错误。")
            if fingerprint(base) != base["fingerprint"]:
                raise ContentError("base 指纹不匹配；请保持导出的原文不变。")
            p["base"] = copy.deepcopy(base)
        if action in ("add", "update"):
            fields = item.get("fields")
            if not isinstance(fields, dict) or not fields or not all(isinstance(k, str) for k in fields):
                raise ContentError("fields 必须是非空字段对象。")
            for value in fields.values():
                validate_content(value)
            p["fields"] = fields.copy()
        if action == "add":
            note_type = item.get("notetype", DEFAULT_NOTETYPE)
            deck = item.get("deck")
            if not isinstance(note_type, str) or not note_type.strip():
                raise ContentError("notetype 必须是非空笔记类型名称。")
            if not isinstance(deck, str) or not deck.strip():
                raise ContentError("新增提案需要目标牌组 deck。")
            if note_type == "XXHK-问答":
                note_type = DEFAULT_NOTETYPE
            if note_type == DEFAULT_NOTETYPE:
                if not set(p["fields"]).issubset(DEFAULT_NOTETYPE_FIELDS):
                    raise ContentError("XXHK - 问答请使用「问题」「答案」「引用」字段。")
                p["fields"] = {field: p["fields"].get(field, "") for field in DEFAULT_NOTETYPE_FIELDS}
            p.update(notetype=note_type, deck=deck)
        p["key"] = digest(p)
        clean.append(p)
    return clean
