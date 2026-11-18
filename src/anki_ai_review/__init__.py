"""Anki AI Review: explicit content decisions, then native undoable writes."""
try:
    from aqt import mw, gui_hooks
    from aqt.qt import QAction
except ImportError:
    mw = None

if mw is not None:
    from .snapshot_service import start, stop
    from .ui import open_center, profile_closing
    action = QAction("Anki AI Review · 内容审批中心", mw)
    action.setShortcut("Ctrl+Alt+R")
    action.triggered.connect(open_center)
    mw.form.menuTools.addAction(action)
    gui_hooks.profile_did_open.append(start)
    gui_hooks.profile_will_close.append(stop)
    gui_hooks.profile_will_close.append(profile_closing)
