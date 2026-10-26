"""FlickDock: one persistent Windows shelf for files, images and text."""
from __future__ import annotations

import ctypes
import json
import math
import os
import shutil
import subprocess
import sys
import time
import zipfile
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from fractions import Fraction

from PySide6.QtCore import (QEasingCurve, QPoint, QRect, QRectF, QSize, QEvent, Qt, QMimeData,
                             QPropertyAnimation, QTimer, QUrl)
from PySide6.QtGui import (QColor, QCursor, QDrag, QFont, QFontDatabase, QGuiApplication,
                           QIcon, QLinearGradient, QPainter, QPainterPath, QPalette, QPen, QPixmap, QTransform)
from PySide6.QtWidgets import (QApplication, QFileDialog, QFrame,
                                QHBoxLayout, QLabel, QLineEdit, QMenu, QMessageBox, QPushButton,
                                QScrollArea, QSystemTrayIcon, QVBoxLayout, QWidget, QPlainTextEdit, QStackedWidget, QGridLayout, QSizePolicy, QButtonGroup)

from flickdock_icons import app_icon, tray_icon, glyph_icon, glyph_pixmap, file_pixmap, document_pixmap
from flickdock_motion import Spring, MotionShadow, ContentTile, FeedbackButton
from drag_probe import ContentDragGate, is_content_mime, native_window_move_active
from instance_guard import InstanceGuard
from shelf_content import ContentRepository, merge_shelves
from app_settings import SettingsStore, SettingsDialog
from global_hotkeys import HotkeyManager
from surface_morph import SurfaceMorph, SurfaceSnapshot
from content_grid import IconItem
from shake_detector import ShakeDetector, config_for_sensitivity
from dock_geometry import trigger_rect, reveal_rect, dock_hover_rect
from image_preview import ImagePreview
from media_preview import MediaPreview
from content_resolver import ContentResolver
from dock_preview import DockPreview
from confirm_clear import ClearConfirmOverlay, confirmation_active
from content_stack import StackTile, stack_pixmap
from selection_archive import ZipArchiveJob
from drag_artwork import drag_pixmap
from async_images import ImageCache
from widget_retirement import WidgetRetirementQueue
from capture_files import CopyFilesJob
from drop_routing import OverviewDropRouter
from app_version import APP_NAME, APP_VERSION
from update_controller import UpdateController
from platform_native import data_directory, native_input_available, mouse_buttons_mask
from account_service import AccountService
from account_panel import AccountPanel
from shelf_sync import ShelfSync
from sync_adapter import ShelfAdapter
import ui_theme

IDLE_SECONDS = 3.0
BASE = data_directory(APP_NAME)
STATE_FILE = BASE / "shelves.json"
OLD_STATE_FILE = Path(os.getenv("APPDATA", str(Path.home()))) / "DoudouShelf" / "shelves.json"
FONT_FAMILY = "HarmonyOS Sans SC"
CARD_W, CARD_H = 394, 274
EXPANDED_W, EXPANDED_H = 788, 550
MODES = ("permanent",)


def load_font(app):
    global FONT_FAMILY
    font_dir = Path(getattr(sys, "_MEIPASS", Path(__file__).parent)) / "fonts"
    families = []
    for weight in ("Regular",):
        source = font_dir / f"HarmonyOS_Sans_SC_{weight}.ttf"
        if source.exists():
            font_id = QFontDatabase.addApplicationFont(str(source))
            if font_id >= 0:
                families.extend(QFontDatabase.applicationFontFamilies(font_id))
    if "HarmonyOS Sans SC" not in families:
        raise RuntimeError("随附 HarmonyOS Sans 字体无法加载，请重新安装 FlickDock。")
    FONT_FAMILY = "HarmonyOS Sans SC"
    font = QFont(FONT_FAMILY, 11)
    font.setHintingPreference(QFont.HintingPreference.PreferFullHinting)
    app.setFont(font)

def motion_enabled() -> bool:
    if sys.platform != "win32":
        return True
    value = ctypes.c_int(1)
    try:
        ok = ctypes.windll.user32.SystemParametersInfoW(0x1042, 0, ctypes.byref(value), 0)
        return bool(value.value) if ok else True
    except OSError:
        return True


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    for i in range(2, 10000):
        candidate = path.with_name(f"{path.stem} ({i}){path.suffix}")
        if not candidate.exists():
            return candidate
    raise RuntimeError("无法找到可用文件名")


def zip_items(paths: list[str], destination: Path) -> Path:
    destination = unique_path(destination)
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
        for raw in paths:
            source = Path(raw)
            if source.is_file():
                archive.write(source, source.name)
            elif source.is_dir():
                for child in source.rglob("*"):
                    if child.is_file():
                        archive.write(child, str(Path(source.name) / child.relative_to(source)))
    return destination


def kind(path: str) -> str:
    return Path(path).suffix.lstrip(".").upper()[:4] or "FILE"


def kind_colors(text: str) -> tuple[str, str]:
    return {
        "PNG": ("#4779fa", "#7855ed"), "JPG": ("#4779fa", "#7855ed"),
        "JPEG": ("#4779fa", "#7855ed"), "ZIP": ("#f7ba32", "#acd61b"),
        "PDF": ("#fa8248", "#eb446d"), "MP4": ("#19c7d5", "#4378ed"),
        "DOCX": ("#49aaed", "#3659b4"), "XLSX": ("#31be7e", "#0b8057"),
    }.get(text, ("#53b4e9", "#6071d9"))


TEXT_SUFFIXES = {".txt", ".md", ".json", ".csv", ".log", ".xml", ".html",
                 ".py", ".js", ".ts", ".css", ".ini", ".yaml", ".yml"}
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".bmp", ".webp", ".gif"}


def text_snippet(path: str, limit: int = 240) -> str | None:
    source = Path(path)
    if source.suffix.lower() not in TEXT_SUFFIXES or not source.is_file():
        return None
    try:
        data = source.open("rb").read(4096)
        try:
            content = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            content = data.decode("gb18030", errors="replace")
        content = content.strip()
        return content[:limit] + ("…" if len(content) > limit else "")
    except OSError:
        return None


class AnimatedMenu(QMenu):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.fade = QPropertyAnimation(self, b"windowOpacity", self)
        self.fade.setDuration(120)
        self.fade.setEasingCurve(QEasingCurve.Type.OutCubic)

    def showEvent(self, event):
        self.fade.stop()
        self.setWindowOpacity(.25)
        super().showEvent(event)
        self.fade.setStartValue(.25)
        self.fade.setEndValue(1)
        self.fade.start()


def style_menu(menu: QMenu) -> None:
    palette = menu.palette()
    palette.setColor(QPalette.ColorRole.Window, QColor(ui_theme.color("surface")))
    palette.setColor(QPalette.ColorRole.Base, QColor(ui_theme.color("surface")))
    palette.setColor(QPalette.ColorRole.Text, QColor(ui_theme.color("text")))
    palette.setColor(QPalette.ColorRole.WindowText, QColor(ui_theme.color("text")))
    menu.setPalette(palette)
    set_ui_style(menu, "QMenu {background:#fff;color:#1d1d1f;border:1px solid #e5e5e9;border-radius:12px;padding:6px;}"
                       "QMenu::item {background:transparent;color:#1d1d1f;padding:8px 24px;min-width:150px;}"
                       "QMenu::item:selected {background:#e9f3ff;color:#1d1d1f;border-radius:7px;}"
                       "QMenu::separator {height:1px;background:#ededf0;margin:5px 8px;}")


def ui_style(text):
    return text

def set_ui_style(widget, source):
    ui_theme.style_widget(widget, source, FONT_FAMILY)

class DropZone(QFrame):
    def __init__(self, shelf: "ShelfWindow", mode: str):
        super().__init__(shelf.card)
        self.shelf, self.mode = shelf, mode
        self.setObjectName("dropZone")
        self.origin = QPoint()
        self.did_drag = False
        self.drop_highlight = False
        self.setAcceptDrops(True)
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedSize(362, 166)
        self.symbol = symbol = QLabel(self)
        symbol.setGeometry(13, 14, 20, 20)
        symbol.setPixmap(glyph_pixmap("shelf", 20, ui_theme.color("icon"), 2.0))
        symbol.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.title = QLabel("已收集", self)
        self.title.setGeometry(40, 10, 90, 27)
        set_ui_style(self.title, "color:#25272c;font:600 14px 'HarmonyOS Sans SC';background:transparent;")
        self.title.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.description = QLabel("", self)
        self.description.hide()
        self.description.setGeometry(238, 11, 108, 26)
        self.description.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        set_ui_style(self.description, "color:#7b838f;font:12px 'HarmonyOS Sans SC';background:transparent;")
        self.description.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.icon = StackTile(self, motion=shelf.manager.motion)
        self.icon.setGeometry(96, -3, 170, 140)
        self.icon.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.caption = QLabel(self)
        self.caption.setGeometry(136, 132, 90, 27)
        self.caption.setWordWrap(False)
        self.caption.setAlignment(Qt.AlignmentFlag.AlignCenter)
        set_ui_style(self.caption, "color:#626c79;font:13px 'HarmonyOS Sans SC';background:#e7ebf1;border-radius:13px;padding-right:12px;")
        self.caption.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.count_arrow=QLabel(self)
        self.count_arrow.setGeometry(204,138,14,14)
        self.count_arrow.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.title.hide();self.symbol.hide()
        self.feedback = Spring(self, {"hover": 0.0}, lambda _: self._apply_style(), motion=shelf.manager.motion)
        self._apply_style()

    @property
    def paths(self):
        return self.shelf.files[self.mode]

    def _apply_style(self):
        active = bool(self.shelf.popup and self.shelf.popup.isVisible() and self.shelf.popup.mode == self.mode)
        if self.drop_highlight:
            border, background = "#75b8ff", "#edf6ff"
        elif active:
            border, background = "#bfdbfa", "#f4f8fd"
        elif hasattr(self, 'feedback') and self.feedback.values["hover"] > .3:
            border, background = "#d9dfe8", "#f5f7fa"
        else:
            border, background = "#e5e8ed", "#f8f9fb"
        set_ui_style(self, f"QFrame#dropZone {{background:{background};border:1px solid {border};border-radius:18px;}}")

    def refresh(self):
        self._apply_style()
        self.count_arrow.setPixmap(glyph_pixmap('chevron',14,ui_theme.color('secondary'),dpr=self.devicePixelRatioF()))
        self.icon.set_stack(self.shelf.content,self.paths,self.shelf.preview_cache)
        if not self.paths:
            self.icon.set_content("empty")
            self.caption.setText("0 项")
            return
        self.caption.setText(f"{len(self.paths)} 项")

    def pulse(self, kind="drop", finished=None):
        self.icon.pulse(kind, finished)

    def dragEnterEvent(self, event):
        if self.shelf.accepts_content_drop(event):
            self.shelf.active_mode = self.mode
            self.drop_highlight = True
            self.icon.set_hover(True)
            self._apply_style()
            self.shelf.touch()
            event.setDropAction(Qt.DropAction.CopyAction)
            event.accept()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        if self.shelf.accepts_content_drop(event):
            self.shelf.touch()
            event.setDropAction(Qt.DropAction.CopyAction)
            event.accept()
        else:
            event.ignore()

    def dragLeaveEvent(self, event):
        self.drop_highlight = False
        self.icon.set_hover(False)
        self._apply_style()

    def dropEvent(self, event):
        self.drop_highlight = False
        self.icon.set_hover(False)
        if self.shelf.accepts_content_drop(event) and self.shelf.receive_mime(self.mode, event.mimeData()):
            event.setDropAction(Qt.DropAction.CopyAction)
            event.accept()
        else:
            event.ignore()
        self._apply_style()

    def enterEvent(self, event):
        self.shelf.touch()
        self.feedback.animate({"hover": 1.0}, response=.14, damping=1.0)
        self.icon.set_hover(True)
        super().enterEvent(event)

    def leaveEvent(self, event):
        self.feedback.animate({"hover": 0.0}, response=.14, damping=1.0)
        self.icon.set_hover(False)
        super().leaveEvent(event)

    def mousePressEvent(self, event):
        self.shelf.touch()
        self.shelf.active_mode = self.mode
        if event.button() == Qt.MouseButton.LeftButton:
            self.origin = event.position().toPoint()
            self.did_drag = False
            self.icon.set_pressed(True)
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        self.shelf.touch()
        if event.buttons() & Qt.MouseButton.LeftButton and self.paths and not self.did_drag and (event.position().toPoint() - self.origin).manhattanLength() > 8:
            self.did_drag = True
            self.icon.set_pressed(False)
            self.shelf.drag_files(self.mode, list(self.paths))
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        self.shelf.touch()
        self.icon.set_pressed(False)
        if event.button() == Qt.MouseButton.LeftButton and not self.did_drag:
            self.shelf.show_contents(self.mode) if self.paths else self.shelf.pick_files(self.mode)
        super().mouseReleaseEvent(event)

    def contextMenuEvent(self, event):
        self.shelf.show_menu(self.mode, event.globalPos())
        event.accept()


class ElidedLabel(QLabel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.full_text = ""
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setWordWrap(False)
        self.setFixedHeight(23)
        self.setMinimumWidth(1)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)

    def setText(self, text):
        self.full_text = str(text).replace("\n", " ").replace("\r", "")
        self.setAccessibleName(self.full_text)
        self._elide()

    def _elide(self):
        QLabel.setText(self, self.fontMetrics().elidedText(self.full_text, Qt.TextElideMode.ElideRight, max(1, self.width())))

    def resizeEvent(self, event):
        self._elide()
        super().resizeEvent(event)


class PreviewPane(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.shelf = parent.shelf
        self.popup = parent
        self.current_path = None
        self.setObjectName("previewPane")
        set_ui_style(self, "QFrame#previewPane {background:#f5f7fa;border:1px solid #e6eaf0;border-radius:16px;}")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16,14,16,16)
        layout.setSpacing(12)
        header = QHBoxLayout()
        labels = QVBoxLayout(); labels.setSpacing(3)
        self.name = ElidedLabel(self)
        set_ui_style(self.name, "color:#28313d;font:600 14px 'HarmonyOS Sans SC';background:transparent;")
        self.info = ElidedLabel(self)
        set_ui_style(self.info, "color:#738091;font:12px 'HarmonyOS Sans SC';background:transparent;")
        labels.addWidget(self.name); labels.addWidget(self.info)
        self.copy_button = FeedbackButton("copy", self, motion=self.shelf.manager.motion)
        self.copy_button.setFixedSize(32,32)
        self.copy_button.clicked.connect(self.copy_current)
        header.addLayout(labels,1); header.addWidget(self.copy_button,0,Qt.AlignmentFlag.AlignTop)
        layout.addLayout(header)
        self.stack=QStackedWidget(self)
        set_ui_style(self.stack, "background:transparent;border:0;")
        self.icon=ContentTile(self,motion=self.shelf.manager.motion)
        self.text=QPlainTextEdit(self)
        self.text.setReadOnly(True); self.text.setAcceptDrops(False); self.text.viewport().setAcceptDrops(False)
        set_ui_style(self.text,"QPlainTextEdit {background:white;color:#303a48;border:1px solid #e1e7ef;border-radius:10px;padding:13px;font:14px 'HarmonyOS Sans SC';selection-background-color:#d9eaff;selection-color:#213247;} QScrollBar:vertical {width:6px;background:transparent;} QScrollBar::handle:vertical {background:#c2cbd8;border-radius:3px;min-height:24px;} QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical {height:0;}")
        self.image=ImagePreview(self)
        self.image.set_loader(self.shelf.image_cache)
        self.image.source_ready.connect(self._image_source_ready)
        self.media=MediaPreview(self,motion=self.shelf.manager.motion)
        self.media.frame_ready.connect(self._poster_ready)
        self.media.playing_changed.connect(self._playing_changed)
        for widget in (self.icon,self.text,self.image,self.media): self.stack.addWidget(widget)
        layout.addWidget(self.stack,1)

    def _playing_changed(self, playing):
        self.shelf.preview_playing=bool(playing)
        self.shelf.touch()

    def _poster_ready(self, path, image):
        if not image.isNull():
            self.shelf.preview_cache[path]=image.copy()
            if len(self.shelf.preview_cache)>100: self.shelf.preview_cache.pop(next(iter(self.shelf.preview_cache)))
            if self.popup.isVisible(): self.popup.update_item_icon(path)
            self.shelf.zones['permanent'].refresh()

    def _show_image(self,path):
        if not self.image.set_source(path):return False
        if self.image.image is not None or self.image.renderer is not None:
            self.stack.setCurrentWidget(self.image)
        else:
            self.icon.set_content('file',pixmap=self.shelf.item_pixmap(self.current_path,128,dpr=self.devicePixelRatioF()))
            self.stack.setCurrentWidget(self.icon)
        return True

    def _image_source_ready(self,path,success):
        ref=self.current_path
        if not ref or getattr(self,'_group_refs',()):return
        expected=self.shelf.content.metadata.get(ref,{}).get('preview_image') or ref
        if os.path.normcase(os.path.abspath(expected))!=os.path.normcase(os.path.abspath(path)):return
        if success:self.stack.setCurrentWidget(self.image)
        elif self.shelf.content.text(ref) is not None:
            self.text.setPlainText(self.shelf.content.text(ref));self.stack.setCurrentWidget(self.text)
        else:
            self.icon.set_content('file',pixmap=self.shelf.item_pixmap(ref,128,dpr=self.devicePixelRatioF()))
            self.stack.setCurrentWidget(self.icon)

    def show_path(self, ref, force=False):
        if self.current_path==ref and not force and not getattr(self,'_group_refs',()): return
        self._group_refs=()
        self.media.stop()
        self.current_path=ref
        self.copy_button.set_glyph("copy")
        self.name.setText(self.shelf.content.title(ref))
        # Decode images and construct native media only after the surface has
        # landed. The endpoint snapshot needs just a lightweight format card.
        category=self.shelf.content.category(ref)
        self._pending_preview=(self.shelf.content_transition_active() and
            category not in ('images','text','links'))
        if self._pending_preview:
            self.icon.set_content('file',pixmap=self.shelf.item_pixmap(ref,128,dpr=self.devicePixelRatioF()))
            self.stack.setCurrentWidget(self.icon)
            self.info.setText('')
            return
        raw=self.shelf.content.text(ref)
        meta=self.shelf.content.metadata.get(ref,{})
        if raw is not None:
            cover=meta.get('preview_image')
            if cover and self._show_image(cover):
                self.info.setText("网页 · "+QUrl(raw).host())
            else:
                self.text.setPlainText(raw)
                self.stack.setCurrentWidget(self.text)
                self.info.setText("正在解析…" if meta.get('loading') else "网页链接" if self.shelf.content.category(ref)=='links' else f"文字 · {len(raw)} 字符")
            self.copy_button.setToolTip("复制链接" if self.shelf.content.category(ref)=='links' else "复制文字")
            return
        source=Path(ref)
        if self.shelf.content.category(ref)=='images' and self._show_image(ref):
            self.info.setText("矢量图像" if source.suffix.lower() in ('.svg','.svgz') else kind(ref)+" 图像")
        elif self.shelf.content.category(ref) in ('video','audio') and MediaPreview.can_preview(ref):
            self.stack.setCurrentWidget(self.media)
            self.media.set_source(ref)
            self.info.setText("视频" if self.shelf.content.category(ref)=='video' else "音频")
        elif text_snippet(ref,1) is not None:
            try:
                data=source.read_bytes()
                try: text=data.decode('utf-8-sig')
                except UnicodeDecodeError: text=data.decode('gb18030',errors='replace')
            except OSError: text=""
            self.text.setPlainText(text); self.stack.setCurrentWidget(self.text)
            self.info.setText(f"{kind(ref)} · {len(text)} 字符")
        else:
            self.icon.set_content("file",pixmap=self.shelf.item_pixmap(ref,128,dpr=self.devicePixelRatioF()))
            self.stack.setCurrentWidget(self.icon)
            try: size=source.stat().st_size if source.is_file() else 0
            except OSError: size=0
            self.info.setText(kind(ref)+" · "+(f"{size/1024:.1f} KB" if size<1024*1024 else f"{size/(1024*1024):.1f} MB"))
        self.copy_button.setToolTip("复制文字内容" if self.stack.currentWidget() is self.text else "复制文件")

    def show_group(self,refs):
        refs=tuple(refs)
        if getattr(self,'_group_refs',())==refs: return
        self.media.stop();self._pending_preview=False;self._group_refs=refs;self.current_path=refs[-1]
        self.name.setText(f'{len(refs)} 项已选');self.info.setText('可一起拖出或打包 ZIP')
        self.icon.set_content('file',pixmap=stack_pixmap(self.shelf.content,refs,128,dpr=self.devicePixelRatioF(),preview_cache=self.shelf.preview_cache))
        self.stack.setCurrentWidget(self.icon);self.copy_button.setToolTip('复制所选内容')

    def resume_preview(self):
        if (not getattr(self,'_pending_preview',False) or self.shelf.content_transition_active()
                or not self.shelf.expanded or not self.shelf.isVisible()): return
        ref=self.current_path
        if ref in self.shelf.files['permanent']:
            self.show_path(ref,force=True)

    def copy_current(self):
        if not self.current_path: return
        if getattr(self,'_group_refs',()): self.shelf.copy_items(self._group_refs)
        elif self.stack.currentWidget() is self.text: QApplication.clipboard().setText(self.text.toPlainText())
        else: self.shelf.copy_items([self.current_path])
        self.copy_button.set_glyph("check"); self.copy_button.spring.kick({"scale":1.8})
        QTimer.singleShot(850,lambda:self.copy_button.set_glyph("copy"))
        self.shelf.touch()

    def enterEvent(self,event):
        self.popup.hovered_ref=None
        self.popup.restore_selected_preview()
        super().enterEvent(event)

    def hideEvent(self,event):
        if self.current_path and self.stack.currentWidget() is self.media:self._pending_preview=True
        self.media.stop()
        super().hideEvent(event)

class FileRow(QFrame):
    def __init__(self, popup, path):
        super().__init__(popup.rows_body)
        self.popup, self.path = popup, path
        self.setAcceptDrops(True)
        self.origin = QPoint()
        self.did_drag = False
        self.setFixedHeight(52)
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.motion = Spring(self, {"highlight": 0.0}, lambda _: self.update(), motion=popup.shelf.manager.motion)
        row = QHBoxLayout(self)
        row.setContentsMargins(7, 5, 4, 5)
        row.setSpacing(7)
        icon = QLabel(self)
        icon.setFixedSize(28, 28)
        self.icon_label = icon
        icon.setPixmap(popup.shelf.item_pixmap(path,28,dpr=self.devicePixelRatioF()))
        icon.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        name = ElidedLabel(self)
        name.setText(popup.shelf.content.title(path))
        name.setToolTip("")
        name.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        set_ui_style(name, "color:#354252;background:transparent;font:13px 'HarmonyOS Sans SC';")
        remove = FeedbackButton("close", self, motion=popup.shelf.manager.motion)
        self.remove_button=remove
        remove.setFixedSize(28, 28)
        policy=remove.sizePolicy();policy.setRetainSizeWhenHidden(True);remove.setSizePolicy(policy)
        remove.setToolTip("移出搁架")
        remove.clicked.connect(lambda: popup.remove_path(path))
        row.addWidget(icon)
        row.addWidget(name, 1)
        row.addWidget(remove)
        remove.hide()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        alpha = 210 if self.path in self.popup.selected else int(150 * self.motion.values["highlight"])
        p.setPen(Qt.PenStyle.NoPen)
        highlight = QColor(ui_theme.color("selected"))
        highlight.setAlpha(max(0, min(255, alpha)))
        p.setBrush(highlight)
        p.drawRoundedRect(self.rect().adjusted(0, 1, -1, -1), 9, 9)

    def enterEvent(self, event):
        self.remove_button.show()
        self.popup.hover_item(self.path)
        self.popup.shelf.touch()
        self.motion.animate({"highlight": 1.0}, response=.14, damping=1)
        super().enterEvent(event)

    def leaveEvent(self, event):
        self.remove_button.hide()
        self.popup.end_hover(self.path)
        self.motion.animate({"highlight": 0.0}, response=.14, damping=1)
        super().leaveEvent(event)

    def mousePressEvent(self, event):
        self.popup.shelf.touch()
        if event.button() == Qt.MouseButton.LeftButton:
            self.origin = event.position().toPoint()
            self.did_drag = False
            self._pending_deselect=(self.path in self.popup.selected and not event.modifiers() &
                (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier))
            if not self._pending_deselect:
                self.popup.select_item(self.path, bool(event.modifiers() & Qt.KeyboardModifier.ControlModifier),bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier))
            self.popup.rows_body.update()
            for child in self.popup.rows_body.findChildren(FileRow):
                child.update()
            self.motion.animate({"highlight": 1.0}, response=.1, damping=1)
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if event.buttons() & Qt.MouseButton.LeftButton and not self.did_drag and (event.position().toPoint() - self.origin).manhattanLength() > 8:
            self.did_drag = True
            chosen = [p for p in self.popup.shelf.files[self.popup.mode] if p in self.popup.selected]
            self.popup.shelf.drag_files(self.popup.mode, chosen or [self.path])
            QTimer.singleShot(0, self.popup.rebuild)
        super().mouseMoveEvent(event)

    def mouseDoubleClickEvent(self, event):
        self.popup.shelf.open_path(self.path)
        self.popup.shelf.touch()
        super().mouseDoubleClickEvent(event)

    def mouseReleaseEvent(self,event):
        if event.button()==Qt.MouseButton.LeftButton and not self.did_drag and getattr(self,'_pending_deselect',False):
            self.popup.select_item(self.path,toggle=True)
        self._pending_deselect=False
        super().mouseReleaseEvent(event)

    def contextMenuEvent(self,event):
        self.popup.show_item_menu(self.path,event.globalPos());event.accept()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_C and event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            refs = [ref for ref in self.popup.shelf.files[self.popup.mode] if ref in self.popup.selected]
            self.popup.shelf.copy_items(refs or [self.path])
            event.accept()
        elif event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.popup.shelf.open_path(self.path)
            event.accept()
        elif event.key() == Qt.Key.Key_Delete:
            self.popup.remove_path(self.path)
            event.accept()
        else:
            super().keyPressEvent(event)


class ContentsPopup(QFrame):
    CATEGORIES=(("all","全部"),("images","图片"),("video","视频"),("audio","音频"),("text","文字"),("files","文件"),("links","网页"))
    def __init__(self,shelf,mode="permanent"):
        super().__init__(shelf.card)
        self.shelf,self.mode=shelf,"permanent"
        self.selected=set(); self.primary_selection=None; self.hovered_ref=None; self.category="all"
        self.retirement=WidgetRetirementQueue(self)
        self._build_timer=QTimer(self);self._build_timer.setSingleShot(True)
        self._build_timer.timeout.connect(self._append_batch)
        set_ui_style(self,"background:transparent;")
        root=QVBoxLayout(self); root.setContentsMargins(0,0,0,0); root.setSpacing(12)
        header=QHBoxLayout()
        self.title=QLabel(self)
        set_ui_style(self.title,"color:#28313d;font:600 14px 'HarmonyOS Sans SC';background:transparent;")
        header.addWidget(self.title); header.addStretch()
        self.zip_button=QPushButton('打包所选',self)
        self.zip_button.setAccessibleName('将所选内容打包为 ZIP')
        self.zip_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.zip_button.clicked.connect(self.compress_selected)
        set_ui_style(self.zip_button,"QPushButton {color:#1876db;background:#e9f3ff;border:0;border-radius:7px;padding:6px 10px;font:13px 'HarmonyOS Sans SC';} QPushButton:hover {background:#d9eaff;} QPushButton:disabled {color:#8490a0;}")
        header.addWidget(self.zip_button)
        self.zip_all_button=QPushButton('打包全部',self)
        self.zip_all_button.setAccessibleName('将搁架全部内容打包为 ZIP')
        self.zip_all_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.zip_all_button.clicked.connect(lambda:self.shelf.compress_refs(list(self.shelf.files['permanent'])))
        set_ui_style(self.zip_all_button,"QPushButton {color:#1876db;background:#e9f3ff;border:0;border-radius:7px;padding:6px 10px;font:13px 'HarmonyOS Sans SC';} QPushButton:hover {background:#d9eaff;} QPushButton:disabled {color:#8490a0;}")
        header.addWidget(self.zip_all_button)
        self.clear_button=QPushButton("清空全部",self)
        self.clear_button.setAccessibleName("清空全部内容")
        self.clear_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.clear_button.clicked.connect(self.shelf.confirm_clear_all)
        set_ui_style(self.clear_button,"QPushButton {color:#626c79;background:transparent;border:0;border-radius:7px;padding:6px 10px;font:13px 'HarmonyOS Sans SC';} QPushButton:hover {background:#eef1f6;color:#b9404c;} QPushButton:pressed {background:#e1e7ef;} QPushButton:disabled {color:#8490a0;}")
        header.addWidget(self.clear_button); root.addLayout(header)
        filters=QHBoxLayout(); filters.setSpacing(5)
        self.category_group=QButtonGroup(self); self.category_group.setExclusive(True); self.category_buttons={}
        for key,label in self.CATEGORIES:
            button=QPushButton(label,self); button.setCheckable(True); button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setMinimumHeight(30); self.category_group.addButton(button); self.category_buttons[key]=button
            set_ui_style(button,"QPushButton {background:transparent;color:#626c79;border:0;border-radius:7px;padding:5px 12px;font:13px 'HarmonyOS Sans SC';} QPushButton:hover {background:#eef1f6;} QPushButton:checked {background:#e7ebf1;color:#28313d;font-weight:600;}")
            button.clicked.connect(lambda checked=False,key=key:self.set_category(key))
            filters.addWidget(button)
        filters.addStretch(); root.addLayout(filters)
        body=QHBoxLayout(); body.setSpacing(16)
        self.scroll=QScrollArea(self); self.scroll.setFixedWidth(246); self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame); self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        set_ui_style(self.scroll,"QScrollArea {background:transparent;border:0;} QScrollBar:vertical {width:5px;background:transparent;} QScrollBar::handle:vertical {background:#c3ccd9;border-radius:2px;min-height:22px;} QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical {height:0;}")
        self.rows_body=QWidget(); self.rows_layout=QVBoxLayout(self.rows_body)
        self.rows_layout.setContentsMargins(0,0,3,0); self.rows_layout.setSpacing(3); self.scroll.setWidget(self.rows_body)
        self.preview=PreviewPane(self); body.addWidget(self.scroll); body.addWidget(self.preview,1)
        root.addLayout(body,1); self.rebuild()
        self.drop_router=OverviewDropRouter(self)

    def visible_refs(self):
        return [ref for ref in reversed(self.shelf.files['permanent']) if self.category=='all' or self.shelf.content.category(ref)==self.category]

    def set_mode(self,mode="permanent"): self.rebuild()

    def set_category(self,key):
        if key not in self.category_buttons: return
        self.category=key; self.hovered_ref=None
        self.selected.intersection_update(self.visible_refs())
        if self.primary_selection not in self.selected: self.primary_selection=None
        self.rebuild(); self.shelf.touch()

    def _clear_layout(self,layout):
        while layout.count():
            item=layout.takeAt(0)
            if item.widget(): self.retirement.retire(item.widget())
            elif item.layout(): self._clear_layout(item.layout()); item.layout().deleteLater()

    def rebuild(self):
        refs=self.visible_refs()
        previous_signature=getattr(self,'_render_signature',None)
        signature=(tuple(refs),self.category,self.shelf.manager.settings.view_mode,
                   self.shelf.manager.settings.theme,
                   tuple((ref,repr(self.shelf.content.metadata.get(ref,{}))) for ref in refs))
        if getattr(self,'_render_signature',None)==signature:
            return
        self._render_signature=signature
        self._build_timer.stop()
        self._build_epoch=getattr(self,'_build_epoch',0)+1
        epoch=self._build_epoch
        position=self.scroll.verticalScrollBar().value()
        self._clear_layout(self.rows_layout)
        self.hovered_ref=None
        self.selected.intersection_update(refs)
        if self.primary_selection not in self.selected: self.primary_selection=next((r for r in refs if r in self.selected),None)
        self.title.setText(f"内容总览 · {len(refs)} 项")
        self.clear_button.setEnabled(self.shelf.has_files())
        self.category_buttons[self.category].setChecked(True)
        if self.shelf.manager.settings.view_mode=='icons':
            grid=QGridLayout(); grid.setContentsMargins(0,0,0,0); grid.setHorizontalSpacing(8); grid.setVerticalSpacing(8)
            self.rows_layout.addLayout(grid)
            def append(index,ref): grid.addWidget(IconItem(self,ref),index//2,index%2)
        else:
            def append(index,ref): self.rows_layout.insertWidget(self.rows_layout.count()-1,FileRow(self,ref))
        self.rows_layout.addStretch()
        self._build_refs=refs;self._build_cursor=0;self._build_append=append
        self._build_position=position
        # Four initial cards are enough to establish the endpoint. Remaining
        # widgets wait until the transition ends, with a small per-tick budget.
        self._append_batch(initial=True)
        if refs:
            current=self.primary_selection or (self.preview.current_path if self.preview.current_path in refs else refs[0])
            previous_metadata=dict(previous_signature[4]) if previous_signature else {}
            self.preview.show_path(current,force=previous_metadata.get(current)!=repr(self.shelf.content.metadata.get(current,{}))); self.preview.show()
            self.restore_selected_preview()
        else:
            self.preview.media.stop(); self.preview.hide()
            empty=QLabel("还没有收集内容" if not self.shelf.has_files() else "没有这类内容",self.rows_body)
            set_ui_style(empty,"color:#738091;font:13px 'HarmonyOS Sans SC';padding:18px 8px;")
            self.rows_layout.insertWidget(0,empty)
        self.scroll.verticalScrollBar().setValue(position)
        self.update_selection_actions()

    def _append_batch(self,initial=False):
        if not initial and (self.shelf.content_transition_active() or not self.isVisible()): return
        start=time.perf_counter(); count=0
        while self._build_cursor<len(self._build_refs):
            index=self._build_cursor
            self._build_append(index,self._build_refs[index]);self._build_cursor+=1;count+=1
            if count>=(4 if initial else 6) or time.perf_counter()-start>=.003: break
        if self._build_cursor<len(self._build_refs):
            if not self.shelf.content_transition_active(): self._build_timer.start(12)
        elif self._build_position and not self.scroll.verticalScrollBar().value():
            self.scroll.verticalScrollBar().setValue(self._build_position)

    def resume_content(self):
        if self.shelf.content_transition_active() or not self.isVisible(): return
        if self._build_cursor<len(self._build_refs): self._build_timer.start(12)
        self.preview.resume_preview()

    def chosen_refs(self):
        return [ref for ref in self.shelf.files['permanent'] if ref in self.selected]

    def update_selection_actions(self):
        count=len(self.selected);busy=self.shelf.archive_job is not None
        choosing=self.shelf._archive_choosing
        self.zip_button.setVisible(True)
        self.zip_button.setText('打包中…' if busy else '选择位置…' if choosing else '打包所选')
        self.zip_button.setEnabled(bool(count) and not busy and not choosing)
        self.zip_all_button.setEnabled(self.shelf.has_files() and not busy and not choosing)
        self.zip_button.setToolTip(f'将选中的 {count} 项打包为 ZIP')

    def select_item(self,ref,toggle=False,extend=False):
        refs=self.visible_refs()
        if extend and self.primary_selection in refs and ref in refs:
            start,end=sorted((refs.index(self.primary_selection),refs.index(ref)))
            self.selected.update(refs[start:end+1])
        elif toggle:
            if ref in self.selected: self.selected.remove(ref)
            else: self.selected.add(ref)
        elif ref not in self.selected: self.selected={ref}
        self.primary_selection=ref if ref in self.selected else next((r for r in self.visible_refs() if r in self.selected),None)
        self.hovered_ref=None
        self.restore_selected_preview()
        for item in self.rows_body.findChildren(QWidget):
            if hasattr(item,'set_selected'): item.set_selected(item.path in self.selected)
            elif isinstance(item,FileRow): item.update()
        self.update_selection_actions()

    def hover_item(self,ref):
        self.hovered_ref=ref; self.preview.show_path(ref)

    def end_hover(self,ref):
        if self.hovered_ref==ref: self.hovered_ref=None
        QTimer.singleShot(0,self.restore_selected_preview)

    def restore_selected_preview(self):
        if self.hovered_ref is not None:return
        chosen=self.chosen_refs()
        if len(chosen)>1:self.preview.show_group(chosen)
        elif self.primary_selection in self.visible_refs(): self.preview.show_path(self.primary_selection)

    def compress_selected(self):
        self.shelf.compress_refs(self.chosen_refs())

    def show_item_menu(self,ref,position):
        if ref not in self.selected:self.select_item(ref)
        refs=self.chosen_refs() or [ref]
        menu=AnimatedMenu(self);style_menu(menu)
        if not self.shelf.content.is_text(ref):
            menu.addAction('打开文件',lambda:self.shelf.open_path(ref))
            menu.addAction('在文件夹中显示',lambda:self.shelf.reveal_ref(ref))
            menu.addAction('复制文件路径',lambda:QApplication.clipboard().setText(ref))
        menu.addAction('复制所选内容',lambda:self.shelf.copy_items(refs))
        action=menu.addAction('将所选内容打包为 ZIP…',lambda:self.shelf.compress_refs(refs))
        action.setEnabled(self.shelf.archive_job is None)
        if len(refs)==1 and not self.shelf.content.is_text(ref):
            menu.addAction('重命名文件…',lambda:self.shelf.rename_ref(ref))
        if any(not self.shelf.content.is_text(item) for item in refs):
            menu.addAction('移动所选文件到…',lambda:self.shelf.move_files('permanent',refs))
        menu.addAction('从搁架移除此项',lambda:self.remove_path(ref))
        self.shelf._menu_open=True
        try:menu.exec(position)
        finally:self.shelf._menu_open=False;menu.deleteLater();self.shelf.touch()

    def update_item_icon(self,ref):
        for item in self.rows_body.findChildren(QWidget):
            if getattr(item,'path',None)!=ref: continue
            if isinstance(item,IconItem): item.icon=self.shelf.item_pixmap(ref,64,dpr=item.devicePixelRatioF()); item.update()
            elif isinstance(item,FileRow): item.icon_label.setPixmap(self.shelf.item_pixmap(ref,28,dpr=item.devicePixelRatioF()))

    def remove_path(self,ref): self.shelf.remove_path('permanent',ref)

    def leaveEvent(self,event):
        self.hovered_ref=None; self.restore_selected_preview(); super().leaveEvent(event)

class ShelfWindow(QWidget):
    def __init__(self, manager: "ShelfManager", number: int, data: dict | None = None):
        super().__init__()
        self.manager = manager
        self.number = number
        data = data or {}
        self.content = ContentRepository(data.get("texts"),data.get("metadata"))
        self.image_cache=ImageCache(self)
        self.content.thumbnail_provider=self.image_cache.pixmap
        self._transition_preparing=False
        self._pending_image_paths=set()
        self._image_timer=QTimer(self);self._image_timer.setSingleShot(True)
        self._image_timer.timeout.connect(self._flush_images)
        self.image_cache.ready.connect(self._image_ready)
        refs = data.get("permanent", [])
        self.files = {"permanent": [ref for ref in refs if isinstance(ref, str)] if isinstance(refs, list) else []}

        self.preview_cache = {}
        self.preview_playing = False
        self.pinned = bool(data.get("pinned", False))
        self.last_activity = time.monotonic()
        self.dragging = False
        self.drag_origin: QPoint | None = None
        self.visible_state = "hidden"
        self.handle: DockHandle | None = None
        self.popup: ContentsPopup | None = None
        self.archive_job=None
        self._archive_choosing=False
        self._archive_refs=()
        self._archive_epoch=0
        self.copy_jobs=set()
        self._copy_epoch=0
        self.active_mode = "permanent"
        self._rename_mode = "permanent"
        self.setWindowFlags(Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint |
                            Qt.WindowType.WindowStaysOnTopHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAcceptDrops(True)
        self.setMinimumSize(CARD_W + 28, CARD_H + 27)
        self.resize(CARD_W + 28, CARD_H + 27)
        self._build_ui()
        self.resolver=ContentResolver(self.content,lambda:self.manager.settings.capture_dir,lambda ref:ref in self.files['permanent'],on_ready=self._resolved_content,parent=self)
        self.refresh()
        QTimer.singleShot(0, self._resume_pending_content)
        pos = data.get("pos")
        if isinstance(pos, list) and len(pos) == 2:
            self.move(int(pos[0]), int(pos[1]))

    def _build_ui(self):
        self.setWindowTitle(APP_NAME)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.presentation_kind = "manual"
        self._menu_open = False
        self.incoming_drop = False
        self._intake_epoch = 0
        self._intake_stage = 'idle'
        self._intake_timer=QTimer(self)
        self._intake_timer.setInterval(16)
        self._intake_timer.timeout.connect(self._advance_intake)
        self._intake_tile=None
        self.active_drag = None
        self.expanded = False
        self.opened_at = 0.0
        self._dock_target = QPoint()
        self.card = QFrame(self)
        self.card.setObjectName("shelfCard")
        self.card.setGeometry(14, 7, CARD_W, CARD_H)
        set_ui_style(self.card, "QFrame#shelfCard {background:#ffffff;border:1px solid #e1e5ec;border-radius:24px;}")
        self.surface_effect = MotionShadow(self.card, alpha=40)
        self.card.setGraphicsEffect(self.surface_effect)
        self.presentation = Spring(self, {"sx": 1, "sy": 1, "alpha": 1, "dx": 0, "dy": 0}, self._apply_presentation, motion=self.manager.motion)
        self._morph_host_active=False
        self.geometry_morph=SurfaceMorph(self,self.geometry(),self._apply_geometry,motion=self.manager.motion,prepare=self._prepare_morph,settle=self._settle_morph)
        self._explicit_hiding = False
        logo = QLabel(self.card)
        logo.setGeometry(17, 16, 32, 32)
        logo.setPixmap(app_icon().pixmap(QSize(32,32),self.devicePixelRatioF()))
        logo.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        title = QLabel(APP_NAME, self.card)
        title.setGeometry(56, 20, 144, 26)
        set_ui_style(title, "color:#282d35;font:600 15px 'HarmonyOS Sans SC';background:transparent;")
        title.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.shelf_label = QLabel("", self.card)
        self.shelf_label.setGeometry(56, 34, 144, 16)
        set_ui_style(self.shelf_label, "color:#7c8796;font:11px 'HarmonyOS Sans SC';background:transparent;")
        self.shelf_label.hide()
        self.shelf_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.menu_button = FeedbackButton("more", self.card, motion=self.manager.motion)
        self.menu_button.setGeometry(CARD_W - 80, 17, 30, 30)
        self.menu_button.clicked.connect(lambda: self.show_menu())
        self.close_button = FeedbackButton("dock", self.card, motion=self.manager.motion)
        self.close_button.setGeometry(CARD_W - 44, 17, 30, 30)
        self.close_button.clicked.connect(lambda: self.hide_animated(explicit=True))
        self.pin_button=FeedbackButton("pin-filled" if self.pinned else "pin",self.card,motion=self.manager.motion)
        self.pin_button.setCheckable(True); self.pin_button.setChecked(self.pinned)
        self.pin_button.clicked.connect(self.toggle_pin)
        self.pin_button.setToolTip("取消钉住" if self.pinned else "钉住搁架")
        self.pin_button.setAccessibleName(self.pin_button.toolTip())
        self.expand_button = FeedbackButton("expand", self.card, motion=self.manager.motion)
        self.expand_button.setToolTip("展开内容")
        self.expand_button.clicked.connect(self.toggle_contents)
        self.zones = {mode: DropZone(self, mode) for mode in MODES}
        self.zones["permanent"].setGeometry(16, 66, 362, 166)
        self.status_label = QLabel(self.card)
        self.status_label.setGeometry(20, 240, CARD_W - 40, 22)
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        set_ui_style(self.status_label, "color:#8490a0;font:11px 'HarmonyOS Sans SC';background:transparent;")
        self.rename_edit = QLineEdit(self.card)
        set_ui_style(self.rename_edit, "QLineEdit {background:white;color:#313741;border:1px solid #72aff3;border-radius:8px;padding:3px 6px;font:11px 'HarmonyOS Sans SC';}")
        self.rename_edit.returnPressed.connect(self.commit_rename)
        self.rename_edit.editingFinished.connect(self.commit_rename)
        self.rename_edit.hide()
        for button in (self.pin_button,self.expand_button,self.menu_button,self.close_button):
            button.setParent(self)
        self._layout_header(CARD_W)

    def _layout_header(self,width):
        center=QRectF(self.card.geometry()).center()
        effect=self.surface_effect
        transform=QTransform()
        transform.translate(center.x()+effect.dx,center.y()+effect.dy)
        transform.scale(effect.sx,effect.sy);transform.translate(-center.x(),-center.y())
        for button,x in ((self.pin_button,width-176),(self.expand_button,width-132),(self.menu_button,width-88),(self.close_button,width-44)):
            point=transform.map(QRectF(self.card.x()+x,self.card.y()+14,36,36).center())
            button.setFixedSize(36,36);button.move(round(point.x()-18),round(point.y()-18))
            button.set_presentation_scale(effect.sx,effect.sy)
            button.setVisible(self.presentation_kind!='drag')
            button.raise_()

    def toggle_pin(self):
        self.pinned=self.pin_button.isChecked()
        self.pin_button.set_glyph('pin-filled' if self.pinned else 'pin')
        self.pin_button.setToolTip('取消钉住' if self.pinned else '钉住搁架')
        self.pin_button.setAccessibleName(self.pin_button.toolTip())
        self.touch(); self.manager.save()

    def _prepare_morph(self,origin,target):
        with self._surface_transaction():
            self._prepare_morph_frame(origin,target)

    @contextmanager
    def _surface_transaction(self):
        """Publish one complete presentation state, never a layout checkpoint."""
        enabled=self.updatesEnabled()
        if enabled:self.setUpdatesEnabled(False)
        try:yield
        finally:
            if enabled:
                self.setUpdatesEnabled(True)
                self.update()

    def _expansion_rect(self,current):
        area=(QGuiApplication.screenAt(current.center()) or QGuiApplication.primaryScreen()).availableGeometry()
        width,height=min(EXPANDED_W,area.width()-38),min(EXPANDED_H,area.height()-38)
        right=current.x()+current.width()
        x=max(area.left()+4,min(right-width-28,area.right()-width-20))
        y=max(area.top()+4,min(current.y(),area.bottom()-height-30))
        return QRect(x,y,width+28,height+27)

    def _reserved_surface_rect(self,origin,target=None):
        target=self._expansion_rect(origin) if target is None else target
        bounds=origin.united(target).adjusted(-16,-12,16,12)
        step=Fraction(self.devicePixelRatioF()).limit_denominator(8).denominator
        x=origin.x()+math.floor((bounds.x()-origin.x())/step)*step
        y=origin.y()+math.floor((bounds.y()-origin.y())/step)*step
        return QRect(x,y,bounds.right()-x+1,bounds.bottom()-y+1)

    def _prepare_morph_frame(self,origin,target):
        snapshot=getattr(self,'_surface_snapshot',None)
        if snapshot is None:
            snapshot=self._surface_snapshot=SurfaceSnapshot(self)
        controls=(self.pin_button,self.expand_button,self.menu_button,self.close_button)
        source=getattr(self,'_morph_from_image',None)
        if source is None: source=snapshot.capture_current(self.card,controls)
        self._morph_from_image=None
        self._morph_host_active=True
        # A single compositor owns both the moving and resting surface. Align
        # the host rebase to its physical pixel phase and keep it at completion.
        required=origin.united(target).adjusted(-16,-12,16,12)
        if not self.geometry().contains(required):
            self.setGeometry(self._reserved_surface_rect(origin,target))
        # The native host AND live widget tree stay at fixed endpoint bounds.
        # Only the compositor's rectangle moves. Moving the large effect source
        # on every tick invalidates Qt's backing store and causes resize stalls.
        self.card.setGeometry(target.x()-self.x()+14,target.y()-self.y()+7,target.width()-28,target.height()-27)
        self._layout_header(self.card.width())
        target_image=snapshot.capture_live(self.card,controls)
        snapshot.begin(source,target_image,origin,present=False,target_rect=target)
        self.surface_effect.begin_surface_morph(snapshot)
        self._apply_geometry(origin)

    def _settle_morph(self,rect):
        snapshot=getattr(self,'_surface_snapshot',None)
        self.surface_effect.end_surface_morph()
        if snapshot is not None: snapshot.finish()
        self._morph_from_image=None
        self._morph_host_active=False
        self._transition_preparing=False
        if not self.geometry().contains(rect):self.setGeometry(rect)
        self.card.setGeometry(rect.x()-self.x()+14,rect.y()-self.y()+7,rect.width()-28,rect.height()-27)
        self._layout_header(self.card.width())
        if self.expanded and self.popup:
            # Remote metadata or a new drop may have completed while the live
            # tree was hidden behind its transition image.
            self.popup.rebuild()
        self.card.show()
        if self.expanded and self.popup:
            QTimer.singleShot(16,self.popup.resume_content)
        if self._pending_image_paths: self._image_timer.start(16)

    def content_transition_active(self):
        return (self._transition_preparing or self.geometry_morph.active
                or self._morph_host_active or self.visible_state=='hiding')

    def _image_ready(self,path):
        self._pending_image_paths.add(path)
        if not self.content_transition_active() and not self._image_timer.isActive():
            self._image_timer.start(16)

    def _flush_images(self):
        if self.content_transition_active(): return
        ready=self._pending_image_paths;self._pending_image_paths=set()
        refs=[ref for ref in self.files['permanent'] if ref in ready or
              self.content.metadata.get(ref,{}).get('preview_image') in ready]
        if not refs:return
        self.zones['permanent'].refresh()
        if self.popup and self.popup.isVisible():
            for ref in refs:self.popup.update_item_icon(ref)
            if getattr(self.popup.preview,'_group_refs',()):
                chosen=self.popup.preview._group_refs
                self.popup.preview._group_refs=()
                self.popup.preview.show_group(chosen)

    def _apply_geometry(self,rect):
        snapshot=getattr(self,'_surface_snapshot',None)
        if self._morph_host_active and snapshot is not None and snapshot.active:
            # The live tree stays at its endpoint size under one compositor.
            snapshot.set_frame(rect,self.geometry_morph.progress)
            self._place_morph_controls(snapshot,rect)
            return
        if not self.geometry().contains(rect):self.setGeometry(rect)
        self.card.setGeometry(rect.x()-self.x()+14,rect.y()-self.y()+7,rect.width()-28,rect.height()-27)
        self._layout_header(self.card.width())

    def _place_morph_controls(self,snapshot,rect):
        transform=snapshot.presentation_transform(rect)
        for button,offset in ((self.pin_button,176),(self.expand_button,132),(self.menu_button,88),(self.close_button,44)):
            center=transform.map(QRectF(rect.x()+rect.width()-14-offset-self.x(),rect.y()+21-self.y(),36,36).center())
            button.move(round(center.x()-18),round(center.y()-18))
            button.set_presentation_scale(self.surface_effect.sx,self.surface_effect.sy)
            button.raise_()

    def _capture_morph_source(self):
        if not self.geometry_morph.motion: return
        snapshot=getattr(self,'_surface_snapshot',None)
        if snapshot is None: snapshot=self._surface_snapshot=SurfaceSnapshot(self)
        self._morph_from_image=snapshot.capture_current(self.card,(self.pin_button,self.expand_button,self.menu_button,self.close_button))

    def visual_geometry(self): return QRect(self.geometry_morph.current)

    def item_pixmap(self,ref,size=90,stack=1,dpr=None):
        image=self.preview_cache.get(ref)
        if image is not None and not image.isNull():
            ratio=dpr or self.devicePixelRatioF()
            pm=QPixmap(round(size*ratio),round(size*ratio)); pm.setDevicePixelRatio(ratio); pm.fill(Qt.GlobalColor.transparent)
            painter=QPainter(pm); painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            scale=min(size*.84/image.width(),size*.84/image.height()); w,h=image.width()*scale,image.height()*scale
            painter.drawImage(QRectF((size-w)/2,(size-h)/2,w,h),image); painter.end()
            return pm
        return self.content.item_pixmap(ref,size,stack,dpr=dpr)

    def _resume_pending_content(self):
        changed = False
        for ref in tuple(self.files['permanent']):
            meta = self.content.metadata.get(ref, {})
            if not meta.get('loading') or ref in self.resolver.pending_refs:
                continue
            source = meta.get('source_url', '')
            if isinstance(source, str) and source:
                self.resolver.resolve(ref, source)
            else:
                # Older interrupted captures did not keep their data URI.
                # Leave a usable item rather than an endless loading state.
                meta.update(loading=False, error='上次解析未完成，请重新拖入此内容')
                changed = True
        if changed:
            self.refresh()
            self.manager.save()

    def _resolved_content(self,ref,result):
        if ref not in self.files['permanent']: return
        result=dict(result); result['loading']=False
        if str(result.get('source_url','')).startswith('data:'):
            original=result.pop('source_url')
            result.pop('final_url',None)
            self.content.metadata.get(ref,{}).pop('source_url',None)
            self.content.metadata.get(ref,{}).pop('final_url',None)
            if result.get('error'): self.content.texts[ref]=original
        path=result.get('path')
        if path and Path(path).exists():
            refs=self.files['permanent']; index=refs.index(ref)
            if path in refs: refs.remove(ref)
            else: refs[index]=path
            self.content.metadata[path]=result
            if self.popup:
                if ref in self.popup.selected: self.popup.selected.remove(ref); self.popup.selected.add(path)
                if self.popup.primary_selection==ref: self.popup.primary_selection=path
        else: self.content.metadata.setdefault(ref,{}).update(result)
        self.refresh(); self.manager.save(); self.manager.update_handles()

    def confirm_clear_all(self):
        count=len(self.files['permanent'])
        if not count or confirmation_active(self): return False
        self.geometry_morph.complete()
        dialog=ClearConfirmOverlay(self.card,count,font_family=FONT_FAMILY,theme=self.manager.settings.theme)
        self._clear_confirmation=dialog
        self.touch()
        try: confirmed=dialog.exec()
        finally:self._clear_confirmation=None;dialog.deleteLater();self.touch()
        if confirmed: self.remove_all('permanent')
        return confirmed

    def _apply_presentation(self, values):
        self.surface_effect.set_motion(sx=values["sx"], sy=values["sy"], dx=values["dx"], dy=values["dy"])
        snapshot=getattr(self,'_surface_snapshot',None)
        if snapshot is not None and snapshot.active:
            snapshot.refresh_presentation()
            self._place_morph_controls(snapshot,self.visual_geometry())
        elif hasattr(self,'pin_button'):self._layout_header(self.card.width())
        confirmation=getattr(self,'_clear_confirmation',None)
        if confirmation is not None and confirmation.isVisible():confirmation.raise_()
        self.setWindowOpacity(max(0.0, min(1.0, values["alpha"])))

    def toggle_contents(self):
        if self.property('flickdockConfirmationActive'): return
        if self.expanded:
            self.collapse_contents()
        else:
            mode = self.active_mode if self.files[self.active_mode] else next((m for m in MODES if self.files[m]), self.active_mode)
            self.show_contents(mode)

    def touch(self):
        self.last_activity = time.monotonic()

    def has_files(self) -> bool:
        return any(self.files[m] for m in MODES)

    def refresh(self):
        for mode in MODES:
            self.files[mode] = [p for p in self.files[mode] if self.content.exists(p)]
            self.zones[mode].refresh()
        count = sum(len(self.files[m]) for m in MODES)
        self.status_label.setText('正在复制内容…' if self.copy_jobs else '')
        self.expand_button.setEnabled(count > 0)
        if self.popup and self.popup.isVisible():
            if not self.content_transition_active():self.popup.rebuild()
        self.update()

    def add_paths(self, mode, refs):
        for ref in refs:
            value = ref if self.content.is_text(ref) else str(Path(ref).resolve())
            if self.content.exists(value):
                if value in self.files[mode]: self.files[mode].remove(value)
                self.files[mode].append(value)
        self.refresh()
        self.touch()
        self.manager.save()
        self.manager.update_handles()

    def import_paths(self,mode,refs,on_committed=None,on_failed=None):
        """Apply the intake policy only to external file imports, never ZIPs."""
        refs=list(refs)
        if self.manager.settings.capture_mode!='copy':
            self.add_paths(mode,refs)
            if on_committed:on_committed()
            return
        directory=self.manager.settings.capture_dir
        owned=os.path.normcase(os.path.abspath(directory))
        external=[]
        for ref in refs:
            if self.content.is_text(ref):continue
            key=os.path.normcase(os.path.abspath(ref))
            if key!=owned and not key.startswith(owned+os.sep):external.append(ref)
        if not external:
            self.add_paths(mode,refs)
            if on_committed:on_committed()
            return
        job=CopyFilesJob(external,directory,parent=self)
        epoch=self._copy_epoch
        self.copy_jobs.add(job)
        def done(paths=None,error=''):
            self.copy_jobs.discard(job)
            if paths and epoch==self._copy_epoch and not self.manager._quitting:
                replacement=dict(zip(external,paths))
                for old,new in replacement.items():
                    self.content.metadata[new]={'capture_mode':'copy','original_name':Path(old).name}
                self.add_paths(mode,[replacement.get(ref,ref) for ref in refs])
                if on_committed:on_committed()
                else:self.zones[mode].pulse('drop')
            elif on_failed and epoch==self._copy_epoch:on_failed()
            if error and self.popup:self.popup.preview.info.setText('复制失败：'+error)
            self.status_label.setText('复制失败：'+error if error else '' if not self.copy_jobs else '正在复制内容…')
            job.deleteLater();self.touch();self.manager.update_handles()
        job.finished.connect(lambda paths:done(paths))
        job.failed.connect(lambda error:done(error=error))
        job.cancelled.connect(done)
        self.status_label.setText('正在复制内容…')
        job.start();self.touch()

    def remove_path(self, mode: str, path: str):
        if self.popup and self.popup.preview.media.path==path:
            self.popup.preview.media.release_source()
            self.popup.preview.current_path=None
        self.preview_cache.pop(path,None)
        self.image_cache.forget(path)
        if self.popup and self.popup.preview.current_path==path:
            self.popup.preview.image.clear_source()
            self.popup.preview.current_path=None
            self.popup.preview._pending_preview=False
        if self.archive_job:
            related=path in self._archive_refs
            if not related and not self.content.is_text(path):
                removed=Path(path).resolve()
                for source in self.archive_job.paths:
                    source=Path(source).resolve()
                    if source==removed or source in removed.parents or removed in source.parents:
                        related=True;break
            if related:
                self._archive_epoch+=1;self.archive_job.cancel()
        self.resolver.cancel(path)
        if path in self.files[mode]:
            self.files[mode].remove(path)
        self.refresh()
        self.touch()
        self.manager.save()
        self.manager.update_handles()

    def remove_all(self, mode: str):
        self._cancel_intake()
        self._copy_epoch+=1
        for job in tuple(self.copy_jobs):job.cancel()
        if self.popup:
            self.popup.preview.media.release_source()
            self.popup.preview.current_path=None
            self.popup.preview._group_refs=()
            self.popup.preview._pending_preview=False
            self.popup.preview.image.clear_source()
        self.preview_cache.clear()
        self.image_cache.clear()
        self._pending_image_paths.clear()
        self._archive_epoch+=1
        if self.archive_job:self.archive_job.cancel()
        self.resolver.cancel_all()
        self.files[mode].clear()
        self.refresh()
        self.touch()
        self.manager.save()
        self.manager.update_handles()

    def receive_mime(self, mode, mime):
        if self.manager.outgoing_drag or self.dragging or confirmation_active(self):
            return False
        remote=self.resolver.candidates(mime)
        jobs=[]
        if remote and not any(url.isLocalFile() for url in mime.urls()) and not mime.hasImage():
            paths=[]
            for url in remote:
                placeholder='正在解析图片 · '+str(time.monotonic_ns()) if url.startswith('data:') else url
                ref=self.content.add_text(placeholder)
                # Persist the original source until resolution completes so an
                # interrupted download can resume after restarting the app.
                self.content.metadata[ref]={'source_url':url, 'loading':True, 'title':'网页图片' if url.startswith('data:') else url}
                paths.append(ref); jobs.append((ref,url))
        else:
            paths = self.content.capture_mime(mime, self.manager.settings.capture_dir)
        if not paths:
            return False
        self._cancel_intake()
        epoch=self._intake_epoch
        self.incoming_drop = True
        self.manager.just_dropped = time.monotonic()
        self.import_paths(mode,paths,
            on_committed=lambda:self._committed_intake(mode,epoch),
            on_failed=lambda:self._abort_intake(epoch))
        for ref,url in jobs: self.resolver.resolve(ref,url)
        return True

    def _cancel_intake(self):
        self._intake_epoch+=1
        self.incoming_drop=False
        self._intake_stage='idle'
        self._intake_timer.stop()
        for zone in self.zones.values():zone.icon.cancel_pulse()
        if self._intake_tile is not None:
            self._intake_tile.cancel_pulse();self._intake_tile.hide()

    def _abort_intake(self,epoch):
        if epoch==self._intake_epoch:self._cancel_intake()

    def _committed_intake(self,mode,epoch):
        """One accepted drop: commit, visible landing, then tuck once."""
        if epoch!=self._intake_epoch or not self.incoming_drop:return
        if not self.isVisible():
            self._show_at(self._dock_target,self.presentation_kind,QPoint())
        self._intake_mode=mode
        if self.expanded and not self.pinned:
            self._intake_stage='folding'
            self._intake_timer.start()
            # This completion belongs to the intake, not the interruptible
            # geometry animation. Reopening the overview cannot lose it.
            self.collapse_contents()
        else:self._land_intake()

    def _advance_intake(self):
        if not self.incoming_drop:
            self._intake_timer.stop();return
        if self._intake_stage=='folding' and not self.geometry_morph.active:
            self._land_intake()
        elif self._intake_stage=='waiting_copy' and not self.copy_jobs:
            self._land_intake()

    def _land_intake(self):
        epoch=self._intake_epoch;mode=self._intake_mode
        if not self.incoming_drop:return
        self._intake_timer.stop();self._intake_stage='landing'
        tile=self.zones[mode].icon
        if self.expanded:
            # A pinned/explicitly reopened overview keeps its selected preview.
            # Landing feedback is a transparent, non-interactive overlay.
            if self._intake_tile is None:self._intake_tile=StackTile(self,motion=self.manager.motion)
            tile=self._intake_tile
            tile.set_stack(self.content,self.files[mode],self.preview_cache)
            visual=self.visual_geometry()
            tile.setGeometry(visual.center().x()-self.x()-80,
                             visual.center().y()-self.y()-75,160,150)
            tile.show();tile.raise_()
        if self.manager.motion:
            tile.replay_landing()
            self.presentation.kick({'sx':-.45,'sy':.65})
        tile.pulse('drop',lambda:self._finish_intake(epoch))

    def accepts_content_drop(self, event):
        if self.manager.outgoing_drag or self.dragging or confirmation_active(self):
            return False
        source = event.source()
        if isinstance(source, QWidget) and (source is self or self.isAncestorOf(source)):
            return False
        return is_content_mime(event.mimeData())

    def _highlight_content_drop(self, active):
        zone = self.zones["permanent"]
        zone.drop_highlight = active
        zone.icon.set_hover(active)
        zone._apply_style()

    def dragEnterEvent(self, event):
        if self.accepts_content_drop(event):
            self.active_mode = "permanent"
            self._highlight_content_drop(True)
            self.touch()
            event.setDropAction(Qt.DropAction.CopyAction)
            event.accept()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        if self.accepts_content_drop(event):
            self.touch()
            event.setDropAction(Qt.DropAction.CopyAction)
            event.accept()
        else:
            event.ignore()

    def dragLeaveEvent(self, event):
        self._highlight_content_drop(False)
        event.accept()

    def dropEvent(self, event):
        self._highlight_content_drop(False)
        if self.accepts_content_drop(event) and self.receive_mime("permanent", event.mimeData()):
            event.setDropAction(Qt.DropAction.CopyAction)
            event.accept()
        else:
            event.ignore()

    def _finish_intake(self,epoch=None):
        if not self.incoming_drop or (epoch is not None and epoch!=self._intake_epoch):
            return
        if self._intake_tile is not None:self._intake_tile.hide()
        if self.copy_jobs:
            # A later, faster copy must not tuck the shelf while an earlier
            # accepted import is still running in the background.
            self._intake_stage='waiting_copy';self._intake_timer.start();return
        self.incoming_drop = False
        self._intake_stage='idle';self._intake_timer.stop()
        # Completing a drop commits the tuck just like the hide control. The
        # pointer is still over the drop zone and must leave before reopening.
        if not self.pinned: self.hide_animated(explicit=True)

    def drag_files(self, mode, paths):
        paths = [p for p in paths if self.content.exists(p)]
        if not paths:
            return Qt.DropAction.IgnoreAction
        self.manager.drag_gate.reset_session()
        self.manager.outgoing_drag = True
        self.dragging = True
        self.touch()
        drag = QDrag(self)
        self.active_drag = drag
        mime = self.content.mime_for(paths)

        drag.setMimeData(mime)
        artwork=drag_pixmap(self.content,paths,preview_cache=self.preview_cache,dpr=self.devicePixelRatioF())
        drag.setPixmap(artwork)
        drag.setHotSpot(QPoint(round(artwork.deviceIndependentSize().width()/2),46))
        self.zones[mode].pulse("lift")
        result = Qt.DropAction.IgnoreAction
        try:
            result = drag.exec(Qt.DropAction.CopyAction, Qt.DropAction.CopyAction)
        finally:
            # QDrag owns native drag visuals. Dispose them on every exit,
            # including Escape/cancel and exceptions, not at app shutdown.
            drag.setPixmap(QPixmap())
            self.active_drag = None
            drag.deleteLater()
            self.dragging = False
            self.manager.outgoing_drag = False
            self.touch()
        self.complete_drag(mode, paths, result)
        return result

    def complete_drag(self, mode, paths, result):
        # A drag exports content; removal is always an explicit user action.
        self.zones["permanent"].pulse("return" if result == Qt.DropAction.IgnoreAction else "out")
        if self.popup:
            self.popup.rebuild()
        self.touch()

    def _show_at(self, target, kind, offset, reserve=None):
        was_visible = self.isVisible()
        self.manager.activate_shelf(self)
        self.presentation_kind = kind
        self.visible_state = "floating"
        self._explicit_hiding = False
        self.opened_at = time.monotonic()
        self._dock_target = QPoint(target)
        width, height = getattr(self, "_expanded_size", (EXPANDED_W, EXPANDED_H)) if self.expanded else (CARD_W, CARD_H)
        rect = QRect(target.x(), target.y(), width + 28, height + 27)
        if not was_visible:
            # Reserve the small and expanded envelopes before the native
            # surface becomes visible. Expansion then never moves its HWND
            # while Windows still holds the old compact backing-store image.
            self.setGeometry(self._reserved_surface_rect(rect,reserve))
        if was_visible and self.geometry_morph.current != rect:
            self.geometry_morph.animate(rect)
        else:
            self.geometry_morph.jump(rect)
        if not was_visible:
            self.presentation.jump({"sx": .95 if self.manager.motion else 1, "sy": .91 if self.manager.motion else 1,
                                    "alpha": 0, "dx": offset.x() if self.manager.motion else 0, "dy": offset.y() if self.manager.motion else 0})
        # A visible surface may still be morphing from a different width.
        # Keep controls on its current bounds until each geometry frame lands.
        if self._morph_host_active:
            self._apply_geometry(self.visual_geometry())
        else:
            self._layout_header(self.card.width())
        self.show()
        self.raise_()
        self.touch()
        self.presentation.animate({"sx": 1, "sy": 1, "alpha": 1, "dx": 0, "dy": 0}, response=.24, damping=.72)
        self.manager.update_handles()

    def show_transient(self, cursor=None, reason="manual", velocity=None, reserve=None):
        cursor = cursor or QCursor.pos()
        screen = QGuiApplication.screenAt(cursor) or QGuiApplication.primaryScreen()
        area = screen.availableGeometry()
        width,height=(self.visual_geometry().width(),self.visual_geometry().height()) if self.expanded else (CARD_W+28,CARD_H+27)
        x = max(area.left() + 4, min(cursor.x() + 22, area.right() - width - 4))
        y = max(area.top() + 4, min(cursor.y() - 130, area.bottom() - height - 4))
        offset=QPoint(0,14)
        if velocity is not None:
            vx,vy=velocity
            offset=QPoint(round(max(-24,min(24,-vx*.012))),round(max(-18,min(18,-vy*.012))))
        self._show_at(QPoint(x,y),reason,offset,reserve=reserve)
        if velocity is not None and self.manager.motion:
            # The newly revealed shelf settles against the actual flick,
            # with a small elastic impulse, rather than a generic fade alone.
            self.presentation.kick({'sx':.55,'sy':-.7})

    def reveal_from_dock(self):
        if self.manager.edge_suppressed: return
        area=self.manager.edge.screen_area()
        size=QSize(*(getattr(self,'_expanded_size',(EXPANDED_W,EXPANDED_H)) if self.expanded else (CARD_W,CARD_H)))+QSize(28,27)
        rect=reveal_rect(area,self.manager.edge.geometry(),size,self.manager.settings.dock_edge)
        offsets={'right':QPoint(30,0),'left':QPoint(-30,0),'top':QPoint(0,-24),'bottom':QPoint(0,24)}
        self._show_at(rect.topLeft(),'dock',offsets[self.manager.settings.dock_edge])

    def hide_animated(self, explicit=True):
        if not self.isVisible() or self.visible_state == "hiding" or (self.pinned and not explicit):
            return
        self._cancel_intake()
        self._explicit_hiding = explicit
        if explicit:
            self.manager.edge_suppressed = True
            self.manager.suppressed_region = QRect(self.manager.edge.geometry())
        self.geometry_morph.stop()
        if self.popup: self.popup.preview.media.stop()
        self.visible_state = "hiding"
        self.manager.update_handles()
        docked = self.presentation_kind == "dock"
        if self.manager.motion:
            self.presentation.kick({"sx": .35, "sy": -.55})
        def finish():
            if self.visible_state == "hiding":
                self.hide_immediately()
                self.manager.update_handles()
                self.manager.save()
        offset={'right':QPoint(32,0),'left':QPoint(-32,0),'top':QPoint(0,-24),'bottom':QPoint(0,24)}[self.manager.settings.dock_edge] if docked else QPoint(0,8)
        self.presentation.animate({"dx": offset.x() if self.manager.motion else 0,
                                   "dy": offset.y() if self.manager.motion else 0,
                                   "sx": .96 if self.manager.motion else 1, "sy": .92 if self.manager.motion else 1, "alpha": 0},
                                  finish, response=.17, damping=.84)

    def hide_immediately(self):
        self._cancel_intake()
        self.presentation.stop()
        self.geometry_morph.stop()
        self.hide()
        self.visible_state = "hidden"
        self.incoming_drop = False
        self.expanded = False
        self.expand_button.set_glyph("expand")
        self.status_label.show()
        self.zones["permanent"].show()
        if self.popup:
            self.popup.hide()
        self.geometry_morph.jump(QRect(self.x(), self.y(), CARD_W + 28, CARD_H + 27))
        self.presentation.jump({"sx": 1, "sy": 1, "alpha": 1, "dx": 0, "dy": 0})

    def hover_region_contains(self,point):
        if not self.isVisible(): return False
        rect=self.visual_geometry()
        if self.presentation_kind=='dock': rect=dock_hover_rect(rect,self.manager.edge.geometry(),self.manager.settings.dock_edge)
        return rect.contains(point)

    def show_contents(self, mode="permanent", target=None):
        with self._surface_transaction():
            self._show_contents(mode,target)

    def _show_contents(self,mode,target):
        if self.property('flickdockConfirmationActive'): return
        self._capture_morph_source()
        self._transition_preparing=True
        self.visible_state = "floating"
        if self.popup is None:
            self.popup = ContentsPopup(self)
        else:
            self.popup.category='all'
            self.popup.category_buttons['all'].setChecked(True)
            self.popup.rebuild()
        self.popup.hovered_ref=None
        self.popup.restore_selected_preview()
        self.expanded = True
        self.zones["permanent"].hide()
        self.status_label.hide()
        self.expand_button.set_glyph("collapse")
        current=self.visual_geometry()
        default_target=self._expansion_rect(current)
        width,height=default_target.width()-28,default_target.height()-27
        self._expanded_size = (width, height)
        self.popup.setGeometry(16, 65, width - 32, height - 81)
        self.popup.show()
        destination=default_target if target is None else QRect(target)
        self.geometry_morph.animate(destination)
        self.presentation.animate({"sx": 1, "sy": 1, "alpha": 1, "dx": 0, "dy": 0}, response=.19, damping=1)
        self.touch()

    def collapse_contents(self, target=None, finished=None):
        with self._surface_transaction():
            self._collapse_contents(target,finished)

    def _collapse_contents(self,target,finished):
        if self.property('flickdockConfirmationActive'): return
        if not self.expanded:
            return
        self._capture_morph_source()
        self._transition_preparing=True
        self.expanded = False
        if self.popup:
            self.popup.hide()
        self.zones["permanent"].show()
        self.status_label.show()
        self.expand_button.set_glyph("expand")
        right = self.geometry_morph.current.x() + self.geometry_morph.current.width()
        x = right - CARD_W - 28
        self._dock_target.setX(x)
        destination=QRect(x,self.visual_geometry().y(),CARD_W+28,CARD_H+27) if target is None else QRect(target)
        self.geometry_morph.animate(destination,finished=finished)
        self.touch()

    def pick_files(self, mode: str):
        paths, _ = QFileDialog.getOpenFileNames(self, "选择文件")
        if paths:
            self.import_paths(mode, paths)
            self.zones[mode].pulse("drop")

    def paste_clipboard(self, mode):
        self.receive_mime(mode,QApplication.clipboard().mimeData())

    def copy_items(self, refs):
        if not refs:
            return
        QApplication.clipboard().setMimeData(self.content.mime_for(refs))
        self.touch()

    def real_paths(self, mode):
        return [ref for ref in self.files[mode] if not self.content.is_text(ref) and self.content.exists(ref)]

    def show_menu(self, mode=None, pos=None):
        if confirmation_active(self):return
        self.touch()
        menu = AnimatedMenu(self)
        style_menu(menu)
        self._menu_open = True
        self._add_mode_actions(menu, "permanent")
        menu.addSeparator()
        menu.addAction("设置…", self.manager.show_settings)
        menu.addAction("收起到屏幕边缘", lambda: self.hide_animated(explicit=True))
        try:
            menu.exec(pos or self.menu_button.mapToGlobal(QPoint(0, 39)))
        finally:
            self._menu_open = False
            menu.deleteLater()
            self.touch()

    def _add_mode_actions(self, menu, mode="permanent"):
        refs = self.files["permanent"]
        menu.addAction("添加文件…", lambda: self.pick_files("permanent"))
        menu.addAction("粘贴剪贴板内容", lambda: self.paste_clipboard("permanent"))
        if not refs:
            return
        menu.addSeparator()
        menu.addAction("内容总览", self.show_contents)
        only_text = all(self.content.is_text(ref) for ref in refs)
        menu.addAction("复制文字" if only_text else "复制内容", lambda: self.copy_files("permanent"))
        menu.addAction('打包全部为 ZIP…',lambda:self.compress_refs(list(self.files['permanent'])))
        menu.addSeparator()
        menu.addAction("清空搁架…", self.confirm_clear_all)

    def open_path(self, ref):
        if self.content.is_text(ref):
            if self.content.category(ref)=='links':
                from PySide6.QtGui import QDesktopServices
                QDesktopServices.openUrl(QUrl(self.content.text(ref))); return
            self.copy_items([ref])
            if self.popup:
                self.popup.preview.copy_button.set_glyph("check")
                QTimer.singleShot(850, lambda: self.popup.preview.copy_button.set_glyph("copy"))
        elif Path(ref).exists():
            from PySide6.QtGui import QDesktopServices
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(ref).resolve())))
        else:
            QMessageBox.warning(self, APP_NAME, f"找不到文件：\n{ref}")

    def reveal(self, mode):
        refs = self.real_paths(mode)
        if refs:
            subprocess.Popen(["explorer.exe", "/select,", refs[0]])

    def reveal_ref(self,ref):
        if not self.content.is_text(ref) and Path(ref).exists():
            subprocess.Popen(['explorer.exe','/select,',ref])

    def copy_files(self, mode):
        self.copy_items(self.files[mode])

    def compress(self, mode):
        refs=self.popup.chosen_refs() if self.expanded and self.popup else []
        self.compress_refs(refs or self.files[mode])

    def choose_zip_path(self):
        directory=self.manager.settings.capture_dir
        try:directory.mkdir(parents=True,exist_ok=True)
        except OSError as error:
            if self.popup:self.popup.preview.info.setText('无法打开保存位置：'+str(error))
            return ''
        dialog=QFileDialog(self,'将所选内容打包为 ZIP',str(directory/'文件打包.zip'))
        # Use Windows' own Save As dialog (Desktop/Quick access, full path,
        # localized system buttons and standard file selection feedback).
        dialog.setOption(QFileDialog.Option.DontConfirmOverwrite,True)
        dialog.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint,True)
        dialog.setAcceptMode(QFileDialog.AcceptMode.AcceptSave)
        dialog.setNameFilter('ZIP 文件 (*.zip)');dialog.setDefaultSuffix('zip')
        dialog.setWindowModality(Qt.WindowModality.WindowModal)
        try:return dialog.selectedFiles()[0] if dialog.exec() and dialog.selectedFiles() else ''
        finally:dialog.deleteLater()

    def compress_refs(self,refs):
        refs=[ref for ref in refs if self.content.exists(ref)]
        if not refs or self.archive_job is not None or self._archive_choosing:return False
        self.geometry_morph.complete()
        self._archive_choosing=True
        if self.popup:
            self.popup.zip_button.setText('选择保存位置…')
            self.popup.zip_button.setEnabled(False)
        try:chosen=self.choose_zip_path()
        finally:
            self._archive_choosing=False
            if self.popup:self.popup.update_selection_actions()
        if not chosen:return False
        paths=[ref for ref in refs if not self.content.is_text(ref)]
        texts=[(self.content.title(ref),self.content.text(ref)) for ref in refs if self.content.is_text(ref)]
        job=self.archive_job=ZipArchiveJob(paths,chosen,texts=texts,parent=self)
        self._archive_refs=tuple(refs)
        self._archive_epoch+=1;epoch=self._archive_epoch
        def done(path='',error=''):
            self.archive_job=None
            self._archive_refs=()
            published=path and not self.manager._quitting and epoch==self._archive_epoch
            if published:
                self.add_paths('permanent',[path])
                if self.popup and self.popup.isVisible():
                    self.popup.set_category('all')
                    self.popup.select_item(path)
                    self.popup.scroll.verticalScrollBar().setValue(0)
                    self.popup.preview.info.setText('已保存 · '+str(path))
            if self.popup:
                self.popup.update_selection_actions()
                if error:self.popup.preview.info.setText('打包失败：'+error)
            job.deleteLater();self.touch()
        job.finished.connect(lambda path:done(path));job.failed.connect(lambda error:done(error=error));job.cancelled.connect(done)
        if self.popup:self.popup.update_selection_actions()
        job.start();self.touch();return True

    def move_files(self, mode, refs=None):
        folder = QFileDialog.getExistingDirectory(self, "选择目标文件夹")
        if not folder:
            return
        try:
            for ref in list(self.files[mode] if refs is None else refs):
                if ref not in self.files[mode]:continue
                if self.content.is_text(ref):
                    continue
                source = Path(ref)
                target = unique_path(Path(folder) / source.name)
                if self.popup and self.popup.preview.media.path==ref:self.popup.preview.media.release_source()
                result=shutil.move(str(source),str(target))
                self.files[mode][self.files[mode].index(ref)]=result
                self.image_cache.forget(ref);self.preview_cache.pop(ref,None)
                if ref in self.content.metadata:self.content.metadata[result]=self.content.metadata.pop(ref)
                if self.popup:
                    if ref in self.popup.selected:self.popup.selected.remove(ref);self.popup.selected.add(result)
                    if self.popup.primary_selection==ref:self.popup.primary_selection=result
                    self.popup.preview.current_path=None
            self.refresh()
            self.manager.save()
        except Exception as ex:
            self.manager.save()
            QMessageBox.warning(self, APP_NAME, f"移动失败：{ex}")

    def start_rename(self, mode: str):
        if len(self.files[mode]) != 1 or self.content.is_text(self.files[mode][0]):
            return
        self.rename_ref(self.files[mode][0])

    def rename_ref(self,ref):
        if ref not in self.files['permanent'] or self.content.is_text(ref):return
        self._rename_mode='permanent';self._rename_ref=ref
        self.geometry_morph.complete()
        if self.expanded and self.popup:
            self.popup.select_item(ref)
            preview=self.popup.preview
            position=preview.mapTo(self.card,QPoint(16,12))
            self.rename_edit.setGeometry(position.x(),position.y(),max(140,preview.width()-68),32)
        else:
            zone=self.zones['permanent']
            self.rename_edit.setGeometry(zone.x()+12,zone.y()+128,zone.width()-24,32)
        self.rename_edit.setText(Path(ref).stem)
        self.rename_edit.show()
        self.rename_edit.raise_()
        self.rename_edit.setFocus()
        self.rename_edit.selectAll()
        self.touch()

    def commit_rename(self):
        ref=getattr(self,'_rename_ref',None)
        if not self.rename_edit.isVisible() or ref not in self.files['permanent']:
            return
        new_name = self.rename_edit.text().strip()
        self.rename_edit.hide()
        if not new_name or any(c in new_name for c in '<>:"/\\|?*'):
            return
        src = Path(ref)
        target = src.with_name(new_name + src.suffix)
        if target == src:
            return
        if target.exists():
            QMessageBox.warning(self, APP_NAME, "目标文件名已经存在")
            return
        try:
            if self.popup and self.popup.preview.media.path==ref:self.popup.preview.media.release_source()
            src.rename(target)
            self.files['permanent']=[str(target) if old==ref else old for old in self.files['permanent']]
            if ref in self.content.metadata:self.content.metadata[str(target)]=self.content.metadata.pop(ref)
            self.image_cache.forget(ref);self.preview_cache.pop(ref,None)
            if self.popup:
                if ref in self.popup.selected:self.popup.selected.remove(ref);self.popup.selected.add(str(target))
                if self.popup.primary_selection==ref:self.popup.primary_selection=str(target)
                self.popup.preview.current_path=None
            self.refresh()
            self.manager.save()
        except Exception as ex:
            QMessageBox.warning(self, APP_NAME, f"重命名失败：{ex}")

    def mousePressEvent(self, event):
        self.touch()
        local=self.card.mapFrom(self,event.position().toPoint())
        if event.button() == Qt.MouseButton.LeftButton and self.card.rect().contains(local) and local.y()<60:
            self.geometry_morph.stop()
            self.drag_origin = event.globalPosition().toPoint() - self.visual_geometry().topLeft()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        self.touch()
        if self.drag_origin and event.buttons() & Qt.MouseButton.LeftButton:
            target = event.globalPosition().toPoint() - self.drag_origin
            self.geometry_morph.jump(QRect(target, self.visual_geometry().size()))
            self.presentation_kind = "manual"
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self.drag_origin:
            self.drag_origin = None
            self.manager.save()
        self.touch()
        super().mouseReleaseEvent(event)

    def enterEvent(self, event):
        self.touch()
        super().enterEvent(event)

    def contextMenuEvent(self, event):
        self.show_menu(pos=event.globalPos())
        event.accept()

    def keyPressEvent(self, event):
        if confirmation_active(self):event.accept();return
        if event.key()==Qt.Key.Key_A and event.modifiers() & Qt.KeyboardModifier.ControlModifier and self.expanded and self.popup:
            refs=self.popup.visible_refs();self.popup.selected=set(refs)
            if refs:self.popup.select_item(refs[0])
            event.accept();return
        if event.key() == Qt.Key.Key_Escape:
            self.hide_animated(explicit=True)
            event.accept()
            return
        if event.key() == Qt.Key.Key_V and event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self.paste_clipboard(self.active_mode)
            event.accept()
            return
        if event.key() == Qt.Key.Key_C and event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            if self.expanded and self.popup:
                refs = [ref for ref in self.files[self.popup.mode] if ref in self.popup.selected]
                if not refs:
                    current = self.popup.preview.current_path
                    refs = [current] if current in self.files[self.popup.mode] else self.files[self.popup.mode]
            else:
                refs = self.files[self.active_mode]
            self.copy_items(refs)
            event.accept()
            return
        super().keyPressEvent(event)


class DockHandle(QWidget):
    """One fully hittable activation strip, also monitored by the manager."""
    def __init__(self,manager):
        super().__init__()
        self.manager=manager
        self.setWindowTitle('FlickDock edge')
        self.setWindowFlags(Qt.WindowType.Tool|Qt.WindowType.FramelessWindowHint|Qt.WindowType.WindowStaysOnTopHint|Qt.WindowType.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAcceptDrops(True); self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.appearance=Spring(self,{'alpha':0},lambda _:self.update(),motion=manager.motion)
        self.position_at_edge()

    def screen_area(self): return QGuiApplication.primaryScreen().availableGeometry()

    def position_at_edge(self):
        values=self.manager.dock_values()
        self.setGeometry(trigger_rect(self.screen_area(),values['dock_edge'],values['dock_position'],values['dock_length'],values['dock_thickness']))

    def show_handle(self):
        self.position_at_edge()
        if not self.isVisible():
            self.appearance.jump({'alpha':0}); self.show(); self.raise_(); self.repaint()
            self.appearance.animate({'alpha':1},response=.18,damping=1)

    def paintEvent(self,event):
        painter=QPainter(self)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Source)
        painter.fillRect(self.rect(),QColor(0,0,0,1))
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        values=self.manager.dock_values()
        if values['trigger_visible']:
            tint=QColor(values['dock_color']);tint.setAlpha(round(255*values['dock_opacity']/100*self.appearance.values['alpha']))
            painter.setBrush(tint);painter.setPen(Qt.PenStyle.NoPen)
            painter.drawRoundedRect(QRectF(self.rect()).adjusted(.5,.5,-.5,-.5),3,3)
        painter.end()

    def enterEvent(self,event):
        if not self.manager.edge_suppressed: self.manager.show_docked()
        super().enterEvent(event)

    def dragEnterEvent(self,event):
        if self.manager.shelf.accepts_content_drop(event):
            event.setDropAction(Qt.DropAction.CopyAction); event.accept()
            QTimer.singleShot(0,self.manager.show_docked)
        else: event.ignore()

    def dropEvent(self,event):
        if self.manager.shelf.accepts_content_drop(event) and self.manager.shelf.receive_mime('permanent',event.mimeData()):
            event.setDropAction(Qt.DropAction.CopyAction); event.accept()
        else: event.ignore()

class ShelfManager:
    def __init__(self, app):
        self.app = app
        self.motion = motion_enabled()
        self.settings = SettingsStore(BASE)
        self.startup_error = None
        try:
            self.settings.reconcile_startup()
        except OSError:
            self.startup_error = "自启动位置未能更新。可在设置中关闭后重新开启自启动。"
        ui_theme.set_theme(self.settings.theme)
        ui_theme.apply_application(app, FONT_FAMILY)
        self.settings_dialog = None
        self.updates = UpdateController(APP_VERSION, BASE, settings=self.settings,
                                        quit_callback=self.quit, is_busy=self._update_busy)
        self._dock_preview_values=None
        self._shake_preview_value=None
        self._quitting=False
        self.dock_preview=DockPreview()
        self.edge_suppressed = False
        self.suppressed_region = QRect()
        self.shelves = []
        self.active_shelf = None
        self.outgoing_drag = False
        self.just_dropped = 0.0
        self.was_down = False
        self._button_mask=0
        self.shake_detector=ShakeDetector(config_for_sensitivity(self.settings.shake_sensitivity))
        self._drag_probe_detector=ShakeDetector(replace(config_for_sensitivity(100),reversals=1))
        self._drag_content_ready=False
        self.shake_anchor_x = 0
        self.shake_direction = 0
        self.shake_reversals = 0
        self.shake_last_change = 0.0
        self.drag_started_inside = False
        self.auto_shown = None
        self.edge = DockHandle(self)
        self.drag_gate = ContentDragGate(self._content_type_ready,self._probe_drop,lambda:self.outgoing_drag)
        self.tray = QSystemTrayIcon(app_icon(), app)
        self.tray.setToolTip(f"FlickDock {APP_VERSION} · 文件搁架")
        menu = AnimatedMenu()
        style_menu(menu)
        menu.addAction("显示搁架", self.show_first)
        menu.addAction("粘贴到搁架", self.paste_new)
        menu.addAction("设置…", self.show_settings)
        menu.addAction("检查更新…", self.check_updates)
        menu.addSeparator()
        menu.addAction("退出 FlickDock", self.quit)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self.tray_activated)
        self.tray.show()
        self.load()
        if not self.shelves:
            self._create_shelf({})
        if self.active_shelf is None:
            self.active_shelf = next((item for item in self.shelves if item.has_files()), self.shelves[0])
        self.update_handles()
        self.account = AccountService(BASE)
        self.sync = ShelfSync(BASE,self.account,ShelfAdapter(self))
        self._sync_status = ''
        self.sync.status_changed.connect(self._sync_status_changed)
        self.hotkeys = HotkeyManager(app, self.show_shortcut, self.show_overview)
        self.shortcut_error = self.hotkeys.apply(self.settings.shelf_shortcut, self.settings.overview_shortcut)
        self.settings.changed.connect(self.apply_settings)
        self.timer = QTimer(self.app)
        self.timer.timeout.connect(self.poll)
        self.timer.setTimerType(Qt.TimerType.PreciseTimer)
        self.timer.start(20)
        self.app.setQuitOnLastWindowClosed(False)
        if self.app.platformName() not in ('offscreen','minimal'):
            QTimer.singleShot(0,self.account.resume)
        self.update_timer = QTimer(self.app)
        self.update_timer.timeout.connect(self.updates.check_automatically)
        # Development, release verification and offscreen tests never schedule
        # background network traffic or installer execution.
        if getattr(sys, 'frozen', False) and self.app.platformName() not in ('offscreen', 'minimal'):
            self.update_timer.start(6 * 60 * 60 * 1000)
            QTimer.singleShot(4000, self.updates.check_automatically)

    def _update_busy(self):
        return (getattr(self, '_quitting', False) or getattr(self, 'outgoing_drag', False)
                or bool(QApplication.mouseButtons())
                or QApplication.activeModalWidget() is not None
                or QApplication.activePopupWidget() is not None
                or any(s.dragging or s.incoming_drop or confirmation_active(s)
                       for s in getattr(self, 'shelves', ())))

    def check_updates(self):
        self.show_settings()
        self.updates.open(manual=True)

    def _sync_status_changed(self,text):
        self._sync_status = text
        if self.settings_dialog and hasattr(self,'account_panel'):
            self.account_panel.set_sync_status(text)

    def _create_shelf(self, data):
        shelf = ShelfWindow(self, 1, data)
        shelf.handle = self.edge
        self.shelf = self.active_shelf = shelf
        self.shelves = [shelf]

    def activate_shelf(self, shelf):
        self.active_shelf = self.shelf
        self.edge.hide()

    def tray_activated(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self.show_first()

    def show_first(self):
        if hasattr(self,'shelf') and confirmation_active(self.shelf): return
        if not self.shelves:
            self._create_shelf({})
        self.active_shelf.show_transient()

    def show_docked(self):
        if self._dock_preview_values is not None or confirmation_active(self.shelf): return
        if self.active_shelf is None or not self.active_shelf.has_files():
            self.active_shelf = next((s for s in self.shelves if s.has_files()), self.shelves[0])
        self.active_shelf.reveal_from_dock()
        if self.settings.trigger_view=='expanded' and not self.active_shelf.expanded:
            self.active_shelf.show_contents()

    def paste_new(self):
        self.show_first()
        self.active_shelf.paste_clipboard("permanent")


    def show_settings(self):
        self.reset_drag_gesture()
        if self.settings_dialog is None:
            self.settings_dialog = SettingsDialog(self.settings)
            self.settings_dialog.update_requested.connect(self.updates.activate)
            self.settings_dialog.rejected.connect(self.updates.cancel)
            self.updates.state_changed.connect(lambda state,payload:self.settings_dialog.set_update_state(state,**payload))
            self.account_panel = AccountPanel(self.account,self.settings_dialog,theme=self.settings.theme,email_binding=False)
            self.settings_dialog.set_account_panel(self.account_panel)
            self.settings_dialog.preview_changed.connect(self.preview_dock)
            self.settings_dialog.gesture_preview_changed.connect(self.preview_shake)
            self.settings_dialog.shortcut_validator = lambda values: self.hotkeys.validate(values["shelf_shortcut"], values["overview_shortcut"])
        self.settings_dialog.load_values()
        self.settings_dialog.set_update_state(self.updates.state,**self.updates.state_payload)
        self.account_panel.set_sync_status(self._sync_status)
        self.settings_dialog.show()
        problem = self.shortcut_error or self.startup_error
        if problem:
            self.settings_dialog.error_label.setText(problem)
            self.settings_dialog.error_label.show()
        self.settings_dialog.raise_()
        self.settings_dialog.activateWindow()

    def dock_values(self):
        if self._dock_preview_values is not None: return self._dock_preview_values
        return {key:getattr(self.settings,key) for key in ('dock_edge','dock_position','dock_length','dock_thickness','trigger_visible','dock_color','dock_opacity')}

    def preview_dock(self,values):
        self._dock_preview_values=dict(values) if isinstance(values,dict) else None
        self.edge.position_at_edge()
        if self._dock_preview_values is not None:
            self.edge.hide();self.dock_preview.show_preview(self.edge.screen_area(),self._dock_preview_values)
        else:
            self.dock_preview.hide()
            if not self._quitting:self.update_handles()

    def apply_settings(self):
        self.reset_drag_gesture()
        ui_theme.set_theme(self.settings.theme)
        ui_theme.apply_application(self.app, FONT_FAMILY)
        self.shelf.refresh()
        self.edge.position_at_edge()
        self._apply_shake_sensitivity()
        self.update_handles()
        if self.shelf.popup: self.shelf.popup.preview.media.apply_theme()
        if self.settings_dialog:
            self.settings_dialog.apply_theme(self.settings.theme)
            self.account_panel.apply_theme(self.settings.theme)
        self.updates.apply_theme(self.settings.theme)
        self.shortcut_error = self.hotkeys.apply(self.settings.shelf_shortcut, self.settings.overview_shortcut)

    def reset_drag_gesture(self):
        self._drag_content_ready=False
        self.drag_gate.reset_session()
        self.shake_detector.reset()
        self._drag_probe_detector.reset()

    def _apply_shake_sensitivity(self):
        value=self.settings.shake_sensitivity if self._shake_preview_value is None else self._shake_preview_value
        config=config_for_sensitivity(value)
        if self.shake_detector.config!=config:
            self.reset_drag_gesture()
            self.shake_detector=ShakeDetector(config)

    def preview_shake(self,value):
        self._shake_preview_value=value
        self._apply_shake_sensitivity()

    def show_shortcut(self):
        if confirmation_active(self.shelf) or self.shelf._archive_choosing: return
        self.reset_drag_gesture()
        self.edge_suppressed = False
        if self.shelf.expanded:
            cursor=QCursor.pos()
            area=(QGuiApplication.screenAt(cursor) or QGuiApplication.primaryScreen()).availableGeometry()
            x=max(area.left()+4,min(cursor.x()+22,area.right()-CARD_W-32))
            y=max(area.top()+4,min(cursor.y()-130,area.bottom()-CARD_H-31))
            self.shelf.presentation_kind='shortcut'
            self.shelf.visible_state='floating'
            self.shelf.collapse_contents(target=QRect(x,y,CARD_W+28,CARD_H+27))
        else:self.shelf.show_transient(QCursor.pos(), reason="shortcut")
        if not self._pointer_dragging():
            self.shelf.activateWindow()
            self.shelf.setFocus()

    def _pointer_dragging(self):
        return bool(mouse_buttons_mask() & 1)

    def show_overview(self):
        if confirmation_active(self.shelf) or self.shelf._archive_choosing: return
        self.reset_drag_gesture()
        self.edge_suppressed = False
        cursor = QCursor.pos()
        area = (QGuiApplication.screenAt(cursor) or QGuiApplication.primaryScreen()).availableGeometry()
        width,height=min(EXPANDED_W,area.width()-38),min(EXPANDED_H,area.height()-38)
        x = cursor.x() + 18
        if x + width + 28 > area.right():
            x = cursor.x() - width - 46
        x = max(area.left() + 4, min(x, area.right() - width - 30))
        y = max(area.top() + 4, min(cursor.y() - 55, area.bottom() - height - 30))
        if not self.shelf.isVisible():
            self.shelf.show_transient(cursor,reason='overview',reserve=QRect(x,y,width+28,height+27))
        else:
            self.shelf.presentation_kind='overview'
            self.shelf.visible_state='floating'
        # Plan the destination before starting; a jump here used to cancel the
        # very transition that show_contents had just started.
        self.shelf.show_contents(target=QRect(x,y,width+28,height+27))
        if not self._pointer_dragging():
            self.shelf.activateWindow()
            self.shelf.setFocus()

    def load(self):
        self._legacy_source = None
        source = STATE_FILE if STATE_FILE.exists() else OLD_STATE_FILE
        try:
            data = json.loads(source.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return
            if int(data.get("version", 0)) < 6:
                self._legacy_source = source
            merged = merge_shelves(data.get("shelf", data))
            retained, seen = [], set()
            for ref in merged.get("permanent", []) + merged.get("temporary", []):
                key = ref if ContentRepository.is_text(ref) else os.path.normcase(ref)
                if key not in seen:
                    seen.add(key)
                    retained.append(ref)
            merged["permanent"] = retained
            merged.pop("temporary", None)
            current=data.get("shelf",data)
            merged["metadata"]=current.get("metadata",{}) if isinstance(current,dict) else {}
            merged["pinned"]=current.get("pinned",False) if isinstance(current,dict) else False
            self._create_shelf(merged)
        except (FileNotFoundError, json.JSONDecodeError, OSError, TypeError, ValueError):
            pass

    def save(self):
        BASE.mkdir(parents=True, exist_ok=True)
        if self._legacy_source is not None and self._legacy_source.exists():
            backup = BASE / "shelves-before-v6.json"
            if not backup.exists():
                shutil.copy2(self._legacy_source, backup)
            self._legacy_source = None
        refs = self.shelf.files["permanent"]
        self.shelf.content.prune(refs)
        item = {"permanent": self.shelf.files["permanent"],
                "texts": self.shelf.content.export_texts(refs), "metadata": self.shelf.content.export_metadata(refs), "pinned": self.shelf.pinned, "pos": [self.shelf.visual_geometry().x(), self.shelf.visual_geometry().y()]}
        data = {"version": 6, "shelf": item}
        temp = STATE_FILE.with_suffix(".tmp")
        temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(STATE_FILE)
        if hasattr(self,'sync'): self.sync.local_changed()

    def update_handles(self):
        if self._dock_preview_values is not None:
            self.edge.hide();return
        if (self.settings.keep_empty_handle or any(s.has_files() for s in self.shelves)) and not any(s.isVisible() and s.visible_state != "hiding" for s in self.shelves):
            self.edge.show_handle()
        else:
            self.edge.hide()

    def _content_drag_validated(self, position):
        if self.outgoing_drag or self.auto_shown is not None or self.shelf.isVisible():
            return
        shelf = self.active_shelf or self.shelves[0]
        shelf.show_transient(position,reason='drag',velocity=self.shake_detector.trigger_velocity)
        if self.settings.trigger_view=='expanded':shelf.show_contents()
        self.auto_shown = shelf

    def _content_type_ready(self,position):
        if self.outgoing_drag or self.shelf.isVisible() or not self.was_down:return
        # OLE confirmation is a separate, potentially delayed operation. Count
        # the user's configured gesture after it, rather than letting delayed
        # MIME delivery make both sensitivity endpoints pop at the same time.
        self._drag_content_ready=True
        self.shake_detector.reset()

    def _probe_drop(self, mime, position):
        if self.auto_shown is None:return False
        return bool(self.auto_shown and self.auto_shown.receive_mime("permanent", mime))

    def poll(self):
        now=time.monotonic(); pos=QCursor.pos()
        native=native_input_available()
        mask=mouse_buttons_mask()
        down=bool(mask&1); new_press=bool(mask&~self._button_mask); self._button_mask=mask
        if self.edge_suppressed and not self.edge.geometry().contains(pos): self.edge_suppressed=False
        if self.edge.isVisible() and self.edge.geometry().contains(pos) and not self.edge_suppressed and not self.shelf.isVisible(): self.show_docked()
        for shelf in self.shelves:
            if not shelf.isVisible(): continue
            modal=QApplication.activeModalWidget(); popup=QApplication.activePopupWidget()
            blocked=shelf._menu_open or shelf.rename_edit.isVisible() or modal is not None or popup is not None or confirmation_active(shelf)
            press_blocked=modal is not None or shelf.rename_edit.isVisible() or (popup is not None and popup.frameGeometry().contains(pos)) or confirmation_active(shelf)
            in_settings=self.settings_dialog is not None and self.settings_dialog.isVisible() and self.settings_dialog.frameGeometry().contains(pos)
            inside=shelf.hover_region_contains(pos)
            if new_press and not inside and not in_settings and not press_blocked and not shelf.pinned and not self.outgoing_drag:
                shelf.hide_animated(explicit=True); continue
            if shelf.dragging or shelf.incoming_drop or shelf.pinned: continue
            if shelf.presentation_kind=='dock' and not blocked and not self.outgoing_drag:
                if not inside and shelf.visible_state!='hiding': shelf.hide_animated(explicit=False)
                elif inside and shelf.visible_state=='hiding' and not shelf._explicit_hiding and not self.edge_suppressed: shelf.reveal_from_dock()
            elif shelf.visible_state=='floating' and not blocked and not down and not shelf.preview_playing:
                if inside or in_settings: shelf.touch()
                elif now-shelf.last_activity>=IDLE_SECONDS: shelf.hide_animated(explicit=False)
        if not native or self.outgoing_drag:
            self.was_down=down; self.reset_drag_gesture(); return
        if down and not self.was_down:
            self.reset_drag_gesture(); self.auto_shown=None
            self.drag_started_inside=any(s.isVisible() and s.visual_geometry().contains(pos) for s in self.shelves)
        # Python 3.12 on Windows uses a coarse GetTickCount clock for monotonic.
        # QPC timestamps avoid quantised dt corrupting velocity/acceleration.
        sample_time=time.perf_counter()
        moving=native_window_move_active()
        blocked=self.outgoing_drag or self.drag_started_inside or self.shelf.isVisible()
        detector=self.shake_detector if self._drag_content_ready else self._drag_probe_detector
        shaken=detector.update(pos.x(),pos.y(),sample_time,button_down=down,window_moving=moving,blocked=blocked)
        if shaken and self.auto_shown is None and not self.shelf.isVisible() and now-self.just_dropped>.4:
            if self._drag_content_ready:self._content_drag_validated(pos)
            else:self.drag_gate.arm(pos)
        if self.drag_gate.active: self.drag_gate.track_position(pos)
        if not down and self.was_down:
            self.reset_drag_gesture()
            if self.auto_shown and not self.auto_shown.incoming_drop: self.auto_shown.hide_animated(explicit=False)
            self.auto_shown=None
        self.was_down=down

    def quit(self):
        self._quitting=True
        self.timer.stop()
        self.update_timer.stop()
        self.updates.close()
        self.sync.close()
        self.account.close()
        self.hotkeys.close()
        if self.settings_dialog:
            self.settings_dialog.close()
        self.drag_gate.close()
        if self.outgoing_drag:
            QDrag.cancel()
        self.save()
        self.tray.hide()
        self.edge.hide()
        self.dock_preview.hide()
        for shelf in self.shelves:
            shelf._copy_epoch+=1
            for job in tuple(shelf.copy_jobs):job.cancel();job.wait(3000)
            if shelf.archive_job:
                shelf.archive_job.cancel();shelf.archive_job.wait(3000)
            shelf.resolver.close()
            shelf.image_cache.close()
            if shelf.popup: shelf.popup.preview.media.release_source()
            shelf.presentation.stop()
            shelf.geometry_morph.stop()
            shelf.hide()
        self.app.quit()

def main():
    if "--verify-cloud" in sys.argv:
        from verify_cloud import run
        return run(sys.argv,load_font)
    if "--verify-update" in sys.argv:
        from offscreen_verify import run_update
        return run_update(sys.argv, load_font)
    if "--verify-media" in sys.argv:
        # Release verification runs without the manager, tray or native hooks.
        # Require the offscreen platform before constructing any QWidget.
        from offscreen_verify import run
        return run(sys.argv, load_font)
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(APP_VERSION)
    load_font(app)
    guard = InstanceGuard(app, str(BASE))
    if not guard.acquire():
        return
    app.setWindowIcon(app_icon())
    manager = ShelfManager(app)
    guard.activated.connect(manager.show_first)
    if "--preview" in sys.argv:
        manager.show_first()
        QTimer.singleShot(650, lambda: (manager.shelves[0].grab().save(str(Path.cwd() / "flickdock-preview.png")), manager.quit()))
    result = app.exec()
    guard.close()
    sys.exit(result)

if __name__ == "__main__":
    raise SystemExit(main())
