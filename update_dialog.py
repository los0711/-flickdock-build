"""Presentation for update checks/downloads; no network or installation here."""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (QDialog, QFrame, QGraphicsDropShadowEffect, QHBoxLayout, QLabel, QPlainTextEdit,
                               QProgressBar, QPushButton, QVBoxLayout)

from app_settings import SettingsCloseButton, SettingsTitleBar
from platform_native import remove_host_window_shadow


def _plain(value, fallback=""):
    return value if isinstance(value, str) else fallback


def _bytes(value):
    value = max(0, int(value))
    if value >= 1024 * 1024: return f"{value / (1024 * 1024):.1f} MB"
    if value >= 1024: return f"{value / 1024:.0f} KB"
    return f"{value} B"


class UpdateDialog(QDialog):
    """A reusable, nonmodal update view controlled by the caller.

    bind_client() accepts UpdateClient's checking/update_available/up_to_date/
    failed/progress/verified_file/cancelled signals. Only user activation emits
    request_install; receiving a verified download never installs or opens UI.
    The caller decides when automatic results deserve a visible notification.
    """
    request_check = Signal()
    request_download = Signal()
    request_install = Signal()
    cancel_requested = Signal()
    skipped = Signal(str)
    dismissed = Signal()

    STATES = frozenset(("idle", "checking", "available", "latest", "error",
                        "downloading", "ready", "cancelling", "cancelled"))

    def __init__(self, current_version: str, parent=None, *, theme="light"):
        super().__init__(parent, Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint)
        self.setObjectName("flickdockUpdate")
        self.setWindowTitle("FlickDock · 软件更新")
        self.setFont(QFont("HarmonyOS Sans SC", 11))
        self.setModal(False)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setMinimumSize(492, 412)
        self.resize(532, 512)
        self._current_version = _plain(current_version, "—")
        self._latest_version = ""
        self._notes = ""
        self._manifest = None
        self._verified_path = ""
        self._received = self._total = 0
        self._state = "idle"
        self._error = ""
        self._retry_action = "check"
        self._cancel_pending = False
        self._install_requested = False
        self._dismissed = False
        self._client = None
        self._bindings = []

        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 16, 16, 16)
        self.surface = QFrame(self)
        self.surface.setObjectName('updateSurface')
        shadow = QGraphicsDropShadowEffect(self.surface)
        shadow.setBlurRadius(24)
        shadow.setOffset(0, 6)
        shadow.setColor(QColor(15, 28, 48, 78))
        self.surface.setGraphicsEffect(shadow)
        outer.addWidget(self.surface)
        root = QVBoxLayout(self.surface)
        root.setContentsMargins(24, 16, 24, 22)
        root.setSpacing(16)
        self.title_bar = SettingsTitleBar(self)
        header = QHBoxLayout(self.title_bar)
        header.setContentsMargins(0, 0, 0, 0)
        title = QLabel("软件更新", self.title_bar)
        title.setObjectName("updateTitle")
        title.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        header.addWidget(title, 1)
        self.close_button = SettingsCloseButton(self.title_bar)
        self.close_button.setObjectName("updateClose")
        self.close_button.setAccessibleName("关闭软件更新")
        self.close_button.setToolTip("关闭")
        self.close_button.clicked.connect(self.reject)
        header.addWidget(self.close_button)
        root.addWidget(self.title_bar)

        self.version_card = QFrame(self)
        self.version_card.setObjectName("updateVersions")
        versions = QHBoxLayout(self.version_card)
        versions.setContentsMargins(16, 14, 16, 14)
        versions.setSpacing(24)
        for label, attribute in (("当前版本", "current_version_label"), ("最新版本", "latest_version_label")):
            column = QVBoxLayout()
            column.setSpacing(5)
            caption = QLabel(label, self.version_card)
            caption.setObjectName("updateCaption")
            column.addWidget(caption)
            value = QLabel(self.version_card)
            value.setObjectName("updateVersionValue")
            value.setTextFormat(Qt.TextFormat.PlainText)
            setattr(self, attribute, value)
            column.addWidget(value)
            versions.addLayout(column, 1)
        root.addWidget(self.version_card)

        self.status_label = QLabel(self)
        self.status_label.setObjectName("updateStatus")
        self.status_label.setTextFormat(Qt.TextFormat.PlainText)
        root.addWidget(self.status_label)
        self.message_label = QLabel(self)
        self.message_label.setObjectName("updateMessage")
        self.message_label.setWordWrap(True)
        self.message_label.setTextFormat(Qt.TextFormat.PlainText)
        root.addWidget(self.message_label)
        self.progress_bar = QProgressBar(self)
        self.progress_bar.setObjectName("updateProgress")
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setFixedHeight(6)
        self.progress_bar.setAccessibleName("更新下载进度")
        root.addWidget(self.progress_bar)
        self.progress_label = QLabel(self)
        self.progress_label.setObjectName("updateCaption")
        self.progress_label.setTextFormat(Qt.TextFormat.PlainText)
        root.addWidget(self.progress_label)
        self.notes_heading = QLabel("更新说明", self)
        self.notes_heading.setObjectName("updateCaption")
        root.addWidget(self.notes_heading)
        self.release_notes = QPlainTextEdit(self)
        self.release_notes.setObjectName("updateNotes")
        self.release_notes.setReadOnly(True)
        self.release_notes.setUndoRedoEnabled(False)
        self.release_notes.setAccessibleName("更新说明")
        self.release_notes.setMinimumHeight(90)
        self.release_notes.setMaximumHeight(180)
        root.addWidget(self.release_notes, 1)
        root.addStretch()

        footer = QHBoxLayout()
        footer.setSpacing(10)
        self.skip_button = QPushButton("跳过此版本", self)
        self.skip_button.setObjectName("updateSkip")
        self.skip_button.setMinimumHeight(38)
        self.skip_button.setAutoDefault(False)
        self.skip_button.clicked.connect(self._skip)
        footer.addWidget(self.skip_button)
        footer.addStretch()
        self.secondary_button = QPushButton(self)
        self.secondary_button.setMinimumSize(82, 38)
        self.secondary_button.setAutoDefault(False)
        self.secondary_button.clicked.connect(self._secondary_clicked)
        footer.addWidget(self.secondary_button)
        self.primary_button = QPushButton(self)
        self.primary_button.setObjectName("updatePrimary")
        self.primary_button.setMinimumSize(112, 38)
        self.primary_button.setAutoDefault(False)
        self.primary_button.setDefault(False)
        self.primary_button.clicked.connect(self._primary_clicked)
        footer.addWidget(self.primary_button)
        root.addLayout(footer)
        self.request_check.connect(self._check_client)
        self.request_download.connect(self._download_client)
        self.cancel_requested.connect(self._cancel_client)
        self.apply_theme(theme)
        self._render_state()

    @property
    def state(self): return self._state

    @property
    def current_version(self): return self._current_version

    @property
    def latest_version(self): return self._latest_version

    @property
    def verified_path(self): return self._verified_path

    @property
    def manifest(self): return dict(self._manifest) if self._manifest is not None else None

    def bind_client(self, client):
        """Bind once, or replace the old bindings without duplicate actions."""
        if client is self._client: return
        for signal, handler in self._bindings:
            try: signal.disconnect(handler)
            except (RuntimeError, TypeError): pass
        self._bindings.clear()
        self._client = client
        if client is None: return
        for name, handler in (("checking", self._on_checking), ("update_available", self._on_available),
                              ("up_to_date", self._on_latest), ("failed", self._on_failed),
                              ("progress", self._on_progress), ("verified_file", self._on_verified),
                              ("cancelled", self._on_cancelled)):
            signal = getattr(client, name)
            signal.connect(handler)
            self._bindings.append((signal, handler))

    def set_state(self, state, *, manifest=None, current_version=None, latest_version=None,
                  notes=None, error="", received_bytes=None, total_bytes=None,
                  progress=None, verified_path=None, operation=None):
        if state not in self.STATES: raise ValueError(f"Unknown update state: {state}")
        previous = self._state
        self._state = state
        if current_version is not None: self._current_version = _plain(current_version, "—")
        if manifest is not None:
            self._manifest = dict(manifest)
            self._latest_version = _plain(self._manifest.get("version"))
            self._notes = _plain(self._manifest.get("notes"))
        if latest_version is not None: self._latest_version = _plain(latest_version)
        if notes is not None: self._notes = _plain(notes)
        if received_bytes is not None: self._received = max(0, int(received_bytes))
        if total_bytes is not None: self._total = max(0, int(total_bytes))
        if progress is not None:
            self._total = 100
            self._received = max(0, min(100, int(progress)))
        if verified_path is not None: self._verified_path = str(verified_path)
        self._error = _plain(error)
        if state == "error":
            self._retry_action = operation or ("download" if previous in ("downloading", "cancelling") else "check")
        if state in ("idle", "checking", "cancelled"):
            self._verified_path = ""
        if state != "ready": self._install_requested = False
        if state in ("checking", "downloading") and previous != state:
            self._received = 0 if received_bytes is None and progress is None else self._received
            self._total = 0 if total_bytes is None and progress is None else self._total
        self._render_state()

    def show_error(self, message, *, operation="check"):
        self.set_state("error", error=message, operation=operation)

    def _render_state(self):
        state = self._state
        status, message, primary, secondary = {
            "idle": ("检查软件更新", "获取最新版本与更新说明。", "检查更新", "关闭"),
            "checking": ("正在检查更新…", "正在获取最新版本信息。", "正在检查…", "取消检查"),
            "available": ("发现新版本", "查看更新说明，准备好后即可下载。", "下载并更新", "稍后"),
            "latest": ("已是最新版本", "当前安装的版本已是最新版本。", "重新检查", "关闭"),
            "error": ("暂时无法完成更新", self._error or "请稍后重试。", {"check":"重试", "download":"重试下载", "install":"重试安装"}.get(self._retry_action, "重试"), "关闭"),
            "downloading": ("正在下载更新", "下载完成后即可安装新版本。", "正在下载…", "取消下载"),
            "ready": ("更新已下载", "安装前请保存正在进行的工作。", "正在打开安装程序…" if self._install_requested else "立即安装", "稍后"),
            "cancelling": ("正在取消…", "正在结束当前更新任务。", "正在取消…", "正在取消…"),
            "cancelled": ("已取消", "你可以随时重新检查更新。", "重新检查", "关闭"),
        }[state]
        self.current_version_label.setText(self._current_version)
        self.latest_version_label.setText(self._latest_version or (self._current_version if state=="latest" else "—"))
        self.status_label.setText(status)
        self.message_label.setText(message if len(message)<=300 else message[:299]+"…")
        self.primary_button.setText(primary)
        self.primary_button.setEnabled(state not in ("checking", "downloading", "cancelling")
            and not (state=="ready" and (self._install_requested or not self._verified_path)))
        self.secondary_button.setText(secondary)
        self.secondary_button.setEnabled(state!="cancelling")
        self.skip_button.setVisible(state=="available" and bool(self._latest_version))
        notes_visible = state in ("available", "downloading", "ready")
        self.notes_heading.setVisible(notes_visible)
        self.release_notes.setVisible(notes_visible)
        text = self._notes or "此版本未提供更新说明。"
        if self.release_notes.toPlainText()!=text: self.release_notes.setPlainText(text)
        progress_visible = state in ("checking", "downloading", "ready", "cancelling")
        self.progress_bar.setVisible(progress_visible)
        self.progress_label.setVisible(state in ("downloading", "ready"))
        if state=="ready":
            self.progress_bar.setRange(0, 100); self.progress_bar.setValue(100)
            self.progress_label.setText("下载完成")
        elif state=="downloading" and self._total>0:
            percentage = min(100, round(self._received*100/self._total))
            self.progress_bar.setRange(0, 100); self.progress_bar.setValue(percentage)
            self.progress_label.setText(f"{_bytes(self._received)} / {_bytes(self._total)} · {percentage}%")
        else:
            self.progress_bar.setRange(0, 0)
            self.progress_label.setText(f"已下载 {_bytes(self._received)}")

    def _primary_clicked(self):
        if self._state in ("idle", "latest", "cancelled") or (self._state=="error" and self._retry_action=="check"):
            self._cancel_pending = False
            self.set_state("checking")
            self.request_check.emit()
        elif self._state=="available" or (self._state=="error" and self._retry_action=="download"):
            self._cancel_pending = False
            self.set_state("downloading")
            self.request_download.emit()
        elif self._state=="ready" or (self._state=="error" and self._retry_action=="install" and self._verified_path):
            if self._install_requested or not self._verified_path: return
            self._install_requested = True
            self._state = "ready"
            self._render_state()
            self.request_install.emit()

    def _secondary_clicked(self):
        if self._state in ("checking", "downloading"): self._cancel()
        elif self._state!="cancelling": self.reject()

    def _cancel(self):
        if self._cancel_pending: return
        self._cancel_pending = True
        self.set_state("cancelling")
        self.cancel_requested.emit()

    def _skip(self):
        if self._state=="available" and self._latest_version:
            self.skipped.emit(self._latest_version)
            self.reject()

    def showEvent(self, event):
        self._dismissed = False
        super().showEvent(event)
        remove_host_window_shadow(self)

    def done(self, result):
        if self._state in ("checking", "downloading"): self._cancel()
        if not self._dismissed:
            self._dismissed = True
            self.dismissed.emit()
        super().done(result)

    def _check_client(self):
        if self._client is None: return
        try:
            if self._client.check(manual=True) is False:
                self.show_error("正在处理更新，请稍后再试。")
        except (OSError, ValueError, RuntimeError) as error:
            self.show_error(str(error))

    def _download_client(self):
        if self._client is None: return
        try:
            if self._client.download(self.manifest, manual=True) is False:
                self.show_error("正在处理更新，请稍后再试。", operation="download")
        except (OSError, ValueError, RuntimeError) as error:
            self.show_error(str(error), operation="download")

    def _cancel_client(self):
        if self._client is not None: self._client.cancel()

    def _on_checking(self, manual=False):
        self._cancel_pending = False
        self.set_state("checking")

    def _on_available(self, manifest, manual=False):
        if not self._cancel_pending: self.set_state("available", manifest=manifest)

    def _on_latest(self, manual=False):
        if not self._cancel_pending: self.set_state("latest", latest_version=self._current_version)

    def _on_failed(self, message, manual=False):
        if not self._cancel_pending: self.set_state("error", error=message)

    def _on_progress(self, received, total):
        if not self._cancel_pending:
            self.set_state("downloading", received_bytes=received, total_bytes=total)

    def _on_verified(self, path, manifest, manual=False):
        if not self._cancel_pending:
            self.set_state("ready", manifest=manifest, verified_path=path)

    def _on_cancelled(self, manual=False):
        self._cancel_pending = True
        self.set_state("cancelled")

    def apply_theme(self, theme):
        dark = theme=="dark"
        self.close_button.dark = dark
        self.close_button.update()
        bg, surface = ("#1d2027", "#272c35") if dark else ("#f7f9fc", "#ffffff")
        ink, secondary = ("#edf2fa", "#a5b1c3") if dark else ("#253246", "#637186")
        edge, hover = ("#3c4554", "#39465a") if dark else ("#dce3ed", "#edf4fe")
        self.setStyleSheet(f"""
            QDialog#flickdockUpdate {{background:transparent;color:{ink};}}
            QFrame#updateSurface {{background:{bg};border:1px solid {edge};border-radius:18px;}}
            QDialog#flickdockUpdate QLabel {{background:transparent;color:{ink};font-size:13px;}}
            QDialog#flickdockUpdate QLabel#updateTitle {{font-size:21px;font-weight:600;}}
            QDialog#flickdockUpdate QLabel#updateCaption {{color:{secondary};font-size:12px;}}
            QDialog#flickdockUpdate QLabel#updateVersionValue {{font-size:17px;font-weight:600;}}
            QDialog#flickdockUpdate QLabel#updateStatus {{font-size:15px;font-weight:600;}}
            QDialog#flickdockUpdate QLabel#updateMessage {{color:{secondary};}}
            QFrame#updateVersions {{background:{surface};border:1px solid {edge};border-radius:12px;}}
            QPlainTextEdit#updateNotes {{background:{surface};color:{ink};border:1px solid {edge};border-radius:10px;padding:8px;font-size:13px;selection-background-color:#2584ed;selection-color:white;}}
            QProgressBar#updateProgress {{background:{edge};border:0;border-radius:3px;}}
            QProgressBar#updateProgress::chunk {{background:#2584ed;border-radius:3px;}}
            QDialog#flickdockUpdate QPushButton {{background:{surface};color:{ink};border:1px solid {edge};border-radius:9px;padding:6px 12px;font-size:13px;}}
            QDialog#flickdockUpdate QPushButton:hover {{background:{hover};border-color:#75a4dc;}}
            QDialog#flickdockUpdate QPushButton:pressed {{background:{edge};}}
            QDialog#flickdockUpdate QPushButton:focus {{border:1px solid #5d9eeb;}}
            QDialog#flickdockUpdate QPushButton#updatePrimary {{background:#2584ed;color:white;border-color:#2584ed;}}
            QDialog#flickdockUpdate QPushButton#updatePrimary:hover {{background:#3694fa;}}
            QDialog#flickdockUpdate QPushButton#updatePrimary:disabled {{background:{edge};color:{secondary};border-color:{edge};}}
            QDialog#flickdockUpdate QPushButton:disabled {{background:{edge};color:{secondary};border-color:{edge};}}
            QDialog#flickdockUpdate QPushButton#updateSkip {{background:transparent;border:0;color:{secondary};padding:6px 0;}}
            QDialog#flickdockUpdate QPushButton#updateClose {{background:transparent;border:0;border-radius:10px;padding:0;}}
            QDialog#flickdockUpdate QPushButton#updateClose:hover {{background:{hover};}}
            QDialog#flickdockUpdate QScrollBar:vertical {{width:6px;background:transparent;border:0;}}
            QDialog#flickdockUpdate QScrollBar::handle:vertical {{background:{'#5a677b' if dark else '#c1ccda'};border-radius:3px;min-height:24px;}}
            QDialog#flickdockUpdate QScrollBar::add-line:vertical, QDialog#flickdockUpdate QScrollBar::sub-line:vertical {{height:0;}}
            QDialog#flickdockUpdate QScrollBar::add-page:vertical, QDialog#flickdockUpdate QScrollBar::sub-page:vertical {{background:transparent;}}
        """)
