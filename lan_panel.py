"""Small embeddable LAN transfer panel, with explicit incoming consent."""
from __future__ import annotations

from pathlib import Path
import time

from PySide6.QtCore import QByteArray, QRectF, Qt, Signal
from PySide6.QtGui import QFont, QPainter
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import (QComboBox, QFileDialog, QFrame, QHBoxLayout, QLabel,
                               QProgressBar, QPushButton, QSizePolicy, QVBoxLayout, QWidget)


def _label(value='', parent=None, *, muted=False):
    label = QLabel(value, parent); label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True); label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
    if muted: label.setObjectName('lanMuted')
    return label


def _button(text, parent=None):
    button = QPushButton(text, parent); button.setMinimumHeight(34)
    button.setAutoDefault(False); button.setDefault(False)
    return button


def _size(value):
    value = float(value)
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if value < 1024 or unit == 'TB': return f'{value:.1f} {unit}'
        value /= 1024


class _DevicesCombo(QComboBox):
    def wheelEvent(self, event): event.ignore()


class _CancelButton(QPushButton):
    def __init__(self, parent=None):
        super().__init__(parent); self.setFixedSize(32, 32); self.dark = False
        self.setToolTip('取消传输'); self.setAccessibleName('取消传输')
        self.setAutoDefault(False); self.setDefault(False)

    def paintEvent(self, event):
        super().paintEvent(event)
        color = '#b3c1d4' if self.dark else '#66778e'
        svg = f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 20 20"><path d="m6 6 8 8m0-8-8 8" fill="none" stroke="{color}" stroke-width="1.5" stroke-linecap="round"/></svg>'
        painter = QPainter(self)
        QSvgRenderer(QByteArray(svg.encode())).render(painter, QRectF(8, 8, 16, 16))


class LanPanel(QFrame):
    """Mount in Settings/account. start() remains the owner's explicit decision.

    ``set_paths_provider(callback)`` optionally supplies selected shelf paths.
    If none are selected, the send button uses Windows/macOS native file picker.
    No received file is imported into the shelf automatically by this component;
    the owner can connect ``received_paths`` to the shelf's normal intake path.
    """
    received_paths = Signal(list)

    def __init__(self, service, parent=None, *, theme='light'):
        super().__init__(parent)
        self.service = service; self._paths_provider = None; self._rows = {}; self._offers = {}
        self._dark = False; self._samples = {}; self._started = False
        self.setObjectName('lanPanel'); self.setFont(QFont('HarmonyOS Sans SC', 11))
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        layout = QVBoxLayout(self); layout.setContentsMargins(16, 14, 16, 14); layout.setSpacing(10)
        heading = QHBoxLayout(); title = _label('局域网互传', self); title.setObjectName('lanTitle')
        heading.addWidget(title, 1)
        self.peer_count = _label('0 台设备', self, muted=True)
        self.peer_count.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        heading.addWidget(self.peer_count); layout.addLayout(heading)
        self.description = _label('同一网络内加密直传原文件，不占用云空间。接收前请核对两端的确认码。', self, muted=True)
        layout.addWidget(self.description)
        actions = QHBoxLayout(); actions.setSpacing(8)
        self.devices = _DevicesCombo(self); self.devices.setMinimumHeight(36); self.devices.setMinimumWidth(0)
        self.devices.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.devices.setMinimumContentsLength(10); self.devices.setMaxVisibleItems(8)
        actions.addWidget(self.devices, 1)
        self.send_button = _button('发送文件', self); self.send_button.clicked.connect(self._send)
        actions.addWidget(self.send_button); layout.addLayout(actions)
        self.transfers_body = QWidget(self); self.transfers_layout = QVBoxLayout(self.transfers_body)
        self.transfers_layout.setContentsMargins(0, 0, 0, 0); self.transfers_layout.setSpacing(8)
        layout.addWidget(self.transfers_body)
        self.status_label = _label('正在发现附近的 FlickDock…', self, muted=True)
        layout.addWidget(self.status_label)
        self.service.peers_changed.connect(self._peers_changed)
        self.service.offer_received.connect(self._offer)
        self.service.progress.connect(self._progress)
        self.service.completed.connect(self._completed)
        self.service.status_changed.connect(self.status_label.setText)
        self._peers_changed(self.service.peers()); self.apply_theme(theme)

    def set_paths_provider(self, callback): self._paths_provider = callback

    def apply_theme(self, theme):
        self._dark = theme == 'dark'
        color = '#edf2fa' if self._dark else '#172537'
        muted = '#a4b3c7' if self._dark else '#697b93'
        background = '#272e38' if self._dark else '#f8fafd'
        border = '#3a4554' if self._dark else '#e1e7f0'
        field = '#202630' if self._dark else '#ffffff'
        self.setStyleSheet(f'''
            QFrame#lanPanel {{ background:{background}; border:1px solid {border}; border-radius:16px; }}
            QFrame#lanPanel QLabel {{ color:{color}; background:transparent; border:0; }}
            QFrame#lanPanel QLabel#lanTitle {{ font-weight:600; }}
            QFrame#lanPanel QLabel#lanMuted {{ color:{muted}; }}
            QFrame#lanPanel QPushButton {{ color:{color}; background:{field}; border:1px solid {border}; border-radius:9px; padding:5px 12px; }}
            QFrame#lanPanel QPushButton:hover {{ border-color:#79a5e8; }}
            QFrame#lanPanel QPushButton:pressed {{ background:{border}; }}
            QFrame#lanPanel QPushButton:disabled {{ color:{muted}; }}
            QFrame#lanPanel QComboBox {{ color:{color}; background:{field}; border:1px solid {border}; border-radius:9px; padding:4px 10px; }}
            QFrame#lanPanel QComboBox QAbstractItemView {{ background:{field}; color:{color}; selection-background-color:#286dd0; selection-color:white; }}
            QFrame#lanPanel QProgressBar {{ border:0; background:{border}; height:5px; max-height:5px; border-radius:2px; }}
            QFrame#lanPanel QProgressBar::chunk {{ background:#3387f5; border-radius:2px; }}
        ''')
        for row in self._rows.values(): row['cancel'].dark = self._dark; row['cancel'].update()

    def _peers_changed(self, peers):
        selected = self.devices.currentData(); self.devices.clear()
        for peer in peers: self.devices.addItem(f"{peer['name']} · {peer['host']}", peer['id'])
        if selected is not None:
            index = self.devices.findData(selected)
            if index >= 0: self.devices.setCurrentIndex(index)
        self.peer_count.setText(f'{len(peers)} 台设备')
        if not peers: self.devices.addItem('未发现在线设备', '')
        self.send_button.setEnabled(bool(peers))

    def _send(self):
        peer = self.devices.currentData()
        if not peer: return
        paths = list(self._paths_provider() or []) if self._paths_provider else []
        if not paths:
            paths, _ = QFileDialog.getOpenFileNames(self, '选择要发送的文件')
        if not paths: return
        try: self.service.send(peer, paths)
        except (ValueError, OSError) as error: self.status_label.setText(str(error))

    def send_paths(self, peer_id, paths):
        """Overview/context-menu entry can send selected files and folders."""
        try: return self.service.send(peer_id, paths)
        except (ValueError, OSError) as error: self.status_label.setText(str(error)); return ''

    def _row(self, identifier, peer='', direction='send'):
        if identifier in self._rows: return self._rows[identifier]
        frame = QWidget(self.transfers_body); outer = QVBoxLayout(frame); outer.setContentsMargins(0, 5, 0, 5); outer.setSpacing(5)
        heading = QHBoxLayout(); title = _label(('发送至 ' if direction == 'send' else '接收自 ') + peer, frame)
        heading.addWidget(title, 1); cancel = _CancelButton(frame); cancel.dark = self._dark
        cancel.clicked.connect(lambda: self.service.cancel(identifier)); heading.addWidget(cancel); outer.addLayout(heading)
        detail = _label('', frame, muted=True); outer.addWidget(detail)
        bar = QProgressBar(frame); bar.setRange(0, 1000); bar.setTextVisible(False); outer.addWidget(bar)
        self.transfers_layout.addWidget(frame)
        row = {'frame': frame, 'title': title, 'detail': detail, 'bar': bar, 'cancel': cancel, 'extra': None}
        self._rows[identifier] = row
        # Keep completed history small without removing active/cancellable rows.
        if len(self._rows) > 12:
            for old_id, old in list(self._rows.items()):
                if old_id != identifier and old.get('finished'):
                    old['frame'].deleteLater(); del self._rows[old_id]; break
        return row

    def _offer(self, offer):
        self.show_offer(offer)

    def show_offer(self, offer):
        """Replay an offer emitted before this optional panel was mounted."""
        identifier = offer['id']; self._offers[identifier] = offer
        if identifier in self._rows:return
        row = self._row(identifier, offer['peer'], 'receive')
        row['title'].setText(f"{offer['peer']} 想发送 {offer['count']} 个文件")
        names = '、'.join(offer.get('names', [])[:3]); names = names[:90] + ('…' if len(names) > 90 else '')
        row['detail'].setText(f"{names}\n{_size(offer['total'])} · 确认码 {offer['code']}，请与发送方核对")
        row['bar'].hide(); row['cancel'].hide()
        actions = QWidget(row['frame']); line = QHBoxLayout(actions); line.setContentsMargins(0, 0, 0, 0)
        refuse = _button('拒绝', actions); accept = _button('接收…', actions)
        line.addStretch(1); line.addWidget(refuse); line.addWidget(accept)
        refuse.clicked.connect(lambda: self._reject(identifier)); accept.clicked.connect(lambda: self._accept(identifier))
        row['frame'].layout().addWidget(actions); row['extra'] = actions
        self.status_label.setText('有新的接收请求。')

    def _accept(self, identifier):
        folder = QFileDialog.getExistingDirectory(self, '选择接收位置', str(Path.home() / 'Downloads'))
        if not folder: return
        try:
            if self.service.accept(identifier, folder):
                row = self._rows[identifier]; row['extra'].hide(); row['bar'].show(); row['cancel'].show()
                row['detail'].setText('已确认接收，正在建立直连…')
        except (ValueError, OSError) as error: self.status_label.setText(str(error))

    def _reject(self, identifier):
        self.service.reject(identifier)
        row = self._rows[identifier]
        if row.get('extra'): row['extra'].hide()
        row['detail'].setText('已拒绝接收'); row['finished'] = True

    def _progress(self, data):
        identifier = data['id']; row = self._row(identifier, data.get('peer', ''), data['direction'])
        state = data['state']; now = time.monotonic(); received = data.get('received', 0); total = data.get('total', 0)
        if state == 'transferring':
            previous = self._samples.get(identifier, (now, received))
            elapsed = now - previous[0]
            speed = (received - previous[1]) / elapsed if elapsed > .01 and received >= previous[1] else 0
            self._samples[identifier] = (now, received)
            fraction = min(1000, int(received * 1000 / total)) if total else 0
            row['bar'].setRange(0, 1000); row['bar'].setValue(fraction)
            name = str(data.get('name') or ''); name = name[:48] + ('…' if len(name) > 48 else '')
            row['detail'].setText(f'{name} · {_size(received)} / {_size(total)}' + (f' · {_size(speed)}/s' if speed else ''))
        elif state == 'waiting':
            row['bar'].setRange(0, 0)
            row['detail'].setText(f"等待接收方确认 · 确认码 {data.get('code', '')}")
        elif state in ('preparing', 'reconnecting'):
            row['bar'].setRange(0, 0); row['detail'].setText('正在准备传输…' if state == 'preparing' else '连接中断，正在续传…')
        elif state == 'interrupted':
            row['detail'].setText('连接中断，等待发送方续传…'); row['bar'].setRange(0, 0)
        elif state in ('cancelled', 'error', 'complete'):
            row['finished'] = True; row['cancel'].hide()
            if row.get('extra'): row['extra'].hide()
            row['bar'].setRange(0, 1000); row['bar'].setValue(1000 if state == 'complete' else 0)
            row['detail'].setText('传输完成 · 原文件已校验' if state == 'complete' else ('已取消传输' if state == 'cancelled' else data.get('message', '传输失败')))
        self.status_label.setText('局域网加密直传，不占用云空间。')

    def _completed(self, result):
        if result.get('direction') == 'receive': self.received_paths.emit(list(result.get('paths') or []))
