"""Access the active local Anki AI Review queue without opening a window."""

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path
from urllib.error import URLError
from urllib.request import ProxyHandler, Request, build_opener


MAX_JSON_BYTES = 10_000_000


def anki_connect(action, **params):
    # This module only calls read-only AnkiConnect actions.
    port = int(os.environ.get("ANKI_AI_REVIEW_CONNECT_PORT", "8765"))
    if not 1 <= port <= 65535:
        raise ValueError("AnkiConnect 端口无效。")
    request = Request(
        f"http://127.0.0.1:{port}",
        data=json.dumps({"action": action, "version": 6, "params": params}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        with build_opener(ProxyHandler({})).open(request, timeout=5) as response:
            result = json.load(response)
    except (URLError, TimeoutError) as error:
        raise ValueError("无法连接本机 AnkiConnect，请确认 Anki 已打开并启用了 AnkiConnect。") from error
    if result.get("error"):
        raise ValueError(f"AnkiConnect 查询失败：{result['error']}")
    return result.get("result")


def active_profile():
    profile = anki_connect("getActiveProfile")
    if not isinstance(profile, str) or not profile:
        raise ValueError("AnkiConnect 未返回当前 Profile。请确认 Anki 已打开一个 Profile。")
    if Path(profile).name != profile or profile in (".", ".."):
        raise ValueError("AnkiConnect 返回的 Profile 名称无效。")
    return profile


def active_installation(expected_profile=None):
    app_base = (Path(os.environ["APPDATA"]) / "Anki2").resolve()
    profile = active_profile()
    if expected_profile and profile != expected_profile:
        raise ValueError(f"当前 Profile 是 {profile}，预期是 {expected_profile}。")
    profile_dir = (app_base / profile).resolve()
    if not profile_dir.is_relative_to(app_base) or not (profile_dir / "collection.anki2").is_file():
        raise ValueError(f"在 Anki 默认数据目录找不到当前 Profile：{profile}")

    addons = app_base / "addons21"
    addon = addons / "anki_ai_review"
    if not (addon / "manifest.json").is_file():
        raise ValueError("在 Anki 默认数据目录找不到已安装的 Anki AI Review。")
    if str(addons) not in sys.path:
        sys.path.insert(0, str(addons))
    return profile, profile_dir, addon


def review_context(deck_query="", note_type=None):
    profile, _, addon = active_installation()
    from anki_ai_review.core import DEFAULT_NOTETYPE, SCHEMA

    note_type = note_type or DEFAULT_NOTETYPE
    names = anki_connect("modelNames")
    installed = note_type in names
    fields = anki_connect("modelFieldNames", modelName=note_type) if installed else []
    decks = sorted(
        (deck for deck in anki_connect("deckNames") if deck_query.casefold() in deck.casefold()),
        key=str.casefold,
    )
    if active_profile() != profile:
        raise ValueError("活动 Profile 已切换，请重新查询。")
    manifest = json.loads((addon / "manifest.json").read_text(encoding="utf-8-sig"))
    return {
        "profile": profile,
        "review_version": manifest.get("human_version"),
        "schema": SCHEMA,
        "default_notetype": DEFAULT_NOTETYPE,
        "notetype": {"name": note_type, "available": installed, "fields": fields},
        "matching_decks": decks[:30],
        "matching_deck_count": len(decks),
        "fill_in_convention": "XXHK - 问答使用 <u>待回忆内容</u> 挖空。",
    }


def get_note_snapshots(request, expected_profile):
    profile, profile_dir, _ = active_installation(expected_profile)
    try:
        from anki_ai_review.snapshot_ipc import fetch_snapshots
    except ModuleNotFoundError as error:
        if error.name != "anki_ai_review.snapshot_ipc":
            raise
        raise ValueError("后台快照需要 Anki AI Review 0.4.0 或更新版本。升级后请重启 Anki。") from error
    result = fetch_snapshots(profile_dir, request)
    if result.get("profile") != profile or active_profile() != profile:
        raise ValueError("活动 Profile 已切换，请重新获取原文快照。")
    return result


def submit_document(document, expected_profile):
    profile, profile_dir, _ = active_installation(expected_profile)
    from anki_ai_review.core import read_document
    from anki_ai_review.store import Store

    proposals = read_document(document)
    if active_profile() != profile:
        raise ValueError("活动 Profile 已切换，请重新提交。")

    queue = Store(profile_dir / "anki_ai_review" / "history.sqlite3")
    for proposal in proposals:
        baseline = proposal.get("base")
        if baseline and baseline["profile_id"] != queue.identity:
            raise ValueError(f"提案 {proposal['id']} 的原文来自其他 Profile。")
    count = queue.import_proposals(proposals)

    keys = [proposal["key"] for proposal in proposals]
    placeholders = ",".join("?" for key in keys)
    with queue.db() as db:
        rows = db.execute(
            f"SELECT key, status FROM proposals WHERE key IN ({placeholders})", keys
        )
        statuses = {row["key"]: row["status"] for row in rows}
    return {
        "profile": profile,
        "imported": count,
        "already_present": len(proposals) - count,
        "proposals": [
            {"id": p["id"], "status": statuses[p["key"]]} for p in proposals
        ],
    }


def read_json(stream):
    raw = stream.read(MAX_JSON_BYTES + 1)
    if len(raw) > MAX_JSON_BYTES:
        raise ValueError("JSON 数据不能超过 10MB。")
    return json.loads(raw.decode("utf-8-sig"))


def submit(path, expected_profile):
    with path.open("rb") as stream:
        document = read_json(stream)
    return submit_document(document, expected_profile)


def request_from_stdio():
    request = read_json(sys.stdin.buffer)
    operation = request.get("operation")
    if operation == "get_review_context":
        return review_context(request.get("deck_query", ""), request.get("note_type"))
    if operation in ("get_note_snapshots", "submit_proposals"):
        expected = request.get("expected_profile")
        if not isinstance(expected, str) or not expected:
            raise ValueError("请提供明确的 expected_profile。")
        if operation == "get_note_snapshots":
            return get_note_snapshots(request, expected)
        return submit_document(request["document"], expected)
    raise ValueError("未知的审批队列操作。")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("proposal_json", type=Path, nargs="?")
    parser.add_argument("--expect-profile", help="Fail if a different Profile is active.")
    parser.add_argument("--stdio-request", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if not args.stdio_request and args.proposal_json is None:
        parser.error("请提供提案 JSON 文件。")
    try:
        if args.stdio_request:
            result = request_from_stdio()
        else:
            result = submit(args.proposal_json, args.expect_profile)
    except (OSError, ValueError, sqlite3.Error) as error:
        parser.exit(1, f"Anki AI Review 请求失败：{error}\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
