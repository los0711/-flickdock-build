"""Persistent user preferences and a nonmodal settings panel for FlickDock.

Loading preferences never changes startup registration. A formal app reconciles
its own login item on startup and verifies explicit saves against the OS state.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
import sys
from uuid import uuid4

from PySide6.QtCore import QByteArray, QEvent, QObject, QRectF, Qt, Signal
from PySide6.QtGui import QKeySequence, QColor, QFont, QPainter, QPen, QGuiApplication, QIcon, QPixmap
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QFileDialog, QFrame,
                               QHBoxLayout, QLabel, QLineEdit, QPushButton,
                               QVBoxLayout, QKeySequenceEdit, QScrollArea, QWidget,
                               QAbstractSpinBox, QPlainTextEdit, QSlider, QSpinBox, QColorDialog)
from PySide6.QtWidgets import QStyle, QStyleOptionButton
from PySide6.QtSvg import QSvgRenderer

from app_version import APP_VERSION
from platform_startup import (StartupManager, CallbackBackend,
                              startup_runtime_allowed as _platform_startup_allowed,
                              startup_command as _platform_startup_command,
                              read_startup_value as _platform_read_startup,
                              write_startup_value as _platform_write_startup)


RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "FlickDock"
SHELF_SHORTCUT = "Ctrl+Alt+Space"
OVERVIEW_SHORTCUT = "Ctrl+Alt+Shift+Space"
DOCK_EDGES = ("right", "left", "top", "bottom")
DOCK_RANGES = {"dock_position": (0, 100), "dock_length": (32, 600), "dock_thickness": (2, 40), "dock_opacity": (0, 100)}
CAPTURE_MODES = ("reference", "copy")
TRIGGER_VIEWS = ("compact", "expanded")
DEFAULT_SHAKE_SENSITIVITY = 50
_NO_STARTUP_CHANGE = object()


def _startup_runtime_allowed():
    return _platform_startup_allowed()


def normalize_shortcut(value) -> str:
    if isinstance(value, QKeySequence):
        sequence = value
    elif isinstance(value, str):
        sequence = QKeySequence.fromString(value.strip(), QKeySequence.SequenceFormat.PortableText)
    else:
        raise ValueError("快捷键格式无效。")
    if sequence.isEmpty() or sequence.count() != 1:
        raise ValueError("请选择一个完整的组合快捷键。")
    key = sequence[0].key()
    if key in (Qt.Key.Key_unknown, Qt.Key.Key_Shift, Qt.Key.Key_Control,
               Qt.Key.Key_Alt, Qt.Key.Key_Meta, Qt.Key.Key_AltGr) or int(key) == 0:
        raise ValueError("快捷键需要包含一个普通按键，例如空格或字母。")
    normalized = sequence.toString(QKeySequence.SequenceFormat.PortableText)
    if not normalized:
        raise ValueError("请选择快捷键。")
    return normalized


def startup_command() -> str:
    return _platform_startup_command()


def _read_startup_value():
    return _platform_read_startup()


def _write_startup_value(value):
    _platform_write_startup(value)


_ORIGINAL_STARTUP_READ = _read_startup_value
_ORIGINAL_STARTUP_WRITE = _write_startup_value


def _startup_manager():
    # Existing isolated tests can still replace the legacy callbacks. Approval
    # callbacks in that adapter are no-ops, so those tests never reach the OS.
    backend = None
    if (_read_startup_value is not _ORIGINAL_STARTUP_READ
            or _write_startup_value is not _ORIGINAL_STARTUP_WRITE):
        backend = CallbackBackend(_read_startup_value, _write_startup_value)
    return StartupManager(backend=backend, runtime_allowed=_startup_runtime_allowed)


def _startup_registration(enabled):
    if not enabled:
        return None
    if sys.platform == "win32":
        import winreg
        return startup_command(), winreg.REG_SZ
    return startup_command(), 1


class SettingsComboBox(QComboBox):
    """A readable theme-aware arrow while retaining native combo semantics."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.dark = False

    def paintEvent(self, event):
        super().paintEvent(event)
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        pen = QPen(QColor("#b9c6d9" if self.dark else "#66778e"), 1.7)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        x, y = self.width() - 16, self.height() // 2
        p.drawLine(x - 4, y - 2, x, y + 2)
        p.drawLine(x, y + 2, x + 4, y - 2)


class SettingsSpinBox(QSpinBox):
    """Theme-aware step arrows without Qt's styled NoButtons text clipping."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.dark = False

    def paintEvent(self, event):
        super().paintEvent(event)
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        pen = QPen(QColor("#b9c6d9" if self.dark else "#66778e"), 1.5)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        x = self.width() - 12
        upper, lower = self.height() // 4 + 1, 3 * self.height() // 4 - 1
        p.drawLine(x - 3, upper + 1, x, upper - 2)
        p.drawLine(x, upper - 2, x + 3, upper + 1)
        p.drawLine(x - 3, lower - 1, x, lower + 2)
        p.drawLine(x, lower + 2, x + 3, lower - 1)


class SettingsCheckBox(QCheckBox):
    """Keep a vector check visible over the styled indicator at every DPI."""
    def indicator_rect(self):
        option = QStyleOptionButton()
        self.initStyleOption(option)
        return self.style().subElementRect(QStyle.SubElement.SE_CheckBoxIndicator, option, self)

    def paintEvent(self, event):
        super().paintEvent(event)
        if self.isChecked():
            painter = QPainter(self)
            if not self.isEnabled(): painter.setOpacity(.65)
            svg = b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><path d="m5 12 5 5L19 7" fill="none" stroke="#ffffff" stroke-width="2.8" stroke-linecap="round" stroke-linejoin="round"/></svg>'
            QSvgRenderer(QByteArray(svg)).render(painter, QRectF(self.indicator_rect()))


class SettingsResetButton(QPushButton):
    """Paint the reset SVG in logical coordinates instead of scaling a bitmap."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.dark = False
        self.setObjectName("settingsReset")
        self.setFixedSize(36, 34)
        self.setAccessibleName("恢复默认摇动灵敏度 50")
        self.setToolTip("恢复默认值 50")

    def paintEvent(self, event):
        super().paintEvent(event)
        ink = "#b9c6d9" if self.dark else "#66778e"
        svg = f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><path d="M4 10a8 8 0 1 1 1 7M4 4v6h6" fill="none" stroke="{ink}" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>'
        painter = QPainter(self)
        QSvgRenderer(QByteArray(svg.encode("utf8"))).render(painter, QRectF((self.width()-17)/2, (self.height()-17)/2, 17, 17))


class SettingsCloseButton(QPushButton):
    """A full-size close target with an exactly centered vector cross."""
    def __init__(self, parent=None, *, glyph_size=18):
        super().__init__(parent)
        self.dark = False
        self.glyph_size = glyph_size
        self.setObjectName("settingsClose")
        self.setFixedSize(40, 40)
        self.setAccessibleName("关闭设置")
        self.setToolTip("关闭设置")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAutoDefault(False)
        self.setDefault(False)

    @property
    def glyph_rect(self):
        size = self.glyph_size
        return QRectF((self.width()-size)/2, (self.height()-size)/2, size, size)

    def paintEvent(self, event):
        super().paintEvent(event)
        ink = "#cbd6e6" if self.dark else "#526174"
        svg = f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 18 18"><path d="m1.5 1.5 15 15m0-15-15 15" fill="none" stroke="{ink}" stroke-width="1.6" stroke-linecap="round"/></svg>'
        painter = QPainter(self)
        if not self.isEnabled(): painter.setOpacity(.45)
        QSvgRenderer(QByteArray(svg.encode("utf8"))).render(painter, self.glyph_rect)


class SettingsTitleBar(QWidget):
    """Move the frameless settings window from its title area."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("settingsTitleBar")
        self.setFixedHeight(40)
        self._drag_offset = None

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_offset = event.globalPosition().toPoint()-self.window().pos()
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._drag_offset is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self.window().move(event.globalPosition().toPoint()-self._drag_offset)
            event.accept()
        else:
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_offset = None
            event.accept()
        else:
            super().mouseReleaseEvent(event)

    def hideEvent(self, event):
        self._drag_offset = None
        super().hideEvent(event)


class SettingsStore(QObject):
    changed = Signal()

    def __init__(self, base: Path, parent: QObject | None = None):
        super().__init__(parent)
        self.base = Path(base).resolve()
        self.path = self.base / "settings.json"
        self._data = {"theme": "light", "capture_dir": str(self.base / "saved"), "autostart": True,
                      "capture_mode": "reference", "keep_empty_handle": False,
                      "trigger_view": "compact", "shake_sensitivity": DEFAULT_SHAKE_SENSITIVITY,
                      "view_mode": "list", "shelf_shortcut": SHELF_SHORTCUT,
                      "overview_shortcut": OVERVIEW_SHORTCUT, "dock_edge": "right",
                      "dock_position": 8, "dock_length": 76, "dock_thickness": 8,
                      "trigger_visible": True, "dock_color": "#567daf", "dock_opacity": 100,
                      "startup_initialized": False}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                if data.get("theme") in ("light", "dark"):
                    self._data["theme"] = data["theme"]
                directory = data.get("capture_dir")
                if isinstance(directory, str) and directory.strip():
                    candidate = Path(os.path.expandvars(directory)).expanduser()
                    if candidate.is_absolute():
                        self._data["capture_dir"] = str(candidate)
                if isinstance(data.get("autostart"), bool):
                    self._data["autostart"] = data["autostart"]
                if data.get("capture_mode") in CAPTURE_MODES:
                    self._data["capture_mode"] = data["capture_mode"]
                if isinstance(data.get("keep_empty_handle"), bool):
                    self._data["keep_empty_handle"] = data["keep_empty_handle"]
                if data.get("trigger_view") in TRIGGER_VIEWS:
                    self._data["trigger_view"] = data["trigger_view"]
                sensitivity = data.get("shake_sensitivity")
                if type(sensitivity) is int and 1 <= sensitivity <= 100:
                    self._data["shake_sensitivity"] = sensitivity
                self._data["startup_initialized"] = bool(data.get("startup_initialized", "autostart" in data))
                if data.get("view_mode") in ("list", "icons"):
                    self._data["view_mode"] = data["view_mode"]
                if data.get("dock_edge") in DOCK_EDGES:
                    self._data["dock_edge"] = data["dock_edge"]
                if isinstance(data.get("trigger_visible"), bool):
                    self._data["trigger_visible"] = data["trigger_visible"]
                if isinstance(data.get("dock_color"), str) and re.fullmatch(r"#[0-9a-fA-F]{6}", data["dock_color"]):
                    self._data["dock_color"] = data["dock_color"].lower()
                for name, (minimum, maximum) in DOCK_RANGES.items():
                    value = data.get(name)
                    if type(value) is int and minimum <= value <= maximum:
                        self._data[name] = value
                for name in ("shelf_shortcut", "overview_shortcut"):
                    if name in data:
                        try:
                            self._data[name] = normalize_shortcut(data[name])
                        except ValueError:
                            pass
                if self._data["shelf_shortcut"] == self._data["overview_shortcut"]:
                    self._data["shelf_shortcut"] = SHELF_SHORTCUT
                    self._data["overview_shortcut"] = OVERVIEW_SHORTCUT
        except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError):
            pass

    @property
    def theme(self) -> str:
        return self._data["theme"]

    @property
    def capture_dir(self) -> Path:
        return Path(self._data["capture_dir"])

    @property
    def capture_mode(self) -> str:
        return self._data["capture_mode"]

    @property
    def keep_empty_handle(self) -> bool:
        return self._data["keep_empty_handle"]

    @property
    def trigger_view(self) -> str:
        return self._data["trigger_view"]

    @property
    def shake_sensitivity(self) -> int:
        return self._data["shake_sensitivity"]

    @property
    def autostart(self) -> bool:
        return self._data["autostart"]

    @property
    def view_mode(self) -> str:
        return self._data["view_mode"]

    @property
    def shelf_shortcut(self) -> str:
        return self._data["shelf_shortcut"]

    @property
    def overview_shortcut(self) -> str:
        return self._data["overview_shortcut"]

    @property
    def dock_edge(self) -> str:
        return self._data["dock_edge"]

    @property
    def dock_position(self) -> int:
        """Start of the trigger region, as percent of its available edge travel."""
        return self._data["dock_position"]

    @property
    def dock_length(self) -> int:
        """Trigger length in device-independent pixels, along the edge."""
        return self._data["dock_length"]

    @property
    def dock_thickness(self) -> int:
        """Trigger depth in device-independent pixels, into the screen."""
        return self._data["dock_thickness"]

    @property
    def trigger_visible(self) -> bool:
        return self._data["trigger_visible"]

    @property
    def dock_color(self) -> str:
        return self._data["dock_color"]

    @property
    def dock_opacity(self) -> int:
        return self._data["dock_opacity"]

    def reconcile_startup(self) -> bool:
        """Repair a missing or moved own login item without clearing OS blocks."""
        if not _startup_runtime_allowed() or not self.autostart:
            return False
        status = self.startup_status()
        if not status.supported or status.blocked:
            return False
        candidate = dict(self._data, startup_initialized=True)
        return self._commit(candidate, _startup_registration(True))

    def startup_status(self):
        return _startup_manager().status()

    @staticmethod
    def _validate_directory(value) -> Path:
        if not isinstance(value, (str, Path)) or not str(value).strip():
            raise ValueError("请选择内容保存位置。")
        directory = Path(os.path.expandvars(str(value).strip())).expanduser()
        if not directory.is_absolute():
            raise ValueError("保存位置需要使用完整路径，例如 D:\\FlickDock。")
        if directory.exists() and not directory.is_dir():
            raise ValueError("所选位置是文件，请选择一个文件夹。")
        try:
            directory.mkdir(parents=True, exist_ok=True)
            probe = directory / (".flickdock-write-" + uuid4().hex)
            try:
                with probe.open("xb") as stream:
                    stream.write(b"")
            finally:
                if probe.exists():
                    probe.unlink()
        except OSError as error:
            raise ValueError("无法写入这个文件夹，请选择可写入的位置。") from error
        return directory.resolve()

    def update(self, *, theme=None, capture_dir=None, autostart=None, view_mode=None,
               shelf_shortcut=None, overview_shortcut=None, dock_edge=None,
               dock_position=None, dock_length=None, dock_thickness=None,
               trigger_visible=None, dock_color=None, dock_opacity=None,
               capture_mode=None, keep_empty_handle=None, trigger_view=None,
               shake_sensitivity=None) -> bool:
        candidate = dict(self._data)
        if theme is not None:
            if theme not in ("light", "dark"):
                raise ValueError("请选择浅色或暗色外观。")
            candidate["theme"] = theme
        if capture_dir is not None:
            candidate["capture_dir"] = str(self._validate_directory(capture_dir))
        if capture_mode is not None:
            if capture_mode not in CAPTURE_MODES:
                raise ValueError("请选择快捷方式或复制到保存位置。")
            candidate["capture_mode"] = capture_mode
        if keep_empty_handle is not None:
            if not isinstance(keep_empty_handle, bool):
                raise ValueError("空搁架显示入口需要为开启或关闭。")
            candidate["keep_empty_handle"] = keep_empty_handle
        if trigger_view is not None:
            if trigger_view not in TRIGGER_VIEWS:
                raise ValueError("请选择触发后显示小搁架或内容总览。")
            candidate["trigger_view"] = trigger_view
        if shake_sensitivity is not None:
            if type(shake_sensitivity) is not int or not 1 <= shake_sensitivity <= 100:
                raise ValueError("摇动灵敏度需要为 1–100 之间的整数。")
            candidate["shake_sensitivity"] = shake_sensitivity
        if autostart is not None:
            if not isinstance(autostart, bool):
                raise ValueError("自启动选项需要为开启或关闭。")
            candidate["autostart"] = autostart
        if view_mode is not None:
            if view_mode not in ("list", "icons"):
                raise ValueError("请选择列表或图标展示方式。")
            candidate["view_mode"] = view_mode
        if dock_edge is not None:
            if dock_edge not in DOCK_EDGES:
                raise ValueError("请选择左侧、右侧、顶部或底部停靠。")
            candidate["dock_edge"] = dock_edge
        if trigger_visible is not None:
            if not isinstance(trigger_visible, bool):
                raise ValueError("显示触发区域需要为开启或关闭。")
            candidate["trigger_visible"] = trigger_visible
        if dock_color is not None:
            if not isinstance(dock_color, str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", dock_color):
                raise ValueError("请选择有效的触发区颜色。")
            candidate["dock_color"] = dock_color.lower()
        for name, value in (("dock_position", dock_position), ("dock_length", dock_length),
                            ("dock_thickness", dock_thickness), ("dock_opacity", dock_opacity)):
            if value is None:
                continue
            minimum, maximum = DOCK_RANGES[name]
            if type(value) is not int or not minimum <= value <= maximum:
                label = {"dock_position": "边缘位置", "dock_length": "触发区长度", "dock_thickness": "触发区厚度", "dock_opacity": "不透明度"}[name]
                raise ValueError(f"{label}需要为 {minimum}–{maximum} 之间的整数。")
            candidate[name] = value
        if shelf_shortcut is not None:
            candidate["shelf_shortcut"] = normalize_shortcut(shelf_shortcut)
        if overview_shortcut is not None:
            candidate["overview_shortcut"] = normalize_shortcut(overview_shortcut)
        if candidate["shelf_shortcut"] == candidate["overview_shortcut"]:
            raise ValueError("打开搁架和打开总览需要使用不同的快捷键。")
        startup_requested = autostart is not None and _startup_runtime_allowed()
        if candidate == self._data and not startup_requested:
            return False
        if startup_requested:
            candidate["startup_initialized"] = True
        changed = candidate != self._data
        startup_changed = self._commit(candidate,
            _startup_registration(candidate["autostart"]) if startup_requested else _NO_STARTUP_CHANGE,
            startup_explicit=startup_requested)
        return changed or startup_changed

    def _commit(self, candidate, startup_value=_NO_STARTUP_CHANGE, *, startup_explicit=False):
        self.base.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(".settings-" + uuid4().hex + ".tmp")
        startup_backend = None
        startup_snapshot = None
        startup_changed = False
        try:
            with temporary.open("x", encoding="utf-8", newline="\n") as stream:
                json.dump({"version": 1, **candidate}, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            if startup_value is not _NO_STARTUP_CHANGE:
                manager = _startup_manager()
                status = manager.status()
                if not status.supported:
                    raise OSError(status.reason)
                startup_backend = manager.backend
                startup_snapshot = (startup_backend.read(), startup_backend.read_approval())
                status = manager.reconcile(candidate["autostart"], explicit=startup_explicit)
                startup_changed = status.changed
                if status.enabled != candidate["autostart"]:
                    raise OSError(status.reason or "登录启动未能保存，请重试。")
            os.replace(temporary, self.path)
        except Exception:
            if startup_changed and startup_backend is not None and startup_snapshot is not None:
                startup_backend.write(startup_snapshot[0])
                startup_backend.write_approval(startup_snapshot[1])
            raise
        finally:
            if temporary.exists():
                temporary.unlink()
        self._data = candidate
        self.changed.emit()
        return startup_changed


class ThirdPartyDialog(QDialog):
    """An offline, selectable reader for the application's bundled licenses."""
    def __init__(self, parent=None, theme="light"):
        super().__init__(parent, Qt.WindowType.Tool | Qt.WindowType.WindowTitleHint | Qt.WindowType.WindowCloseButtonHint)
        self.setObjectName("flickdockLicenses")
        self.setWindowTitle("FlickDock · 第三方许可")
        self.setModal(False)
        self.setMinimumSize(520, 440)
        self.resize(680, 620)
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 22, 24, 20)
        root.setSpacing(14)
        title = QLabel("第三方许可", self)
        title.setObjectName("licenseTitle")
        root.addWidget(title)
        self.font_notice = QLabel("本软件使用 HarmonyOS Sans 字体", self)
        self.font_notice.setObjectName("licenseAttribution")
        self.font_notice.setWordWrap(True)
        root.addWidget(self.font_notice)
        self.document_combo = SettingsComboBox(self)
        self.document_combo.setAccessibleName("许可文档")
        resource_root = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))
        for label, relative in (
            ("组件说明", "THIRD_PARTY_NOTICES.md"),
            ("HarmonyOS Sans 字体许可", "licenses/HarmonyOS-Sans-LICENSE.txt"),
            ("Qt / PySide6 · LGPL 3.0", "licenses/LGPL-3.0.txt"),
            ("GPL 3.0", "licenses/GPL-3.0.txt"),
            ("Python", "licenses/Python-LICENSE.txt"),
            ("PyInstaller", "licenses/PyInstaller-COPYING.txt"),
        ):
            self.document_combo.addItem(label, str(resource_root / relative))
        root.addWidget(self.document_combo)
        self.license_text = QPlainTextEdit(self)
        self.license_text.setReadOnly(True)
        self.license_text.setAcceptDrops(False)
        self.license_text.setAccessibleName("许可全文")
        root.addWidget(self.license_text, 1)
        footer = QHBoxLayout()
        footer.addStretch()
        close = QPushButton("关闭", self)
        close.setMinimumSize(82, 36)
        close.clicked.connect(self.accept)
        footer.addWidget(close)
        root.addLayout(footer)
        self.document_combo.currentIndexChanged.connect(self._load_document)
        self._load_document()
        self.apply_theme(theme)

    def _load_document(self):
        path = Path(self.document_combo.currentData())
        try:
            text = path.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeError):
            text = "安装目录中缺少这份许可文档，请重新安装 FlickDock。\n\n" + path.name
        self.license_text.setPlainText(text)

    def apply_theme(self, theme):
        dark = theme == "dark"
        self.document_combo.dark = dark
        self.document_combo.update()
        background = "#1d2027" if dark else "#f7f9fc"
        surface = "#272c35" if dark else "#ffffff"
        ink = "#edf2fa" if dark else "#253246"
        edge = "#3c4554" if dark else "#dce3ed"
        hover = "#39465a" if dark else "#edf4fe"
        self.setStyleSheet(f"""
            QDialog#flickdockLicenses {{background:{background};color:{ink};}}
            QDialog#flickdockLicenses QLabel {{color:{ink};background:transparent;font-size:14px;}}
            QDialog#flickdockLicenses QLabel#licenseTitle {{font-size:22px;font-weight:600;}}
            QDialog#flickdockLicenses QLabel#licenseAttribution {{font-size:16px;font-weight:600;}}
            QDialog#flickdockLicenses QPlainTextEdit {{background:{surface};color:{ink};border:1px solid {edge};border-radius:10px;padding:12px;font-size:13px;selection-background-color:#2584ed;selection-color:white;}}
            QDialog#flickdockLicenses QComboBox, QDialog#flickdockLicenses QPushButton {{background:{surface};color:{ink};border:1px solid {edge};border-radius:8px;padding:7px 10px;font-size:13px;}}
            QDialog#flickdockLicenses QComboBox QAbstractItemView {{background:{surface};color:{ink};selection-background-color:{hover};selection-color:{ink};}}
            QDialog#flickdockLicenses QComboBox::drop-down {{border:0;width:28px;}}
            QDialog#flickdockLicenses QComboBox::down-arrow {{image:none;}}
            QDialog#flickdockLicenses QPushButton:hover {{background:{hover};}}
        """)


class AuthorDialog(QDialog):
    def __init__(self, parent=None, theme="light"):
        super().__init__(parent, Qt.WindowType.Tool | Qt.WindowType.WindowTitleHint | Qt.WindowType.WindowCloseButtonHint)
        self.setObjectName("flickdockAuthor")
        self.setWindowTitle("FlickDock · Author @Roger")
        self.setModal(False)
        self.setMinimumSize(400, 300)
        self.resize(440, 320)
        self.license_dialog = None
        root = QVBoxLayout(self)
        root.setContentsMargins(28, 26, 28, 22)
        root.setSpacing(14)
        title = QLabel("FlickDock", self)
        title.setObjectName("authorTitle")
        root.addWidget(title)
        self.author_label = QLabel("Author @Roger", self)
        self.author_label.setObjectName("authorName")
        self.author_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        root.addWidget(self.author_label)
        root.addStretch()
        self.font_notice = QLabel("本软件使用 HarmonyOS Sans 字体", self)
        self.font_notice.setObjectName("authorAttribution")
        self.font_notice.setWordWrap(True)
        root.addWidget(self.font_notice)
        copyright = QLabel("Copyright 2021 Huawei Device Co., Ltd.", self)
        copyright.setObjectName("authorCopyright")
        root.addWidget(copyright)
        footer = QHBoxLayout()
        self.licenses_button = QPushButton("第三方许可", self)
        self.licenses_button.setMinimumHeight(36)
        self.licenses_button.clicked.connect(self.show_licenses)
        footer.addWidget(self.licenses_button)
        footer.addStretch()
        close = QPushButton("关闭", self)
        close.setMinimumSize(82, 36)
        close.clicked.connect(self.accept)
        footer.addWidget(close)
        root.addLayout(footer)
        self.apply_theme(theme)

    def show_licenses(self):
        if self.license_dialog is None:
            self.license_dialog = ThirdPartyDialog(self, self._theme)
        self.license_dialog.apply_theme(self._theme)
        self.license_dialog.show()
        self.license_dialog.raise_()
        self.license_dialog.activateWindow()
        return self.license_dialog

    def apply_theme(self, theme):
        self._theme = theme
        dark = theme == "dark"
        background = "#1d2027" if dark else "#f7f9fc"
        surface = "#272c35" if dark else "#ffffff"
        ink = "#edf2fa" if dark else "#253246"
        secondary = "#a5b1c3" if dark else "#7a8799"
        edge = "#3c4554" if dark else "#dce3ed"
        hover = "#39465a" if dark else "#edf4fe"
        self.setStyleSheet(f"""
            QDialog#flickdockAuthor {{background:{background};color:{ink};}}
            QDialog#flickdockAuthor QLabel {{background:transparent;color:{ink};font-size:14px;}}
            QDialog#flickdockAuthor QLabel#authorTitle {{font-size:25px;font-weight:600;}}
            QDialog#flickdockAuthor QLabel#authorName {{font-size:18px;font-weight:600;}}
            QDialog#flickdockAuthor QLabel#authorAttribution {{font-size:14px;}}
            QDialog#flickdockAuthor QLabel#authorCopyright {{color:{secondary};font-size:12px;}}
            QDialog#flickdockAuthor QPushButton {{background:{surface};color:{ink};border:1px solid {edge};border-radius:8px;padding:6px 12px;font-size:13px;}}
            QDialog#flickdockAuthor QPushButton:hover {{background:{hover};}}
        """)
        if self.license_dialog is not None:
            self.license_dialog.apply_theme(theme)


class SettingsSurface(QFrame):
    """Antialiased backing for the transparent, frameless settings window."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("settingsSurface")
        self.dark = False

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor("#3c4554" if self.dark else "#dce3ed"), 1))
        painter.setBrush(QColor("#1d2027" if self.dark else "#f7f9fc"))
        painter.drawRoundedRect(QRectF(self.rect()).adjusted(.5, .5, -.5, -.5), 18, 18)


class SettingsDialog(QDialog):
    preview_changed = Signal(object)
    gesture_preview_changed = Signal(object)
    update_requested = Signal()

    def __init__(self, store: SettingsStore, parent=None):
        super().__init__(parent, Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint)
        self.store = store
        self.shortcut_validator = None
        self.license_dialog = None
        self.author_dialog = None
        self._loading_values = True
        self._preview_active = False
        self._account_panel = None
        self._update_state = "idle"
        self._draft_color = store.dock_color
        self.setObjectName("flickdockSettings")
        self.setWindowTitle("FlickDock 设置")
        self.setFont(QFont("HarmonyOS Sans SC", 11))
        self.setModal(False)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setMinimumSize(540, 520)
        self.resize(560, 680)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.surface = SettingsSurface(self)
        outer.addWidget(self.surface)
        root = QVBoxLayout(self.surface)
        root.setContentsMargins(24, 16, 24, 20)
        root.setSpacing(14)
        self.title_bar = SettingsTitleBar(self)
        header = QHBoxLayout(self.title_bar)
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(8)
        title = QLabel("设置", self.title_bar)
        title.setObjectName("settingsTitle")
        title.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        header.addWidget(title)
        self.version_label = QLabel(f"v{APP_VERSION}", self.title_bar)
        self.version_label.setObjectName("settingsVersion")
        self.version_label.setAccessibleName("当前版本")
        self.version_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        header.addWidget(self.version_label)
        header.addStretch()
        self.update_status_label = QLabel(self.title_bar)
        self.update_status_label.setObjectName("settingsUpdateStatus")
        self.update_status_label.setMaximumWidth(124)
        self.update_status_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        header.addWidget(self.update_status_label)
        self.update_button = QPushButton("检查更新", self.title_bar)
        self.update_button.setObjectName("settingsUpdate")
        self.update_button.setMinimumSize(96, 36)
        self.update_button.setAutoDefault(False)
        self.update_button.setDefault(False)
        self.update_button.setAccessibleName("检查更新")
        self.update_button.clicked.connect(self.update_requested.emit)
        header.addWidget(self.update_button)
        self.close_button = SettingsCloseButton(self.title_bar, glyph_size=14)
        self.close_button.clicked.connect(self.reject)
        header.addWidget(self.close_button)
        root.addWidget(self.title_bar)

        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setObjectName("settingsScroll")
        self.scroll = scroll
        body = QWidget(scroll)
        body.setObjectName("settingsBody")
        self.body = body
        body_layout = QVBoxLayout(body)
        self.body_layout = body_layout
        body_layout.setContentsMargins(0, 0, 2, 0)
        body_layout.setSpacing(14)
        scroll.setWidget(body)
        root.addWidget(scroll, 1)

        appearance = self._card()
        appearance_layout = QVBoxLayout(appearance)
        appearance_layout.setContentsMargins(16, 14, 16, 14)
        appearance_layout.setSpacing(12)
        appearance_row = QHBoxLayout()
        appearance_row.addWidget(QLabel("外观", appearance), 1)
        self.theme_combo = SettingsComboBox(appearance)
        self.theme_combo.addItem("浅色", "light")
        self.theme_combo.addItem("暗色", "dark")
        self.theme_combo.setMinimumWidth(160)
        self.theme_combo.setAccessibleName("外观")
        self.theme_combo.currentIndexChanged.connect(lambda _: self.apply_theme(self.theme_combo.currentData()))
        appearance_row.addWidget(self.theme_combo)
        appearance_layout.addLayout(appearance_row)
        view_row = QHBoxLayout()
        view_row.addWidget(QLabel("展示方式", appearance), 1)
        self.view_combo = SettingsComboBox(appearance)
        self.view_combo.addItem("列表", "list")
        self.view_combo.addItem("图标", "icons")
        self.view_combo.setMinimumWidth(160)
        self.view_combo.setAccessibleName("展示方式")
        view_row.addWidget(self.view_combo)
        appearance_layout.addLayout(view_row)
        body_layout.addWidget(self._dock_card())
        body_layout.addWidget(self._interaction_card())
        body_layout.addWidget(appearance)

        storage = self._card()
        storage_layout = QVBoxLayout(storage)
        storage_layout.setContentsMargins(16, 14, 16, 14)
        storage_layout.setSpacing(10)
        capture_row = QHBoxLayout()
        capture_row.addWidget(QLabel("放入文件", storage), 1)
        self.capture_mode_combo = SettingsComboBox(storage)
        self.capture_mode_combo.addItem("快捷方式", "reference")
        self.capture_mode_combo.addItem("复制到保存位置", "copy")
        self.capture_mode_combo.setMinimumWidth(190)
        self.capture_mode_combo.setAccessibleName("放入文件的方式")
        capture_row.addWidget(self.capture_mode_combo)
        storage_layout.addLayout(capture_row)
        storage_layout.addWidget(QLabel("内容保存位置", storage))
        path_row = QHBoxLayout()
        path_row.setSpacing(8)
        self.folder_edit = QLineEdit(storage)
        self.folder_edit.setAccessibleName("内容保存位置")
        self.folder_edit.setMinimumHeight(34)
        browse = QPushButton("浏览…", storage)
        browse.setMinimumHeight(34)
        browse.setFixedWidth(78)
        browse.clicked.connect(self.choose_directory)
        path_row.addWidget(self.folder_edit, 1)
        path_row.addWidget(browse)
        storage_layout.addLayout(path_row)
        detail = QLabel("快捷方式保留原文件位置；复制会将文件保存到这里。新接收的图片等内容也保存到这里，文字保存在搁架数据中。", storage)
        detail.setObjectName("settingsDetail")
        detail.setWordWrap(True)
        storage_layout.addWidget(detail)
        body_layout.addWidget(storage)

        shortcuts = self._card()
        shortcuts_layout = QVBoxLayout(shortcuts)
        shortcuts_layout.setContentsMargins(16, 14, 16, 14)
        shortcuts_layout.setSpacing(12)
        shortcut_heading = QLabel("全局快捷键", shortcuts)
        shortcuts_layout.addWidget(shortcut_heading)
        for name, label in (("shelf", "打开搁架"), ("overview", "鼠标旁打开总览")):
            row = QHBoxLayout()
            row.addWidget(QLabel(label, shortcuts), 1)
            editor = QKeySequenceEdit(shortcuts)
            editor.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
            editor.setMaximumSequenceLength(1)
            editor.setMinimumHeight(34)
            editor.setFixedWidth(220)
            editor.setAccessibleName(label)
            row.addWidget(editor)
            shortcuts_layout.addLayout(row)
            setattr(self, name + "_shortcut_edit", editor)
        body_layout.addWidget(shortcuts)

        startup = self._card()
        startup_layout = QVBoxLayout(startup)
        startup_layout.setContentsMargins(16, 14, 16, 14)
        self.startup_checkbox = SettingsCheckBox("登录时自动启动 FlickDock", startup)
        self.startup_checkbox.setAccessibleName("登录时自动启动 FlickDock")
        startup_layout.addWidget(self.startup_checkbox)
        self.startup_status_label = QLabel(startup)
        self.startup_status_label.setObjectName("settingsDetail")
        self.startup_status_label.setWordWrap(True)
        startup_layout.addWidget(self.startup_status_label)
        body_layout.addWidget(startup)
        body_layout.addStretch()
        self.error_label = QLabel(self)
        self.error_label.setObjectName("settingsError")
        self.error_label.setWordWrap(True)
        self.error_label.hide()
        root.addWidget(self.error_label)
        footer = QHBoxLayout()
        self.author_button = QPushButton("Author @Roger", self)
        self.author_button.setObjectName("settingsAuthor")
        self.author_button.setMinimumHeight(36)
        self.author_button.clicked.connect(self.show_author)
        self.licenses_button = self.author_button  # Compatibility for existing callers.
        footer.addWidget(self.author_button)
        footer.addStretch()
        cancel = QPushButton("取消", self)
        self.cancel_button = cancel
        cancel.setMinimumSize(82, 36)
        cancel.clicked.connect(self.reject)
        self.save_button = QPushButton("保存设置", self)
        self.save_button.setObjectName("settingsSave")
        self.save_button.setMinimumSize(104, 36)
        self.save_button.setDefault(True)
        self.save_button.clicked.connect(self.save)
        footer.addWidget(cancel)
        footer.addWidget(self.save_button)
        root.addLayout(footer)
        self.load_values()
        self.set_update_state("idle")
        for widget in self.findChildren(QWidget):
            widget.installEventFilter(self)

    @property
    def update_state(self):
        return self._update_state

    def set_account_panel(self, widget):
        """Place a caller-owned account view first, without importing its module."""
        previous = self._account_panel
        if previous is widget:
            return previous
        if previous is not None:
            self.body_layout.removeWidget(previous)
            previous.hide()
            previous.setParent(None)
        self._account_panel = widget
        if widget is not None:
            self.body_layout.insertWidget(0, widget)
            widget.show()
            for child in [widget, *widget.findChildren(QWidget)]:
                child.installEventFilter(self)
            if hasattr(widget, "apply_theme"):
                widget.apply_theme(self.theme_combo.currentData())
        return previous

    def set_update_state(self, state, version="", received=0, total=0, error=""):
        """Render update feedback beside the single explicit update action."""
        received, total = max(0, int(received)), max(0, int(total))
        labels = {
            "idle": ("", "检查更新"), "checking": ("正在检查…", "检查中…"),
            "latest": ("已是最新版本", "检查更新"),
            "available": ("有新版本", "立即更新"),
            "downloading": (f"正在下载 {min(100, round(received*100/total))}%" if total else "正在下载…", "更新中…"),
            "verifying": ("正在校验…", "更新中…"), "ready": ("更新已准备好", "立即更新"),
            "installing": ("正在安装…", "更新中…"), "cancelling": ("正在取消…", "更新中…"),
            "cancelled": ("更新已取消", "继续更新" if version else "检查更新"),
            "error": ("更新失败" if version else "检查失败", "重试更新" if version else "重试检查"),
        }
        if state not in labels:
            raise ValueError(f"Unknown update state: {state}")
        self._update_state = state
        status, action = labels[state]
        self.update_status_label.setText(status)
        self.update_status_label.setAccessibleName(status + (f"，版本 {version}" if version else ""))
        self.update_status_label.setAccessibleDescription(str(error))
        self.update_button.setText(action)
        self.update_button.setAccessibleName(action)
        self.update_button.setEnabled(state not in ("checking", "downloading", "verifying", "installing", "cancelling"))

    def eventFilter(self, watched, event):
        # QKeySequenceEdit consumes Esc while recording a shortcut. Closing
        # settings consistently must still use the dialog's reject path.
        if (event.type() == QEvent.Type.KeyPress and event.key() == Qt.Key.Key_Escape
                and isinstance(watched, QWidget) and watched.window() is self):
            self.reject()
            event.accept()
            return True
        return super().eventFilter(watched, event)

    def _card(self):
        card = QFrame(self)
        card.setObjectName("settingsCard")
        return card

    @staticmethod
    def _number_field(minimum, maximum, suffix, parent):
        field = SettingsSpinBox(parent)
        field.setRange(minimum, maximum)
        field.setSuffix(suffix)
        field.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.UpDownArrows)
        field.setAlignment(Qt.AlignmentFlag.AlignCenter)
        field.setMinimumHeight(34)
        field.setFixedWidth(104)
        return field

    def _dock_card(self):
        card = self._card()
        layout = QVBoxLayout(card)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(12)
        self.trigger_visible_checkbox = SettingsCheckBox("显示触发区域", card)
        self.trigger_visible_checkbox.setAccessibleName("显示触发区域")
        layout.addWidget(self.trigger_visible_checkbox)
        layout.addWidget(QLabel("边缘停靠", card))
        edge_row = QHBoxLayout()
        edge_row.addWidget(QLabel("停靠边缘", card), 1)
        self.dock_edge_combo = SettingsComboBox(card)
        for label, value in (("右侧", "right"), ("左侧", "left"), ("顶部", "top"), ("底部", "bottom")):
            self.dock_edge_combo.addItem(label, value)
        self.dock_edge_combo.setMinimumWidth(160)
        self.dock_edge_combo.setAccessibleName("停靠边缘")
        edge_row.addWidget(self.dock_edge_combo)
        layout.addLayout(edge_row)
        position_row = QHBoxLayout()
        position_row.setSpacing(12)
        position_row.addWidget(QLabel("边缘位置", card))
        self.dock_position_slider = QSlider(Qt.Orientation.Horizontal, card)
        self.dock_position_slider.setRange(0, 100)
        self.dock_position_slider.setAccessibleName("边缘位置")
        position_row.addWidget(self.dock_position_slider, 1)
        self.dock_position_spin = self._number_field(0, 100, " %", card)
        self.dock_position_spin.setAccessibleName("边缘位置百分比")
        position_row.addWidget(self.dock_position_spin)
        self.dock_position_slider.valueChanged.connect(self.dock_position_spin.setValue)
        self.dock_position_spin.valueChanged.connect(self.dock_position_slider.setValue)
        layout.addLayout(position_row)
        self.dock_position_hint = QLabel(card)
        self.dock_position_hint.setObjectName("settingsDetail")
        self.dock_position_hint.setWordWrap(True)
        layout.addWidget(self.dock_position_hint)
        for name, label in (("dock_length", "触发区长度"), ("dock_thickness", "触发区厚度")):
            row = QHBoxLayout()
            row.addWidget(QLabel(label, card), 1)
            field = self._number_field(*DOCK_RANGES[name], " px", card)
            field.setAccessibleName(label)
            setattr(self, name + "_spin", field)
            row.addWidget(field)
            layout.addLayout(row)
        color_row = QHBoxLayout()
        color_row.addWidget(QLabel("标记颜色", card), 1)
        self.dock_color_button = QPushButton(card)
        self.dock_color_button.setMinimumSize(130, 36)
        self.dock_color_button.setAccessibleName("触发区域标记颜色")
        self.dock_color_button.clicked.connect(self.choose_dock_color)
        color_row.addWidget(self.dock_color_button)
        layout.addLayout(color_row)
        opacity_row = QHBoxLayout()
        opacity_row.setSpacing(12)
        opacity_row.addWidget(QLabel("不透明度", card))
        self.dock_opacity_slider = QSlider(Qt.Orientation.Horizontal, card)
        self.dock_opacity_slider.setRange(0, 100)
        self.dock_opacity_slider.setAccessibleName("触发区域不透明度")
        opacity_row.addWidget(self.dock_opacity_slider, 1)
        self.dock_opacity_spin = self._number_field(0, 100, " %", card)
        self.dock_opacity_spin.setAccessibleName("不透明度百分比")
        opacity_row.addWidget(self.dock_opacity_spin)
        self.dock_opacity_slider.valueChanged.connect(self.dock_opacity_spin.setValue)
        self.dock_opacity_spin.valueChanged.connect(self.dock_opacity_slider.setValue)
        layout.addLayout(opacity_row)
        detail = QLabel("鼠标移入触发区即可展开搁架。尺寸跟随 Windows 显示缩放。", card)
        detail.setObjectName("settingsDetail")
        detail.setWordWrap(True)
        layout.addWidget(detail)
        self.dock_edge_combo.currentIndexChanged.connect(self._update_dock_hint)
        self.dock_edge_combo.currentIndexChanged.connect(self._emit_preview)
        for control in (self.dock_position_spin, self.dock_length_spin, self.dock_thickness_spin, self.dock_opacity_spin):
            control.valueChanged.connect(self._emit_preview)
        self.trigger_visible_checkbox.toggled.connect(self._emit_preview)
        self._update_dock_hint()
        return card

    def _interaction_card(self):
        card = self._card()
        layout = QVBoxLayout(card)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(12)
        layout.addWidget(QLabel("触发与摇动", card))
        self.keep_empty_handle_checkbox = SettingsCheckBox("空搁架仍显示边缘入口", card)
        self.keep_empty_handle_checkbox.setAccessibleName("空搁架仍显示边缘入口")
        layout.addWidget(self.keep_empty_handle_checkbox)
        row = QHBoxLayout()
        row.addWidget(QLabel("触发后显示", card), 1)
        self.trigger_view_combo = SettingsComboBox(card)
        self.trigger_view_combo.addItem("小搁架", "compact")
        self.trigger_view_combo.addItem("内容总览", "expanded")
        self.trigger_view_combo.setMinimumWidth(190)
        self.trigger_view_combo.setAccessibleName("触发后显示的视图")
        row.addWidget(self.trigger_view_combo)
        layout.addLayout(row)
        sensitivity = QHBoxLayout()
        sensitivity.setSpacing(10)
        sensitivity.addWidget(QLabel("摇动灵敏度", card))
        self.shake_sensitivity_slider = QSlider(Qt.Orientation.Horizontal, card)
        self.shake_sensitivity_slider.setRange(1, 100)
        self.shake_sensitivity_slider.setAccessibleName("摇动灵敏度")
        sensitivity.addWidget(self.shake_sensitivity_slider, 1)
        self.shake_sensitivity_spin = self._number_field(1, 100, "", card)
        self.shake_sensitivity_spin.setAccessibleName("摇动灵敏度数值")
        self.shake_sensitivity_spin.setFixedWidth(76)
        sensitivity.addWidget(self.shake_sensitivity_spin)
        self.shake_sensitivity_reset_button = SettingsResetButton(card)
        self.shake_sensitivity_reset_button.clicked.connect(lambda: self.shake_sensitivity_spin.setValue(DEFAULT_SHAKE_SENSITIVITY))
        sensitivity.addWidget(self.shake_sensitivity_reset_button)
        self.shake_sensitivity_slider.valueChanged.connect(self.shake_sensitivity_spin.setValue)
        self.shake_sensitivity_spin.valueChanged.connect(self.shake_sensitivity_slider.setValue)
        self.shake_sensitivity_spin.valueChanged.connect(self._emit_gesture_preview)
        layout.addLayout(sensitivity)
        self.shake_effort_label=QLabel(card)
        self.shake_effort_label.setObjectName('settingsDetail')
        layout.addWidget(self.shake_effort_label)
        detail = QLabel("拖住内容快速来回甩动。调节立即生效，取消后恢复原值。", card)
        detail.setObjectName("settingsDetail")
        detail.setWordWrap(True)
        layout.addWidget(detail)
        return card

    def _update_dock_hint(self):
        vertical = self.dock_edge_combo.currentData() in ("left", "right")
        self.dock_position_hint.setText("0% 靠上，100% 靠下。" if vertical else "0% 靠左，100% 靠右。")

    def _dock_values(self):
        return {"dock_edge": self.dock_edge_combo.currentData(), "dock_position": self.dock_position_spin.value(),
                "dock_length": self.dock_length_spin.value(), "dock_thickness": self.dock_thickness_spin.value(),
                "trigger_visible": self.trigger_visible_checkbox.isChecked(), "dock_color": self._draft_color,
                "dock_opacity": self.dock_opacity_spin.value()}

    def _emit_preview(self, *_):
        if not self._loading_values and self.isVisible():
            self._preview_active = True
            self.preview_changed.emit(self._dock_values())

    def _emit_gesture_preview(self,*_):
        from shake_detector import config_for_sensitivity
        value=self.shake_sensitivity_spin.value()
        config=config_for_sensitivity(value)
        effort='更用力' if value<36 else '适中力度' if value<80 else '更轻快'
        self.shake_effort_label.setText(f'{effort} · {config.reversals} 次有效换向')
        if not self._loading_values and self.isVisible():
            self.gesture_preview_changed.emit(value)

    def _finish_preview(self, force=False):
        if self._preview_active or force:
            self._preview_active = False
            self.preview_changed.emit(None)
            self.gesture_preview_changed.emit(None)

    def _set_dock_color(self, value):
        color = QColor(value)
        if not color.isValid():
            return
        self._draft_color = color.name()
        self.dock_color_button.setText(self._draft_color.upper())
        ratio = max(1.0, self.devicePixelRatioF())
        swatch = QPixmap(round(18 * ratio), round(18 * ratio))
        swatch.setDevicePixelRatio(ratio)
        swatch.fill(Qt.GlobalColor.transparent)
        painter = QPainter(swatch)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor("#8793a3"), .7))
        painter.setBrush(color)
        painter.drawEllipse(1, 1, 16, 16)
        painter.end()
        self.dock_color_button.setIcon(QIcon(swatch))
        self._emit_preview()

    def choose_dock_color(self):
        original = self._draft_color
        picker = QColorDialog(QColor(original), self)
        picker.setWindowTitle("触发区域标记颜色")
        picker.setOption(QColorDialog.ColorDialogOption.DontUseNativeDialog, True)
        picker.currentColorChanged.connect(lambda color: self._set_dock_color(color) if color.isValid() else None)
        if picker.exec() != QDialog.DialogCode.Accepted:
            self._set_dock_color(original)
        picker.deleteLater()

    def show_author(self):
        if self.author_dialog is None:
            self.author_dialog = AuthorDialog(self, self.theme_combo.currentData())
        self.author_dialog.apply_theme(self.theme_combo.currentData())
        self.author_dialog.show()
        self.author_dialog.raise_()
        self.author_dialog.activateWindow()

    def show_licenses(self):
        self.show_author()
        self.license_dialog = self.author_dialog.show_licenses()

    def load_values(self):
        self._loading_values = True
        self.theme_combo.setCurrentIndex(1 if self.store.theme == "dark" else 0)
        self.folder_edit.setText(str(self.store.capture_dir))
        self.capture_mode_combo.setCurrentIndex(self.capture_mode_combo.findData(self.store.capture_mode))
        self.keep_empty_handle_checkbox.setChecked(self.store.keep_empty_handle)
        self.trigger_view_combo.setCurrentIndex(self.trigger_view_combo.findData(self.store.trigger_view))
        self.shake_sensitivity_spin.setValue(self.store.shake_sensitivity)
        self.startup_checkbox.setChecked(self.store.autostart)
        startup = self.store.startup_status()
        self.startup_status_label.setVisible(startup.supported)
        self.startup_status_label.setText(
            "已启用登录启动" if startup.enabled else startup.reason if self.store.autostart else "已关闭登录启动")
        self.view_combo.setCurrentIndex(1 if self.store.view_mode == "icons" else 0)
        self.dock_edge_combo.setCurrentIndex(self.dock_edge_combo.findData(self.store.dock_edge))
        self.dock_position_spin.setValue(self.store.dock_position)
        self.dock_length_spin.setValue(self.store.dock_length)
        self.dock_thickness_spin.setValue(self.store.dock_thickness)
        self.trigger_visible_checkbox.setChecked(self.store.trigger_visible)
        self.dock_opacity_spin.setValue(self.store.dock_opacity)
        self._set_dock_color(self.store.dock_color)
        self.shelf_shortcut_edit.setKeySequence(QKeySequence(self.store.shelf_shortcut))
        self.overview_shortcut_edit.setKeySequence(QKeySequence(self.store.overview_shortcut))
        self.error_label.clear()
        self.error_label.hide()
        self.apply_theme(self.store.theme)
        self._loading_values = False
        self._emit_preview()
        self._emit_gesture_preview()

    def showEvent(self, event):
        self.load_values()
        super().showEvent(event)
        if not self._preview_active:
            self._emit_preview()

    def done(self, result):
        self._finish_preview(force=True)
        super().done(result)

    def hideEvent(self, event):
        self._finish_preview()
        super().hideEvent(event)

    def choose_directory(self):
        chosen = QFileDialog.getExistingDirectory(self, "选择内容保存位置", self.folder_edit.text() or str(self.store.capture_dir))
        if chosen:
            self.folder_edit.setText(chosen)

    def save(self):
        try:
            values = self.draft()
            if values["shelf_shortcut"] == values["overview_shortcut"]:
                raise ValueError("打开搁架和打开总览需要使用不同的快捷键。")
            if self.shortcut_validator is not None:
                error = self.shortcut_validator(values)
                if error:
                    raise ValueError(error)
            self.store.update(**values)
        except (OSError, ValueError, RuntimeError) as error:
            self.error_label.setText(str(error) or "设置未能保存，请重试。")
            self.error_label.show()
            return
        self.accept()

    def draft(self):
        """Return the form values without committing preferences or new behavior."""
        return {"theme": self.theme_combo.currentData(), "capture_dir": self.folder_edit.text(),
                "capture_mode": self.capture_mode_combo.currentData(),
                "keep_empty_handle": self.keep_empty_handle_checkbox.isChecked(),
                "trigger_view": self.trigger_view_combo.currentData(),
                "shake_sensitivity": self.shake_sensitivity_spin.value(),
                "autostart": self.startup_checkbox.isChecked(), "view_mode": self.view_combo.currentData(),
                "shelf_shortcut": normalize_shortcut(self.shelf_shortcut_edit.keySequence()),
                "overview_shortcut": normalize_shortcut(self.overview_shortcut_edit.keySequence()),
                **self._dock_values()}

    def apply_theme(self, theme):
        dark = theme == "dark"
        self.surface.dark = dark
        self.surface.update()
        for combo in (self.theme_combo, self.view_combo, self.dock_edge_combo, self.capture_mode_combo, self.trigger_view_combo):
            combo.dark = dark
            combo.update()
        for field in (self.dock_position_spin, self.dock_length_spin, self.dock_thickness_spin, self.dock_opacity_spin, self.shake_sensitivity_spin):
            field.dark = dark
            field.update()
        self.shake_sensitivity_reset_button.dark = dark
        self.shake_sensitivity_reset_button.update()
        self.close_button.dark = dark
        self.close_button.update()
        bg = "#1d2027" if dark else "#f7f9fc"
        card = "#272c35" if dark else "#ffffff"
        ink = "#edf2fa" if dark else "#253246"
        secondary = "#a5b1c3" if dark else "#7a8799"
        edge = "#3c4554" if dark else "#dce3ed"
        field = "#20252e" if dark else "#fbfcfe"
        hover = "#39465a" if dark else "#edf4fe"
        self.setStyleSheet(f"""
            QDialog#flickdockSettings {{background:transparent;color:{ink};}}
            QFrame#settingsSurface {{background:transparent;border:0;}}
            QScrollArea#settingsScroll, QWidget#settingsBody {{background:transparent;border:0;}}
            QScrollArea#settingsScroll QScrollBar:vertical {{width:6px;background:transparent;border:0;}}
            QScrollArea#settingsScroll QScrollBar::handle:vertical {{background:{'#5a677b' if dark else '#c1ccda'};border-radius:3px;min-height:28px;}}
            QScrollArea#settingsScroll QScrollBar::add-line:vertical, QScrollArea#settingsScroll QScrollBar::sub-line:vertical {{height:0;}}
            QScrollArea#settingsScroll QScrollBar::add-page:vertical, QScrollArea#settingsScroll QScrollBar::sub-page:vertical {{background:transparent;}}
            QDialog#flickdockSettings QLabel, QDialog#flickdockSettings QCheckBox {{color:{ink};font-size:13px;background:transparent;}}
            QDialog#flickdockSettings QLabel#settingsTitle {{font-size:22px;font-weight:600;}}
            QDialog#flickdockSettings QLabel#settingsVersion {{color:{secondary};font-size:12px;}}
            QDialog#flickdockSettings QLabel#settingsUpdateStatus {{color:{secondary};font-size:12px;}}
            QDialog#flickdockSettings QLabel#settingsDetail {{color:{secondary};font-size:12px;}}
            QFrame#settingsCard {{background:{card};border:1px solid {edge};border-radius:13px;}}
            QDialog#flickdockSettings QLineEdit, QDialog#flickdockSettings QComboBox, QDialog#flickdockSettings QSpinBox {{background:{field};color:{ink};border:1px solid {edge};border-radius:8px;padding:7px 10px;font-size:13px;}}
            QDialog#flickdockSettings QKeySequenceEdit {{background:{field};color:{ink};border:1px solid {edge};border-radius:8px;font-size:13px;}}
            QDialog#flickdockSettings QKeySequenceEdit QLineEdit {{border:0;background:transparent;padding:5px 8px;}}
            QDialog#flickdockSettings QLineEdit:focus, QDialog#flickdockSettings QComboBox:focus, QDialog#flickdockSettings QSpinBox:focus {{border-color:#5d9eeb;}}
            QDialog#flickdockSettings QSpinBox QLineEdit {{border:0;background:transparent;padding:0;}}
            QDialog#flickdockSettings QSpinBox::up-button {{subcontrol-origin:border;subcontrol-position:top right;width:22px;border:0;background:transparent;}}
            QDialog#flickdockSettings QSpinBox::down-button {{subcontrol-origin:border;subcontrol-position:bottom right;width:22px;border:0;background:transparent;}}
            QDialog#flickdockSettings QSpinBox::up-arrow, QDialog#flickdockSettings QSpinBox::down-arrow {{image:none;width:0;height:0;}}
            QDialog#flickdockSettings QSlider::groove:horizontal {{height:4px;background:{edge};border-radius:2px;}}
            QDialog#flickdockSettings QSlider::sub-page:horizontal {{background:#2584ed;border-radius:2px;}}
            QDialog#flickdockSettings QSlider::handle:horizontal {{width:16px;margin:-6px 0;border-radius:8px;background:{card};border:1px solid #75a4dc;}}
            QDialog#flickdockSettings QSlider::handle:horizontal:focus {{border:2px solid #2584ed;}}
            QDialog#flickdockSettings QComboBox QAbstractItemView {{background:{card};color:{ink};selection-background-color:{hover};selection-color:{ink};border:1px solid {edge};padding:5px;}}
            QDialog#flickdockSettings QComboBox::drop-down {{border:0;width:28px;background:transparent;}}
            QDialog#flickdockSettings QComboBox::down-arrow {{image:none;}}
            QDialog#flickdockSettings QPushButton {{background:{card};color:{ink};border:1px solid {edge};border-radius:8px;padding:5px 12px;font-size:13px;}}
            QDialog#flickdockSettings QPushButton:hover {{background:{hover};border-color:#75a4dc;}}
            QDialog#flickdockSettings QPushButton:pressed {{background:#d0e4ff;color:#19395e;}}
            QDialog#flickdockSettings QPushButton:disabled {{color:{secondary};background:{field};border-color:{edge};}}
            QDialog#flickdockSettings QPushButton#settingsSave {{background:#2584ed;color:white;border-color:#2584ed;}}
            QDialog#flickdockSettings QPushButton#settingsSave:hover {{background:#3694fa;}}
            QDialog#flickdockSettings QPushButton#settingsSave:pressed {{background:#176ac4;}}
            QDialog#flickdockSettings QPushButton#settingsAuthor {{background:transparent;color:{secondary};border:0;padding:5px 0;}}
            QDialog#flickdockSettings QPushButton#settingsAuthor:hover {{color:{ink};}}
            QDialog#flickdockSettings QPushButton#settingsClose {{background:transparent;border:0;border-radius:10px;padding:0;}}
            QDialog#flickdockSettings QPushButton#settingsClose:hover {{background:{hover};}}
            QDialog#flickdockSettings QPushButton#settingsClose:pressed {{background:{edge};}}
            QDialog#flickdockSettings QPushButton#settingsClose:focus {{border:1px solid #5d9eeb;}}
            QDialog#flickdockSettings QCheckBox {{spacing:10px;}}
            QDialog#flickdockSettings QCheckBox::indicator {{width:17px;height:17px;border:1px solid {edge};border-radius:5px;background:{field};}}
            QDialog#flickdockSettings QCheckBox::indicator:checked {{background:#2584ed;border-color:#2584ed;}}
            QDialog#flickdockSettings QLabel#settingsError {{color:{'#ff9b9b' if dark else '#b9404c'};font-size:12px;}}
        """)
        if self.license_dialog is not None:
            self.license_dialog.apply_theme(theme)
        if self.author_dialog is not None:
            self.author_dialog.apply_theme(theme)
        if self._account_panel is not None and hasattr(self._account_panel, "apply_theme"):
            self._account_panel.apply_theme(theme)
