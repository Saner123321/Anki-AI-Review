"""One request per local socket, used only for reading note snapshots."""

import hashlib
import json
import os
from pathlib import Path
from time import monotonic

from aqt.qt import QCoreApplication, QLocalServer, QLocalSocket

from .core import ContentError

MAX_REQUEST = 32_768
MAX_RESPONSE = 10_000_000


def socket_name(profile_dir):
    path = os.path.normcase(str(Path(profile_dir).resolve()))
    return "anki-ai-review-snapshot-" + hashlib.sha256(path.encode()).hexdigest()[:32]


def frame(value):
    return (json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n").encode()


def snapshot_request(request):
    if not isinstance(request, dict) or request.get("operation") != "get_note_snapshots":
        raise ContentError("未知的快照请求。")
    profile = request.get("expected_profile")
    if not isinstance(profile, str) or not profile:
        raise ContentError("请指定当前 Profile。")
    search, ids = request.get("search"), request.get("note_ids")
    if (search is None) == (ids is None):
        raise ContentError("请提供 Anki 搜索式或笔记 ID 列表，二选一。")
    if search is not None and (
        not isinstance(search, str) or not search.strip() or len(search) > 4000
    ):
        raise ContentError("请提供有效的 Anki 搜索式。")
    if ids is not None and (
        not isinstance(ids, list) or not 1 <= len(ids) <= 500
        or any(type(nid) is not int or not 0 < nid < 2**53 for nid in ids)
    ):
        raise ContentError("请提供 1–500 个有效的笔记 ID。")
    return profile, search, ids


def fetch_snapshots(profile_dir, request):
    snapshot_request(request)
    app = QCoreApplication.instance()
    if app is None:
        app = QCoreApplication([])
    socket = QLocalSocket()
    socket.setReadBufferSize(MAX_RESPONSE + 1)
    socket.connectToServer(socket_name(profile_dir))
    if not socket.waitForConnected(1800):
        raise ContentError("后台快照服务未启动。请安装 Anki AI Review 0.4.0 或更新版本并重启 Anki。")
    try:
        request_data = frame(request)
        if len(request_data) > MAX_REQUEST:
            raise ContentError("请求过大，请缩小搜索范围。")
        if socket.write(request_data) != len(request_data):
            raise ContentError("无法发送快照请求。")
        socket.flush()
        deadline = monotonic() + 20
        received = bytearray()
        while monotonic() < deadline:
            received.extend(bytes(socket.readAll()))
            if len(received) > MAX_RESPONSE:
                raise ContentError("快照数据过大，请分批获取。")
            if received.endswith(b"\n"):
                response = json.loads(received)
                if "error" in response:
                    raise ContentError(response["error"])
                return response["result"]
            if socket.state() == QLocalSocket.LocalSocketState.UnconnectedState:
                break
            timeout = max(1, int((deadline - monotonic()) * 1000))
            socket.waitForReadyRead(timeout)
        raise ContentError("未收到完整的原文快照。请确认 Anki 仍打开在当前 Profile，再重试。")
    finally:
        socket.abort()


def local_listener(parent, profile_dir):
    server = QLocalServer(parent)
    server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
    if not server.listen(socket_name(profile_dir)):
        raise OSError(server.errorString())
    return server
