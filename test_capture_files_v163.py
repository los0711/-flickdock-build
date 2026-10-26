"""Background owned copies, failure cleanup, cancellation and sharing."""
import os
os.environ['QT_QPA_PLATFORM'] = 'offscreen'

from contextlib import contextmanager
from pathlib import Path
import tempfile
import threading
import time
from unittest.mock import patch

from PySide6.QtCore import QEventLoop, QThread, QTimer
from PySide6.QtWidgets import QApplication
import capture_files as subject


def wait_until(predicate, timeout=5):
    started = time.monotonic()
    while not predicate() and time.monotonic() - started < timeout:
        loop = QEventLoop();QTimer.singleShot(5, loop.quit);loop.exec()
    assert predicate(), 'Background copy did not complete'


def watch(job):
    state = {'finished': [], 'failed': [], 'cancelled': [], 'threads': []}
    for name in ('finished', 'failed', 'cancelled'):
        getattr(job, name).connect(lambda *args, name=name: (state[name].append(args), state['threads'].append(QThread.currentThread())))
    return state


app = QApplication([])
with tempfile.TemporaryDirectory(prefix='flickdock-copy-test-') as temporary:
    root = Path(temporary)
    source, store = root / 'source', root / 'store'
    source.mkdir();store.mkdir()
    first = source / 'one' / 'shared.txt';first.parent.mkdir();first.write_text('first original', encoding='utf8')
    second = source / 'two' / 'shared.txt';second.parent.mkdir();second.write_text('second original', encoding='utf8')
    folder = source / 'folder';(folder / 'empty').mkdir(parents=True);(folder / 'nested').mkdir()
    (folder / 'nested' / 'child.bin').write_bytes(b'folder payload\x00\x01')
    foreign = store / 'imports' / 'existing-owner';foreign.mkdir(parents=True);(foreign / 'keep.txt').write_text('keep')

    job = subject.CopyFilesJob([first, folder, second], store)
    state = watch(job)
    assert job.wait(0) and job.start() and not job.start()
    wait_until(lambda: bool(state['finished']))
    assert job.wait(1000) and not state['failed'] and not state['cancelled']
    copied = list(map(Path, state['finished'][0][0]))
    assert [path.name for path in copied] == ['shared.txt', 'folder', 'shared (2).txt']
    assert copied[0].read_text() == 'first original' and copied[2].read_text() == 'second original'
    assert (copied[1] / 'empty').is_dir() and (copied[1] / 'nested' / 'child.bin').read_bytes() == b'folder payload\x00\x01'
    assert len({path.parent for path in copied}) == 1
    assert copied[0].parent.parent == store / 'imports'
    assert first.read_text() == 'first original' and second.read_text() == 'second original'
    assert all(thread == app.thread() for thread in state['threads'])
    preserved = set((store / 'imports').iterdir())
    print('PASS ordered file/folder copies, duplicates, empty folders, owned paths and GUI-thread signal')

    original_copy = subject._copy_file
    def fail_second(incoming, outgoing, event):
        if incoming == second:
            raise OSError('controlled disk failure')
        return original_copy(incoming, outgoing, event)
    with patch.object(subject, '_copy_file', side_effect=fail_second):
        failed = subject.CopyFilesJob([first, second], store);state = watch(failed);failed.start()
        wait_until(lambda: bool(state['failed']))
        assert failed.wait(1000) and not state['finished'] and not state['cancelled']
    assert set((store / 'imports').iterdir()) == preserved and (foreign / 'keep.txt').read_text() == 'keep'
    assert first.read_text() == 'first original' and second.read_text() == 'second original'
    print('PASS failure cleans only this job; originals and previous imports survive')

    entered, release = threading.Event(), threading.Event()
    def block_copy(incoming, outgoing, event):
        assert QThread.currentThread() != app.thread(), 'File I/O on GUI thread'
        entered.set();assert release.wait(5)
        return original_copy(incoming, outgoing, event)
    with patch.object(subject, '_copy_file', side_effect=block_copy):
        cancelled = subject.CopyFilesJob([first, second], store);state = watch(cancelled);cancelled.start()
        wait_until(entered.is_set)
        beats = [];QTimer.singleShot(1, lambda: beats.append(True));wait_until(lambda: bool(beats))
        started = time.perf_counter();cancelled.cancel();assert time.perf_counter() - started < .1
        assert not cancelled.wait(0)
        release.set();assert cancelled.wait(1000)
        wait_until(lambda: bool(state['cancelled']))
        assert not state['finished'] and not state['failed']
    assert set((store / 'imports').iterdir()) == preserved
    early = subject.CopyFilesJob([first], store);state = watch(early);early.cancel();early.start()
    wait_until(lambda: bool(state['cancelled']));assert early.wait(1000)
    assert set((store / 'imports').iterdir()) == preserved
    print('PASS worker I/O leaves heartbeat active; cancel and pre-start cancel clean owned staging')

    # Hold the actual sharing handle while the original is moved and deleted.
    shared = source / 'sharing.bin';payload = bytes(range(256)) * 4096;shared.write_bytes(payload)
    original_reader = subject._source_reader
    entered, release = threading.Event(), threading.Event()
    @contextmanager
    def hold_reader(path):
        with original_reader(path) as stream:
            entered.set();assert release.wait(5);yield stream
    with patch.object(subject, '_source_reader', side_effect=hold_reader):
        sharing = subject.CopyFilesJob([shared], store);state = watch(sharing);sharing.start()
        wait_until(entered.is_set)
        renamed = shared.with_name('renamed.bin');shared.rename(renamed);renamed.unlink()
        release.set();wait_until(lambda: bool(state['finished']) or bool(state['failed']))
        assert not state['failed'] and Path(state['finished'][0][0][0]).read_bytes() == payload
    print('PASS Windows DELETE-sharing allows original rename/delete while copied bytes remain intact')

    recursive = subject.CopyFilesJob([root], store);state = watch(recursive);recursive.start()
    wait_until(lambda: bool(state['failed']))
    assert '包含' in state['failed'][0][0]
    assert (foreign / 'keep.txt').is_file()
    print('PASS source containing destination is rejected without recursive self-copy')
print('COPY_FILES_V163_OK')
