"""Read the active Anki collection on its normal background-operation queue."""

import json
import logging
from pathlib import Path

from PyQt6 import sip
from aqt import mw
from aqt.operations import QueryOp
from aqt.qt import QLocalSocket, QTimer

from .adapter import note_snapshots
from .core import ContentError
from .snapshot_ipc import MAX_REQUEST, MAX_RESPONSE, frame, local_listener, snapshot_request
from .store import Store


class SnapshotService:
    def __init__(self, profile_name, profile_dir, collection):
        self.profile_name = profile_name
        self.profile_dir = Path(profile_dir)
        self.collection = collection
        self.listener = local_listener(mw, profile_dir)
        self.listener.newConnection.connect(self.accept_connections)

    def accept_connections(self):
        while self.listener.hasPendingConnections():
            client = self.listener.nextPendingConnection()
            client.setReadBufferSize(MAX_REQUEST + 1)
            client.disconnected.connect(client.deleteLater)
            timeout = QTimer(client)
            timeout.setSingleShot(True)
            timeout.timeout.connect(client.abort)
            timeout.start(25_000)
            client.readyRead.connect(lambda client=client: self.read_request(client))
            self.read_request(client)

    @staticmethod
    def reply(client, envelope):
        if sip.isdeleted(client) or client.state() != QLocalSocket.LocalSocketState.ConnectedState:
            return
        data = frame(envelope)
        if len(data) > MAX_RESPONSE:
            data = frame({"error": "快照数据过大，请缩小范围或分批获取。"})
        client.write(data)
        client.disconnectFromServer()

    def read_request(self, client):
        if client.property("request_received"):
            return
        if client.bytesAvailable() > MAX_REQUEST:
            client.setProperty("request_received", True)
            self.reply(client, {"error": "请求过大，请缩小搜索范围。"})
            return
        if not client.canReadLine():
            return
        client.setProperty("request_received", True)
        try:
            profile, search, ids = snapshot_request(json.loads(bytes(client.readLine())))
            if profile != self.profile_name:
                raise ContentError("当前 Profile 已切换，请重新查询。")
        except ValueError as error:
            self.reply(client, {"error": str(error)})
            return

        def read(col):
            if col is not self.collection:
                raise ContentError("当前 Profile 已切换，请重新查询。")
            queue = Store(self.profile_dir / "anki_ai_review" / "history.sqlite3")
            result = note_snapshots(col, queue, search=search, note_ids=ids)
            return {"profile": self.profile_name, **result}

        QueryOp(
            parent=mw,
            op=read,
            success=lambda result: self.reply(client, {"result": result}),
        ).failure(lambda error: self.reply(client, {"error": str(error)})).run_in_background()

    def close(self):
        self.listener.close()
        self.listener.deleteLater()


_service = None


def stop():
    global _service
    if _service is not None:
        _service.close()
        _service = None


def start():
    global _service
    stop()
    if mw.col is not None and mw.pm.name:
        try:
            _service = SnapshotService(mw.pm.name, mw.pm.profileFolder(), mw.col)
        except OSError:
            logging.getLogger(__name__).exception("Could not start the Anki AI Review snapshot service")
