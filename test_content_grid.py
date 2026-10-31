"""Icon item checks; synthetic offscreen widgets only, no desktop input."""
import os
os.environ['QT_QPA_PLATFORM'] = 'offscreen'
import tempfile
from pathlib import Path
from types import SimpleNamespace

from PySide6.QtCore import QEvent, QEventLoop, QPointF, Qt, QTimer
from PySide6.QtGui import QFont, QFontDatabase, QImage, QKeyEvent, QMouseEvent
from PySide6.QtWidgets import QApplication, QFrame, QGridLayout, QVBoxLayout, QWidget

from content_grid import IconItem,GRID_ITEM_MIN_WIDTH,GRID_ITEM_HEIGHT
from flickdock_motion import Spring
from shelf_content import ContentRepository


def wait(ms):
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


def mouse(widget, kind, position=(38, 50), button=Qt.MouseButton.LeftButton,
          buttons=Qt.MouseButton.LeftButton, modifiers=Qt.KeyboardModifier.NoModifier):
    event = QMouseEvent(kind, QPointF(*position), button, buttons, modifiers)
    QApplication.sendEvent(widget, event)


app = QApplication([])
font_file = Path(__file__).parent / 'fonts/HarmonyOS_Sans_SC_Regular.ttf'
if font_file.exists():
    font_id = QFontDatabase.addApplicationFont(str(font_file))
    names = QFontDatabase.applicationFontFamilies(font_id)
    if names:
        font = QFont(names[0])
        font.setPixelSize(14)
        app.setFont(font)
calls = {'touch': 0, 'preview': [], 'drag': [], 'open': [], 'copy': [], 'remove': [], 'rebuild': 0}
with tempfile.TemporaryDirectory(prefix='flickdock-grid-') as profile:
    repo = ContentRepository()
    text = repo.add_text('原生文字可以复制，也可以直接拖进输入框。')
    image = Path(profile) / '参考图片.png'
    bitmap = QImage(150, 95, QImage.Format.Format_ARGB32)
    bitmap.fill(Qt.GlobalColor.darkCyan)
    assert bitmap.save(str(image))
    refs = [text, str(image)]
    shelf = SimpleNamespace(
        content=repo, manager=SimpleNamespace(motion=True), files={'permanent': refs},
        touch=lambda: calls.__setitem__('touch', calls['touch']+1),
        drag_files=lambda mode, selected: calls['drag'].append((mode, selected)),
        open_path=lambda ref: calls['open'].append(ref),
        copy_items=lambda selected: calls['copy'].append(selected),
        _menu_open=False)
    popup = QFrame()
    popup.shelf = shelf
    popup.mode = 'permanent'
    popup.selected = set()
    popup.preview = SimpleNamespace(show_path=lambda ref: calls['preview'].append(ref))
    popup.rows_body = QWidget(popup)
    popup.rebuild = lambda: calls.__setitem__('rebuild', calls['rebuild']+1)
    popup.remove_path = lambda ref: calls['remove'].append(ref)
    outer = QVBoxLayout(popup)
    outer.addWidget(popup.rows_body)
    grid = QGridLayout(popup.rows_body)
    grid.setContentsMargins(12, 12, 12, 12)
    grid.setSpacing(12)
    items = [IconItem(popup, ref) for ref in refs]
    for index, item in enumerate(items):
        grid.addWidget(item, 0, index)
    popup.show()
    wait(30)
    assert items[0].width() >= GRID_ITEM_MIN_WIDTH and items[0].height() == GRID_ITEM_HEIGHT
    assert popup.rows_body.rect().contains(items[0].geometry())
    assert items[0].remove_button.width() == 28
    assert not items[0].icon.isNull() and not items[1].icon.isNull()
    mouse(items[0], QEvent.Type.MouseButtonPress)
    mouse(items[0], QEvent.Type.MouseButtonRelease, buttons=Qt.MouseButton.NoButton)
    assert popup.selected == {text}
    mouse(items[1], QEvent.Type.MouseButtonPress, modifiers=Qt.KeyboardModifier.ControlModifier)
    mouse(items[1], QEvent.Type.MouseButtonRelease, buttons=Qt.MouseButton.NoButton,
          modifiers=Qt.KeyboardModifier.ControlModifier)
    assert popup.selected == set(refs)
    assert calls['preview'][-1] == str(image)
    mouse(items[0], QEvent.Type.MouseButtonPress)
    mouse(items[0], QEvent.Type.MouseMove, position=(62, 52), button=Qt.MouseButton.NoButton)
    mouse(items[0], QEvent.Type.MouseButtonRelease, buttons=Qt.MouseButton.NoButton)
    wait(20)
    assert calls['drag'] == [('permanent', refs)] and calls['rebuild'] == 1
    assert popup.selected == set(refs), 'plain press on existing selection keeps a multi-drag intact'
    QApplication.sendEvent(items[0], QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier))
    assert calls['copy'][-1] == refs
    QApplication.sendEvent(items[0], QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Return, Qt.KeyboardModifier.NoModifier))
    assert calls['open'][-1] == text
    items[1].remove_button.click()
    assert calls['remove'][-1] == str(image)
    QApplication.sendEvent(items[0], QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Delete, Qt.KeyboardModifier.NoModifier))
    assert calls['remove'][-1] == text
    popup.selected = {text}
    for item in items:
        item.set_selected(item.ref in popup.selected)
    wait(180)
    assert items[0]._selected and not items[1]._selected
    assert len(items[0].findChildren(Spring)) == 2, 'one item spring + one persistent remove-button spring'
    with tempfile.TemporaryDirectory(prefix='flickdock-icon-render-') as capture:
        assert popup.grab().save(str(Path(capture)/'icon-grid.png'))
    popup.hide()
print('CONTENT_GRID_OK')
