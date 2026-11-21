"""Real ZIP fixtures and offscreen Qt pool tests; no desktop or input hooks."""
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
import zipfile

os.environ["QT_QPA_PLATFORM"] = "offscreen"
profile = tempfile.TemporaryDirectory(prefix="flickdock-zip-profile-")
os.environ["APPDATA"] = profile.name

from PySide6.QtCore import QCoreApplication, QEventLoop, QTimer
import selection_archive as module
from selection_archive import ArchiveCancelled, ZipArchiveJob, create_archive


app = QCoreApplication([])


def zip_contents(path):
    with zipfile.ZipFile(path) as archive:
        assert archive.testzip() is None
        names = archive.namelist()
        assert len(names) == len(set(names)), names
        return {name: archive.read(name) for name in names}


class CountedCancel:
    """Deterministic cancellation after traversal, during actual block writes."""
    def __init__(self, cancel_at):
        self.calls = 0
        self.cancel_at = cancel_at

    def is_set(self):
        self.calls += 1
        return self.calls >= self.cancel_at


def expect_raises(kind, callback):
    try:
        callback()
    except kind:
        return
    raise AssertionError("Expected " + kind.__name__)


def run_job(job, action=None):
    loop = QEventLoop()
    outcomes, thread_ids, beats = [], [], []
    def terminal(kind, value=""):
        outcomes.append((kind, value))
        thread_ids.append(threading.get_ident())
        loop.quit()
    job.finished.connect(lambda value: terminal("finished", value))
    job.failed.connect(lambda value: terminal("failed", value))
    job.cancelled.connect(lambda: terminal("cancelled"))
    heartbeat = QTimer()
    heartbeat.setInterval(5)
    heartbeat.timeout.connect(lambda: beats.append(time.monotonic()))
    heartbeat.start()
    deadline = QTimer()
    deadline.setSingleShot(True)
    deadline.timeout.connect(loop.quit)
    deadline.start(15000)
    assert job.start() is True and job.start() is False
    if action:
        QTimer.singleShot(0, action)
    loop.exec()
    heartbeat.stop()
    deadline.stop()
    assert job.wait(1000), "worker did not clean up"
    app.processEvents()
    assert len(outcomes) == 1, outcomes
    assert thread_ids == [threading.get_ident()], thread_ids
    assert not job.is_running
    return outcomes[0], beats


try:
    with tempfile.TemporaryDirectory(prefix="flickdock-zip-tests-") as temporary:
        root = Path(temporary)
        left, right, out = root / "left", root / "right", root / "out"
        for folder in (left, right, out):
            folder.mkdir()
        first = left / "计划.txt"
        second = right / "计划.txt"
        first.write_bytes("第一个文件\r\n内容完整。".encode("utf-8"))
        second.write_bytes(b"second\x00payload\xff")
        tree = left / "资料"
        (tree / "空文件夹").mkdir(parents=True)
        nested = tree / "嵌套"
        nested.mkdir()
        child = nested / "孩子.dat"
        child.write_bytes(bytes(range(256)) * 4)
        other_tree = right / "资料"
        other_tree.mkdir()
        (other_tree / "next.md").write_text("next", encoding="utf-8")

        exported = create_archive([first, second, child, tree, other_tree, tree], out / "选择.zip",
                                  texts=[("计划.txt", "原生文字🙂\n第二行"), ("../../CON", "safe")])
        contents = zip_contents(exported)
        assert contents["计划.txt"] == first.read_bytes()
        assert contents["计划 (2).txt"] == second.read_bytes()
        assert contents["计划 (3).txt"].decode("utf-8") == "原生文字🙂\n第二行"
        assert contents["资料/空文件夹/"] == b""
        assert contents["资料/嵌套/孩子.dat"] == child.read_bytes()
        assert "孩子.dat" not in contents and contents["资料 (2)/next.md"] == b"next"
        assert all(not name.startswith("/") and "../" not in name and "\\" not in name for name in contents)
        assert not list(root.rglob("*原生*.txt"))
        assert len(list(out.iterdir())) == 1
        print("PASS Unicode bytes, same-name roots, empty folders, parent/child dedup and native text entries")

        existing = out / "existing.zip"
        existing.write_bytes(b"must never change")
        (out / "existing (2).zip").write_bytes(b"also preserved")
        unique = create_archive([first], existing)
        assert unique.name == "existing (3).zip"
        assert existing.read_bytes() == b"must never change"
        assert (out / "existing (2).zip").read_bytes() == b"also preserved"
        assert zip_contents(unique)[first.name] == first.read_bytes()
        normalized = create_archive([], out / "text only", texts=[("CON", "内容")])
        assert normalized.name == "text only.zip" and zip_contents(normalized) == {"_CON.txt": "内容".encode("utf-8")}
        with ThreadPoolExecutor(max_workers=2) as pool:
            racing = [pool.submit(create_archive, [first], out / "racing.zip") for _ in range(2)]
            published = [future.result() for future in racing]
        assert len(set(published)) == 2
        assert {path.name for path in published} == {"racing.zip", "racing (2).zip"}
        assert all(zip_contents(path)[first.name] == first.read_bytes() for path in published)
        print("PASS target never overwritten and native-text-only ZIP needs no disk TXT")

        inside = create_archive([tree], tree / "inside.zip")
        assert all(not name.endswith(".part") and not name.endswith("inside.zip") for name in zip_contents(inside))
        assert not list(tree.glob("*.part"))
        print("PASS saving inside a selected folder does not archive its own output")

        alias = root / "folder-link"
        if os.name == "nt":
            import _winapi
            _winapi.CreateJunction(str(tree), str(alias))
        else:
            alias.symlink_to(tree, target_is_directory=True)
        linked = create_archive([alias, child, tree], out / "with-link.zip")
        linked_contents = zip_contents(linked)
        assert alias.name in linked_contents
        assert linked_contents["资料/嵌套/孩子.dat"] == child.read_bytes()
        assert "孩子.dat" not in linked_contents
        through_alias = create_archive([tree], alias / "through-link.zip")
        through_contents = zip_contents(through_alias)
        assert not any(name.endswith(".part") or name.endswith("through-link.zip") for name in through_contents)
        assert through_contents["资料/嵌套/孩子.dat"] == child.read_bytes()
        assert not list(tree.glob("*.part"))
        print("PASS junction aliases neither discard selected contents nor include their own .part")

        large = root / "large.bin"
        large.write_bytes(os.urandom(8 * 1024 * 1024))
        unrelated = out / "unrelated.part"
        unrelated.write_bytes(b"keep unrelated")
        baseline = {path.name for path in out.iterdir()}
        cancellation = CountedCancel(20)
        expect_raises(ArchiveCancelled, lambda: create_archive([large], out / "cancelled.zip", cancel_event=cancellation))
        assert cancellation.calls >= 20
        assert {path.name for path in out.iterdir()} == baseline
        stopped = threading.Event()
        stopped.set()
        expect_raises(ArchiveCancelled, lambda: create_archive([first], out / "early.zip", cancel_event=stopped))
        expect_raises(FileNotFoundError, lambda: create_archive([root / "missing"], out / "missing.zip"))
        expect_raises(ValueError, lambda: create_archive([], out / "empty.zip"))
        with patch.object(module, "_publish_part", side_effect=OSError("injected publish failure")):
            expect_raises(OSError, lambda: create_archive([first], existing))
        assert {path.name for path in out.iterdir()} == baseline
        assert unrelated.read_bytes() == b"keep unrelated" and existing.read_bytes() == b"must never change"
        print("PASS chunk cancellation, missing source and final publish failure clean only this .part")

        if os.name == "nt":
            import ctypes
            from ctypes import wintypes
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                          wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
            kernel.CreateFileW.restype = wintypes.HANDLE
            kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel.CloseHandle.restype = wintypes.BOOL
            movable = root / "source-share-delete.bin"
            payload = b"source remains readable through its open handle" * 100
            movable.write_bytes(payload)
            with module._source_reader(movable) as reader:
                # Do not truncate or change the file: merely ask Windows for
                # write permission while the archive's reader is present.
                writer = kernel.CreateFileW(str(movable), 0x40000000, 7, None, 3, 0x80, None)
                write_error = ctypes.get_last_error() if writer == ctypes.c_void_p(-1).value else 0
                if not write_error:
                    kernel.CloseHandle(writer)
                assert write_error == 32, "ZIP source reader must not permit concurrent content writes"
                moved = movable.with_name("source-renamed.bin")
                movable.rename(moved)
                moved.unlink()
                assert reader.read() == payload and not moved.exists()
            def read_source(path):
                with module._source_reader(path) as reader:
                    return reader.read(1)
            expect_raises(FileNotFoundError, lambda: read_source(root / "absent-source.bin"))
            locked_writer = root / "already-writing.bin"
            locked_writer.write_bytes(b"unchanged")
            with locked_writer.open("r+b"):
                expect_raises(PermissionError, lambda: read_source(locked_writer))
            locked_writer.unlink()
            print("PASS Windows reader allows rename/unlink, denies content writes and closes failed/open handles")

        success = ZipArchiveJob([large], out / "async.zip", texts=[("说明", "后台压缩")])
        (kind, value), beats = run_job(success)
        assert kind == "finished" and value == success.result_path
        assert zip_contents(value)["large.bin"] == large.read_bytes()
        assert len(beats) >= 2, "Qt event loop was not responsive during compression"
        failure = ZipArchiveJob([root / "no such file"], out / "async-bad.zip")
        (kind, message), _ = run_job(failure)
        assert kind == "failed" and message
        cancellation = ZipArchiveJob([large], out / "async-cancel.zip")
        (kind, _), _ = run_job(cancellation, cancellation.cancel)
        assert kind == "cancelled" and not (out / "async-cancel.zip").exists()
        assert not [path for path in out.glob("*.part") if path != unrelated]
        # Shutdown can wait for cleanup even if terminal signal delivery stops.
        shutdown = ZipArchiveJob([large], out / "shutdown.zip")
        shutdown.start()
        shutdown.cancel()
        assert shutdown.wait(3000) and not (out / "shutdown.zip").exists()
        app.processEvents()
        assert unrelated.read_bytes() == b"keep unrelated"
        print("PASS QThreadPool success/failure/cancel signals once on GUI thread, responsive timers and shutdown cleanup")
        print("SELECTION_ARCHIVE_V161_OK")
finally:
    profile.cleanup()
