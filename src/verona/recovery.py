"""Crash recovery drafts; original design files are never written here."""
import fcntl
import os
from pathlib import Path
import re
import uuid

from PySide6.QtCore import QObject, QTimer
from PySide6.QtWidgets import QMessageBox

from .design_validation import validate_document
from .document_state import DocumentState
from .i18n import tr
from .input_safety import MAX_DESIGN_BYTES, read_design
from .workspace_storage import private_directory, write_private_json


_ID = re.compile(r"[0-9a-f]{32}")
AUTOSAVE_INTERVAL_MS = 30_000


def _lease(directory):
    handle = os.open(directory / "session.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BaseException:
        os.close(handle)
        raise
    return handle


class RecoveryStore:
    def __init__(self, directory=None):
        base = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state"))
        self.root = private_directory(directory or base / "verona/recovery")
        self.directory = self.root / uuid.uuid4().hex
        self.directory.mkdir(mode=0o700)
        self.handle = _lease(self.directory)
        self.pending = {}
        self.previous = {}

    def checkpoint(self, documents):
        keep = set()
        for index, document in enumerate(documents):
            if not document.is_dirty():
                continue
            name = document.recovery_id + ".json"
            keep.add(name)
            data = {"format": "verona", "version": 1, "units": "mm", **document.state(),
                    "_recovery": {"file": document.current_file, "index": index}}
            if data != self.previous.get(name):
                write_private_json(self.directory / name, data, max_bytes=MAX_DESIGN_BYTES)
                self.previous[name] = data
        for path in self.directory.glob("*.json"):
            if path.name not in keep:
                path.unlink()
                self.previous.pop(path.name, None)

    def candidates(self):
        drafts, errors = [], []
        for directory in sorted(self.root.iterdir(), key=lambda p: p.stat().st_mtime):
            if directory == self.directory or not _ID.fullmatch(directory.name) or not directory.is_dir():
                continue
            try:
                handle = _lease(directory)
            except BlockingIOError:
                continue  # A different desktop workspace still owns these drafts.
            except OSError:
                errors.append(directory)
                continue
            self.pending[directory] = handle
            session = []
            for path in directory.glob("*.json"):
                if not _ID.fullmatch(path.stem):
                    continue
                try:
                    data = read_design(path)
                    items, settings = validate_document(data)
                    info = data.get("_recovery")
                    if (not isinstance(info, dict) or type(info.get("index")) is not int
                            or info["index"] < 0 or not (info.get("file") is None
                                                        or isinstance(info["file"], str))):
                        raise ValueError("Invalid recovery information.")
                    document = DocumentState(settings=settings, items=items,
                                             metadata=data.get("metadata", {}), current_file=info.get("file"))
                    document.saved_snapshot = {}
                    session.append((info["index"], path, document))
                except (OSError, ValueError, TypeError, UnicodeError):
                    errors.append(path)
            drafts.extend((path, document) for _, path, document in sorted(session, key=lambda row: row[0]))
        return drafts, errors

    def release_pending(self, discard=()):
        try:
            for path in discard:
                path.unlink(missing_ok=True)
        finally:
            for directory, handle in self.pending.items():
                try:
                    if not any(directory.glob("*.json")):
                        (directory / "session.lock").unlink(missing_ok=True)
                        directory.rmdir()
                except OSError:
                    pass
                finally:
                    os.close(handle)
            self.pending.clear()

    def close(self, *, discard=True):
        if self.handle is None:
            return
        self.release_pending()
        try:
            if discard:
                for path in self.directory.glob("*.json"):
                    path.unlink()
                (self.directory / "session.lock").unlink(missing_ok=True)
                self.directory.rmdir()
        finally:
            handle, self.handle = self.handle, None
            os.close(handle)


class RecoveryController(QObject):
    def __init__(self, window, *, directory=None):
        super().__init__(window)
        self.window = window
        self.store = RecoveryStore(directory)
        self.timer = QTimer(self)
        self.timer.setInterval(AUTOSAVE_INTERVAL_MS)
        self.timer.timeout.connect(self.checkpoint)
        self.timer.start()

    def checkpoint(self):
        try:
            self.store.checkpoint(self.window._documents)
            return True
        except (OSError, ValueError, TypeError) as error:
            self.window.status_label.setText(tr("無法儲存復原草稿：{reason}", reason=str(error)))
            return False

    def _choice(self, count):
        dialog = QMessageBox(self.window)
        dialog.setWindowTitle(tr("復原未儲存的設計"))
        dialog.setText(tr("找到 {count} 份未儲存的草稿。要復原嗎？", count=count))
        restore = dialog.addButton(tr("復原設計"), QMessageBox.ButtonRole.AcceptRole)
        discard = dialog.addButton(tr("捨棄草稿"), QMessageBox.ButtonRole.DestructiveRole)
        dialog.addButton(tr("稍後再說"), QMessageBox.ButtonRole.RejectRole)
        dialog.setDefaultButton(restore)
        dialog.exec()
        clicked = dialog.clickedButton()
        choice = "restore" if clicked is restore else "discard" if clicked is discard else "later"
        dialog.deleteLater()
        return choice

    def restore_available(self):
        try:
            drafts, errors = self.store.candidates()
        except OSError as error:
            self.window.status_label.setText(tr("無法啟動自動復原：{reason}", reason=str(error)))
            return
        try:
            choice = self._choice(len(drafts)) if drafts else "later"
            if choice == "restore":
                for _, document in drafts:
                    if self.window.items or self.window.current_file or self.window.is_dirty():
                        self.window.new_design()
                    self.window._install_document(document.current_file, document.items,
                                                  document.settings, document.metadata)
                    self.window.saved_snapshot = {}
                    self.window._refresh_tab_titles()
                # Transfer protection before removing the previous session's copies.
                if self.checkpoint():
                    self.store.release_pending(path for path, _ in drafts)
            elif choice == "discard":
                self.store.release_pending(path for path, _ in drafts)
        except OSError as error:
            self.window.status_label.setText(tr("無法儲存復原草稿：{reason}", reason=str(error)))
        finally:
            self.store.release_pending()
        if errors:
            QMessageBox.warning(self.window, tr("復原草稿"), tr("部分草稿無法讀取，已保留原檔。"))

    def close(self):
        self.timer.stop()
        try:
            self.store.close()
        except OSError:
            pass  # Leaving a draft behind is safer than disrupting normal closing.
