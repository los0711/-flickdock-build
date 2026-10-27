"""Offscreen file status checks; no real desktop, account, or network access."""
import hashlib
import os
import tempfile
from pathlib import Path
from types import SimpleNamespace

os.environ['QT_QPA_PLATFORM'] = 'offscreen'
_profile = tempfile.TemporaryDirectory(prefix='flickdock-status-')
os.environ['APPDATA'] = _profile.name

from PySide6.QtWidgets import QApplication, QFrame, QVBoxLayout, QWidget

from content_grid import IconItem
from flickdock import ContentsPopup, FileRow, PreviewPane
from flickdock_motion import FeedbackButton
from shelf_content import ContentRepository
from transfer_visuals import CLOUD_STATUS_LABELS, CloudStatusBadge


app = QApplication.instance() or QApplication([])
repo = ContentRepository()
ref = repo.add_text('状态图标随真实上传阶段更新')
class FakeSync:
    state = 'pending'
    def sync_status_for_ref(self, value):
        assert value == ref
        return self.state

sync = FakeSync()
shelf = SimpleNamespace(content=repo,
    manager=SimpleNamespace(sync=sync, motion=False),
    files={'permanent': [ref]},
    item_pixmap=repo.item_pixmap)
popup = QFrame()
popup.shelf = shelf
popup.rows_body = QWidget(popup)
popup.selected = set()
popup.remove_path = lambda _: None
popup.hover_item = lambda _: None
popup.end_hover = lambda _: None
layout = QVBoxLayout(popup)
layout.addWidget(popup.rows_body)
icon = IconItem(popup, ref)
row = FileRow(popup, ref)
rows = QVBoxLayout(popup.rows_body)
rows.addWidget(icon)
rows.addWidget(row)
popup.show()
app.processEvents()

images = []
badge = CloudStatusBadge()
copy_button = FeedbackButton('copy', motion=False)
copy_button.setFixedSize(32, 32)
assert badge.size() == copy_button.size(), 'status and copy actions share one header baseline'
assert badge.grab().toImage().pixelColor(0, 16).alpha() == 0, 'status art stays centered inside its hit cell'
for status in ('pending', 'uploading', 'synced', 'local', 'paused', 'error'):
    sync.state = status
    icon.refresh_cloud_status()
    row.refresh_cloud_status()
    badge.set_status(status)
    assert icon.cloud_status == status
    assert icon.toolTip() == CLOUD_STATUS_LABELS[status]
    assert row.icon_label.cloud_status == status
    assert row.toolTip() == CLOUD_STATUS_LABELS[status]
    assert badge.accessibleName() == CLOUD_STATUS_LABELS[status]
    image = badge.grab().toImage()
    images.append(hashlib.sha256(bytes(image.bits())).hexdigest())
assert len(set(images)) == len(images), 'each upload state needs distinguishable artwork'
badge.set_status('uploading',dict(received=50,total=100,phase='uploading'))
progress_artwork=hashlib.sha256(bytes(badge.grab().toImage().bits())).hexdigest()
assert progress_artwork not in images, 'upload progress should arc around the same status mark'

preview = SimpleNamespace(current_path=ref, _group_refs=(), cloud_badge=CloudStatusBadge(), shelf=shelf)
sync.state = 'synced'
PreviewPane.refresh_cloud_status(preview)
assert preview.cloud_badge.isVisible() and preview.cloud_badge.status == 'synced'
preview._group_refs = (ref, ref)
PreviewPane.refresh_cloud_status(preview)
assert not preview.cloud_badge.isVisible(), 'a multi-selection has no single cloud status'

calls = []
sync.sync_statuses_for_refs = lambda refs: (calls.append(tuple(refs)) or {item:'synced' for item in refs})
view = SimpleNamespace(shelf=shelf, _cloud_statuses={})
ContentsPopup._read_cloud_statuses(view, [ref]*300)
assert len(calls) == 1 and len(calls[0]) == 300
assert ContentsPopup.cloud_status(view, ref) == 'synced'

popup.hide()
_profile.cleanup()
print('CLOUD_FILE_STATUS_OK')
