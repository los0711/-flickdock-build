"""Presentation for update checks/downloads; no network or installation here."""
from __future__ import annotations

import sys

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (QDialog, QFrame, QGraphicsDropShadowEffect, QHBoxLayout, QLabel, QPlainTextEdit,
                               QProgressBar, QPushButton, QSizePolicy, QVBoxLayout)

from app_settings import SettingsCloseButton, SettingsTitleBar
from platform_native import remove_host_window_shadow
from native_ui_common import colors as ui_colors, reference_font


def _plain(value, fallback=""):
    return value if isinstance(value, str) else fallback


def _bytes(value):
    value = max(0, int(value))
    if value >= 1024 * 1024: return f"{value / (1024 * 1024):.1f} MB"
    if value >= 1024: return f"{value / 1024:.0f} KB"
    return f"{value} B"


class _ElidedLabel(QLabel):
    def __init__(self,text='',parent=None):
        super().__init__(parent);self._full_text=''
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setSizePolicy(QSizePolicy.Policy.Ignored,QSizePolicy.Policy.Preferred)
        self.setMinimumWidth(0);self.setText(text)

    def setText(self,text):
        self._full_text=str(text);self.setAccessibleName(self._full_text)
        self._fit()

    def resizeEvent(self,event):
        super().resizeEvent(event);self._fit()

    def _fit(self):
        super().setText(self.fontMetrics().elidedText(self._full_text,Qt.TextElideMode.ElideRight,max(1,self.contentsRect().width())))


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
        self.setFont(QFont(reference_font().family(), 11))
        self.setModal(False)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setMinimumSize(452, 222)
        self.resize(452, 272)
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
        root.setContentsMargins(20,20,20,20)
        root.setSpacing(16)
        self.title_bar = SettingsTitleBar(self)
        self.title_bar.setFixedHeight(48)
        header = QHBoxLayout(self.title_bar)
        header.setContentsMargins(0,0,0,12);header.setSpacing(10)
        self.update_symbol=QLabel('↑',self.title_bar)
        self.update_symbol.setObjectName('updateSymbol');self.update_symbol.setFixedSize(32,32)
        self.update_symbol.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.update_symbol.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        header.addWidget(self.update_symbol)
        captions=QVBoxLayout();captions.setSpacing(2)
        self.title_label=_ElidedLabel('软件更新',self.title_bar);self.title_label.setObjectName('updateTitle')
        self.title_label.setTextFormat(Qt.TextFormat.PlainText)
        self.title_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        captions.addWidget(self.title_label)
        self.version_summary=_ElidedLabel('',self.title_bar);self.version_summary.setObjectName('updateCaption')
        self.version_summary.setTextFormat(Qt.TextFormat.PlainText)
        self.version_summary.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        captions.addWidget(self.version_summary)
        header.addLayout(captions,1)
        self.close_button=SettingsCloseButton(self.title_bar,glyph_size=10)
        self.close_button.setObjectName('updateClose');self.close_button.setFixedSize(24,24)
        self.close_button.setAccessibleName('关闭软件更新');self.close_button.setToolTip('关闭')
        self.close_button.clicked.connect(self.reject);header.addWidget(self.close_button)
        root.addWidget(self.title_bar)

        # Keep the same public version labels for the existing state binding;
        # the reference presents their values in the compact header.
        self.version_card=QFrame(self.surface);self.version_card.hide()
        self.current_version_label=QLabel(self.version_card)
        self.latest_version_label=QLabel(self.version_card)
        for label in (self.current_version_label,self.latest_version_label):
            label.setTextFormat(Qt.TextFormat.PlainText)
        self.notes_card=QFrame(self.surface);self.notes_card.setObjectName('updateNotesCard')
        notes=QVBoxLayout(self.notes_card);notes.setContentsMargins(12,12,12,12);notes.setSpacing(5)
        self.notes_heading=QLabel('更新内容：',self.notes_card);self.notes_heading.setObjectName('updateNotesHeading')
        notes.addWidget(self.notes_heading)
        self.release_notes=QPlainTextEdit(self.notes_card)
        self.release_notes.setObjectName('updateNotes');self.release_notes.setReadOnly(True)
        self.release_notes.setUndoRedoEnabled(False);self.release_notes.setAccessibleName('更新说明')
        self.release_notes.document().setDocumentMargin(0)
        self.release_notes.setMinimumHeight(40);self.release_notes.setMaximumHeight(112)
        notes.addWidget(self.release_notes)
        root.addWidget(self.notes_card)

        self.progress_area=QFrame(self.surface);self.progress_area.setObjectName('updateProgressArea')
        progress=QVBoxLayout(self.progress_area);progress.setContentsMargins(0,0,0,0);progress.setSpacing(6)
        status_row=QHBoxLayout();status_row.setSpacing(8)
        self.status_label=QLabel(self.progress_area);self.status_label.setObjectName('updateStatus')
        self.status_label.setTextFormat(Qt.TextFormat.PlainText);status_row.addWidget(self.status_label,1)
        self.progress_label=QLabel(self.progress_area);self.progress_label.setObjectName('updateProgressLabel')
        self.progress_label.setTextFormat(Qt.TextFormat.PlainText);status_row.addWidget(self.progress_label)
        progress.addLayout(status_row)
        self.message_label=QLabel(self.progress_area);self.message_label.setObjectName('updateMessage')
        self.message_label.setWordWrap(True);self.message_label.setTextFormat(Qt.TextFormat.PlainText)
        progress.addWidget(self.message_label)
        self.progress_bar=QProgressBar(self.progress_area);self.progress_bar.setObjectName('updateProgress')
        self.progress_bar.setTextVisible(False);self.progress_bar.setFixedHeight(6)
        self.progress_bar.setAccessibleName('更新下载进度');progress.addWidget(self.progress_bar)
        self.sha_label=_ElidedLabel('',self.progress_area);self.sha_label.setObjectName('updateSha')
        self.sha_label.setTextFormat(Qt.TextFormat.PlainText)
        self.sha_label.setSizePolicy(QSizePolicy.Policy.Ignored,QSizePolicy.Policy.Preferred)
        progress.addWidget(self.sha_label)
        root.addWidget(self.progress_area)

        footer=QHBoxLayout();footer.setSpacing(10)
        self.skip_button=QPushButton('跳过此版本',self.surface);self.skip_button.setObjectName('updateSkip')
        self.skip_button.setFixedHeight(28);self.skip_button.setAutoDefault(False)
        self.skip_button.clicked.connect(self._skip);footer.addWidget(self.skip_button)
        footer.addStretch()
        self.secondary_button=QPushButton(self.surface);self.secondary_button.setFixedHeight(28)
        self.secondary_button.setMinimumWidth(56);self.secondary_button.setAutoDefault(False)
        self.secondary_button.clicked.connect(self._secondary_clicked);footer.addWidget(self.secondary_button)
        self.primary_button=QPushButton(self.surface);self.primary_button.setObjectName('updatePrimary')
        self.primary_button.setFixedHeight(28);self.primary_button.setMinimumWidth(112)
        self.primary_button.setAutoDefault(False);self.primary_button.setDefault(False)
        self.primary_button.clicked.connect(self._primary_clicked);footer.addWidget(self.primary_button)
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
            "available": ("发现新版本", "查看更新说明，准备好后即可下载。", "立即下载并更新", "稍后"),
            "latest": ("已是最新版本", "当前安装的版本已是最新版本。", "重新检查", "关闭"),
            "error": ("暂时无法完成更新", self._error or "请稍后重试。", {"check":"重试", "download":"重试下载", "install":"重试安装"}.get(self._retry_action, "重试"), "关闭"),
            "downloading": ("正在下载更新", "下载完成后即可安装新版本。", "正在下载…", "取消下载"),
            "ready": ("更新已下载", "安装前请保存正在进行的工作。", "正在打开安装程序…" if self._install_requested else "立即安装", "稍后"),
            "cancelling": ("正在取消…", "正在结束当前更新任务。", "正在取消…", "正在取消…"),
            "cancelled": ("已取消", "你可以随时重新检查更新。", "重新检查", "关闭"),
        }[state]
        self.current_version_label.setText(self._current_version)
        self.latest_version_label.setText(self._latest_version or (self._current_version if state=="latest" else "—"))
        self.title_label.setText(f"发现新版本 FlickDock v{self._latest_version}" if state=='available' and self._latest_version else 'FlickDock 软件更新')
        self.version_summary.setText(f"当前版本 v{self._current_version} · 覆盖安装保留搁架与设置")
        self.status_label.setText(status)
        self.message_label.setText(message if len(message)<=300 else message[:299]+"…")
        self.primary_button.setText(primary)
        self.primary_button.setEnabled(state not in ("checking", "downloading", "cancelling")
            and not (state=="ready" and (self._install_requested or not self._verified_path)))
        self.secondary_button.setText(secondary)
        self.secondary_button.setEnabled(state!="cancelling")
        self.skip_button.setVisible(state=="available" and bool(self._latest_version))
        notes_visible = state in ("available", "downloading", "ready")
        self.notes_card.setVisible(notes_visible)
        self.notes_heading.setVisible(notes_visible)
        self.release_notes.setVisible(notes_visible)
        text = self._notes or "此版本未提供更新说明。"
        if self.release_notes.toPlainText()!=text: self.release_notes.setPlainText(text)
        line_count=max(2,min(6,len(text.splitlines())))
        self.release_notes.setFixedHeight(max(40,line_count*self.release_notes.fontMetrics().height()+8))
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
        # The HTML's compact offer puts versions in the header and presents
        # progress only when a real update operation is running.
        self.progress_area.setVisible(state!='available')
        self.message_label.setVisible(state not in ('downloading','checking','cancelling'))
        checksum=''
        if isinstance(self._manifest,dict):
            installer=self._manifest.get('installer')
            if isinstance(installer,dict):checksum=_plain(installer.get('sha256'))
        self.sha_label.setText('SHA-256: '+checksum)
        self.sha_label.setToolTip('SHA-256: '+checksum)
        self.sha_label.setVisible(bool(checksum) and state in ('downloading','ready'))
        self.surface.layout().activate()
        self.layout().activate()
        target_height=max(222,self.minimumSizeHint().height())
        if self.height()!=target_height:self.resize(self.width(),target_height)

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
        dark=theme=='dark';c=ui_colors(theme)
        family=reference_font().family()
        font=QFont(family);font.setPixelSize(12);self.setFont(font)
        self.close_button.dark=dark;self.close_button.update()
        self.setStyleSheet(f"""
            QDialog#flickdockUpdate {{background:transparent;color:{c['text']};}}
            QFrame#updateSurface {{background:{c['surface']};border:1px solid {c['glass_border']};border-radius:{c['radius']}px;}}
            QWidget#settingsTitleBar {{background:transparent;border:0;border-bottom:1px solid {c['divider']};}}
            QDialog#flickdockUpdate QLabel {{background:transparent;color:{c['text']};font-size:12px;border:0;}}
            QDialog#flickdockUpdate QLabel#updateSymbol {{background:rgba(10,132,255,51);color:{c['blue']};border-radius:12px;font-size:20px;font-weight:700;}}
            QDialog#flickdockUpdate QLabel#updateTitle {{font-size:14px;font-weight:700;}}
            QDialog#flickdockUpdate QLabel#updateCaption {{color:{c['secondary']};font-size:11px;}}
            QDialog#flickdockUpdate QLabel#updateNotesHeading {{font-size:12px;font-weight:700;}}
            QDialog#flickdockUpdate QLabel#updateStatus {{font-size:12px;font-weight:700;}}
            QDialog#flickdockUpdate QLabel#updateProgressLabel {{font-size:11px;color:{c['cyan'] if dark else c['blue']};font-family:monospace;}}
            QDialog#flickdockUpdate QLabel#updateMessage {{color:{c['secondary']};font-size:12px;}}
            QDialog#flickdockUpdate QLabel#updateSha {{color:{c['muted']};font-size:11px;font-family:monospace;}}
            QFrame#updateNotesCard {{background:{c['card']};border:1px solid {c['border']};border-radius:12px;}}
            QPlainTextEdit#updateNotes {{background:transparent;color:{c['secondary']};border:0;padding:0;font-size:12px;selection-background-color:{c['blue']};selection-color:white;}}
            QProgressBar#updateProgress {{background:{c['control']};border:0;border-radius:3px;}}
            QProgressBar#updateProgress::chunk {{background:{c['green']};border-radius:3px;}}
            QDialog#flickdockUpdate QPushButton {{background:{c['control']};color:{c['text']};border:1px solid transparent;border-radius:12px;padding:0 12px;font-size:12px;}}
            QDialog#flickdockUpdate QPushButton:hover {{background:{c['hover']};}}
            QDialog#flickdockUpdate QPushButton:pressed {{background:{c['selected']};}}
            QDialog#flickdockUpdate QPushButton#updatePrimary {{background:{c['blue']};color:white;font-weight:700;}}
            QDialog#flickdockUpdate QPushButton#updatePrimary:hover {{background:rgba(10,132,255,230);}}
            QDialog#flickdockUpdate QPushButton:disabled {{background:{c['control']};color:{c['muted']};}}
            QDialog#flickdockUpdate QPushButton#updateSkip {{background:transparent;border:0;color:{c['secondary']};padding:0;}}
            QDialog#flickdockUpdate QPushButton#updateClose {{background:transparent;border:0;border-radius:8px;padding:0;}}
            QDialog#flickdockUpdate QPushButton#updateClose:hover {{background:{c['hover']};}}
            QDialog#flickdockUpdate QScrollBar:vertical {{width:6px;background:transparent;border:0;}}
            QDialog#flickdockUpdate QScrollBar::handle:vertical {{background:rgba(142,142,147,71);border-radius:3px;min-height:24px;}}
            QDialog#flickdockUpdate QScrollBar::add-line:vertical,QDialog#flickdockUpdate QScrollBar::sub-line:vertical {{height:0;}}
            QDialog#flickdockUpdate QScrollBar::add-page:vertical,QDialog#flickdockUpdate QScrollBar::sub-page:vertical {{background:transparent;}}
        """)
        self.surface.graphicsEffect().setColor(QColor(0,0,0,140) if dark else QColor(15,23,42,41))
