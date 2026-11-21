"""Offscreen disposal regressions; natural Qt event ordering at 125% DPI."""
import gc
import os
import time
import weakref

os.environ['QT_QPA_PLATFORM'] = 'offscreen'
os.environ['QT_SCALE_FACTOR'] = '1.25'

from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtWidgets import QApplication, QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget
from shiboken6 import isValid

from widget_retirement import WidgetRetirementQueue


app = QApplication([])


def run_for(milliseconds):
    loop = QEventLoop()
    QTimer.singleShot(milliseconds, loop.quit)
    loop.exec()


def until(condition, milliseconds=2500):
    deadline = time.perf_counter()+milliseconds/1000
    while not condition() and time.perf_counter()<deadline:
        run_for(10)
    assert condition(), 'Retirement did not complete'


class Counts:
    def __init__(self): self.destroyed = 0
    def on_destroyed(self, _item=None): self.destroyed += 1


class Host(QFrame):
    def __init__(self):
        super().__init__()
        self.setStyleSheet('QFrame { background: #fafbfc; }')
        self.resize(788, 550)
        self.body = QWidget(self)
        self.body.setGeometry(0, 0, 246, 500)
        self.rows = QVBoxLayout(self.body)
        self.queue = WidgetRetirementQueue(self)
        self.counts = Counts()
        self.capture_empty = False
        self.first_empty_paint = None
        self.empty = QLabel('Empty', self)
        self.empty.setGeometry(270, 100, 200, 40)
        self.empty.hide()

    def populate(self, count):
        items = []
        for index in range(count):
            row = QFrame(self.body)
            row.setFixedHeight(40)
            layout = QHBoxLayout(row)
            layout.addWidget(QLabel(str(index), row))
            layout.addWidget(QLabel('Local item', row))
            layout.addWidget(QPushButton('Remove', row))
            row.destroyed.connect(self.counts.on_destroyed)
            self.rows.addWidget(row)
            items.append(row)
        return items

    def retire_rows(self):
        while self.rows.count():
            item = self.rows.takeAt(0)
            if item.widget(): self.queue.retire(item.widget())

    def paintEvent(self, event):
        if self.capture_empty and self.first_empty_paint is None:
            self.first_empty_paint = {
                'time': time.perf_counter(),
                'destroyed': self.counts.destroyed,
                'pending': self.queue.pending_count,
            }
        super().paintEvent(event)


def first_empty_paint_precedes_bulk_disposal():
    host = Host()
    rows = host.populate(250)
    row_refs = [weakref.ref(row) for row in rows]
    host.show()
    run_for(30)
    assert abs(host.devicePixelRatioF()-1.25)<.01
    clicked = []
    def clear():
        clicked.append(time.perf_counter())
        host.retire_rows()
        assert host.queue.pending_count == 250
        assert all(row.isHidden() for row in rows)
        host.capture_empty = True
        host.empty.show()
        host.update()
    QTimer.singleShot(0, clear)
    until(lambda: host.first_empty_paint is not None)
    first = host.first_empty_paint
    assert first['pending'] > 0 and first['destroyed'] < 250, first
    until(lambda: host.counts.destroyed == 250 and host.queue.is_idle)
    assert host.queue.pending_count == 0 and not host.queue._queued_ids
    assert all(not isValid(row) for row in rows)
    rows.clear()
    gc.collect()
    assert all(reference() is None for reference in row_refs), 'Queue retained a disposed row wrapper'
    print('PASS 250 rows: first empty Paint precedes bulk destruction',
          round((first['time']-clicked[0])*1000, 3), 'ms;', first['pending'], 'still queued')
    host.deleteLater()
    run_for(25)


def duplicates_batches_and_external_deletion():
    owner = QWidget()
    queue = WidgetRetirementQueue(owner)
    rows = [QWidget(owner) for _ in range(25)]
    counts = Counts()
    for row in rows: row.destroyed.connect(counts.on_destroyed)
    assert queue.retire_many(rows) == 25
    assert queue.retire_many(rows) == 0 and queue.pending_count == 25
    assert all(row.isHidden() for row in rows)
    # Verify a single callback never schedules more than the advertised batch.
    queue._timer.stop()
    queue._drain()
    assert queue.pending_count == 13
    queue._timer.stop()
    # An independently disposed queued widget must not crash a later batch.
    rows[-1].deleteLater()
    run_for(5)
    assert not isValid(rows[-1])
    queue._drain()
    assert queue.pending_count == 1
    until(lambda: queue.is_idle and counts.destroyed == 25)
    assert not queue._queued_ids
    owner.deleteLater()
    run_for(25)
    print('PASS duplicate retirement, batch limit and externally destroyed items')


def rapid_rebuild_close_and_owner_destruction():
    host = Host()
    first = host.populate(250)
    host.show()
    run_for(20)
    host.retire_rows()
    second = host.populate(250)
    host.retire_rows()
    assert host.queue.pending_count == 500
    host.close()
    until(lambda: host.counts.destroyed == 500 and host.queue.is_idle)
    assert all(not isValid(row) for row in first+second)
    # Closing a QWidget only hides it. An actual owner deletion must cancel
    # the timer and release the Python retirement references before child GC.
    third = host.populate(250)
    host.retire_rows()
    queue = host.queue
    timer = queue._timer
    queue_ref = weakref.ref(queue)
    row_refs = [weakref.ref(row) for row in third]
    assert queue.pending_count == 250 and timer.isActive()
    host.deleteLater()
    run_for(40)
    assert not isValid(host) and not isValid(queue) and not isValid(timer)
    assert queue.pending_count == 0 and not queue._queued_ids
    assert host.counts.destroyed == 750
    first.clear(); second.clear(); third.clear()
    del host, queue, timer
    gc.collect()
    assert queue_ref() is None and all(ref() is None for ref in row_refs), 'Owner destruction retained wrappers'
    run_for(25)
    print('PASS rapid consecutive rebuilds, hidden popup drain and owner deletion with pending items')


first_empty_paint_precedes_bulk_disposal()
duplicates_batches_and_external_deletion()
rapid_rebuild_close_and_owner_destruction()
print('WIDGET_RETIREMENT_V162_OK')
