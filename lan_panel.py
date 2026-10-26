"""Small embeddable LAN transfer panel, with explicit incoming consent."""
from __future__ import annotations

from pathlib import Path
import json
import os
import time

from PySide6.QtCore import QByteArray, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QGuiApplication, QPainter, QPen
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import (QComboBox, QDialog, QFileDialog, QFrame, QGraphicsDropShadowEffect,
                               QHBoxLayout, QLabel, QLineEdit, QProgressBar, QPushButton,
                               QScrollArea, QSizePolicy, QVBoxLayout, QWidget)


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


class _DropIllustration(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent);self.setFixedSize(36,31)

    def paintEvent(self, event):
        painter=QPainter(self);painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor('#3688ed'),1.8))
        painter.drawRoundedRect(QRectF(5,9,26,18),4,4)
        painter.drawLine(9,9,14,9);painter.drawLine(9,6,14,6)
        painter.drawLine(18,3,18,18)
        painter.drawLine(13,13,18,18);painter.drawLine(23,13,18,18)


class _DropZone(QFrame):
    dropped = Signal(list)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName('lanDropZone'); self.setAcceptDrops(True)
        self.setMinimumHeight(146)
        self._active = False
        layout = QVBoxLayout(self); layout.setContentsMargins(18, 16, 18, 14); layout.setSpacing(5)
        illustration = _DropIllustration(self)
        layout.addWidget(illustration, 0, Qt.AlignmentFlag.AlignHCenter)
        self.title = _label('拖入文件或文件夹', self); self.title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.title.setObjectName('lanDropTitle'); layout.addWidget(self.title)
        self.hint = _label('保留原文件和目录结构 · 发送前可核对设备', self, muted=True)
        self.hint.setAlignment(Qt.AlignmentFlag.AlignCenter); layout.addWidget(self.hint)


    @staticmethod
    def _paths(event):
        mime = event.mimeData()
        if not mime.hasUrls(): return []
        urls = mime.urls()
        if not urls or any(not url.isLocalFile() for url in urls): return []
        paths = [Path(url.toLocalFile()) for url in urls]
        return [str(path) for path in paths] if all(path.exists() for path in paths) else []

    def dragEnterEvent(self, event):
        if self._paths(event):
            self._active = True; self.setProperty('dragActive', True); self.style().unpolish(self); self.style().polish(self)
            event.setDropAction(Qt.DropAction.CopyAction); event.accept()
        else: event.ignore()

    def dragMoveEvent(self, event):
        if self._paths(event): event.setDropAction(Qt.DropAction.CopyAction); event.accept()
        else: event.ignore()

    def dragLeaveEvent(self, event):
        self._active = False; self.setProperty('dragActive', False); self.style().unpolish(self); self.style().polish(self)
        event.accept()

    def dropEvent(self, event):
        paths = self._paths(event)
        self.dragLeaveEvent(event)
        if paths:
            self.dropped.emit(paths)
            event.setDropAction(Qt.DropAction.CopyAction); event.accept()
        else: event.ignore()


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
    """Overview's transfer surface. start() remains the owner's explicit decision.

    ``set_paths_provider(callback)`` optionally supplies selected shelf paths.
    If none are selected, the send button uses Windows/macOS native file picker.
    No received file is imported into the shelf automatically by this component;
    the owner can connect ``received_paths`` to the shelf's normal intake path.
    """
    received_paths = Signal(list)
    transfer_started = Signal()

    def __init__(self, service, parent=None, *, theme='light', settings=None):
        super().__init__(parent)
        self.service = service; self.settings = settings; self._paths_provider = None
        self._rows = {}; self._offers = {}; self._queued_paths = []
        self._dark = False; self._samples = {}; self._started = False
        self._history_path = settings.base / 'lan_history.json' if settings else None
        self._history = self._load_history()
        self.setObjectName('lanPanel'); self.setFont(QFont('HarmonyOS Sans SC', 11))
        self.setMinimumHeight(500)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        layout = QVBoxLayout(self); layout.setContentsMargins(0, 0, 0, 0); layout.setSpacing(15)
        heading = QHBoxLayout(); title = _label('发送到附近设备', self); title.setObjectName('lanTitle')
        heading.addWidget(title, 1)
        self.peer_count = _label('0 台设备', self, muted=True)
        self.peer_count.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        heading.addWidget(self.peer_count); layout.addLayout(heading)
        self.description = _label('同一网络内加密直传原文件。接收前请核对两端的确认码。', self, muted=True)
        layout.addWidget(self.description)
        self.drop_zone = _DropZone(self); self.drop_zone.dropped.connect(self.queue_paths)
        layout.addWidget(self.drop_zone)
        pickers = QHBoxLayout(); pickers.setSpacing(8)
        self.pick_files = _button('添加文件', self); self.pick_files.clicked.connect(self._pick_files)
        self.pick_folder = _button('添加文件夹', self); self.pick_folder.clicked.connect(self._pick_folder)
        self.clear_queue = _button('清除待发送', self); self.clear_queue.clicked.connect(lambda:self.queue_paths([],replace=True))
        pickers.addWidget(self.pick_files); pickers.addWidget(self.pick_folder); pickers.addStretch(1)
        pickers.addWidget(self.clear_queue); layout.addLayout(pickers)
        actions = QHBoxLayout(); actions.setSpacing(8)
        self.devices = _DevicesCombo(self); self.devices.setMinimumHeight(36); self.devices.setMinimumWidth(0)
        self.devices.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.devices.setMinimumContentsLength(10); self.devices.setMaxVisibleItems(8)
        actions.addWidget(self.devices, 1)
        self.send_button = _button('发送', self); self.send_button.setObjectName('lanPrimary')
        self.send_button.clicked.connect(self._send)
        actions.addWidget(self.send_button); layout.addLayout(actions)
        destination = QHBoxLayout(); destination.setSpacing(8)
        destination_label=_label('接收位置', self)
        destination_label.setSizePolicy(QSizePolicy.Policy.Fixed,QSizePolicy.Policy.Preferred)
        destination_label.setFixedWidth(72)
        destination.addWidget(destination_label)
        self.receive_path = QLineEdit(self); self.receive_path.setReadOnly(True)
        self.receive_path.setAccessibleName('局域网接收位置')
        self.receive_path.setText(str(settings.lan_receive_dir if settings else Path.home()/'Downloads'))
        destination.addWidget(self.receive_path, 1)
        self.choose_destination = _button('更改…', self); self.choose_destination.clicked.connect(self._choose_destination)
        destination.addWidget(self.choose_destination); layout.addLayout(destination)
        section = _label('最近传输', self); section.setObjectName('lanSection'); layout.addWidget(section)
        self.history_label = _label('', self, muted=True); layout.addWidget(self.history_label)
        self._render_history()
        self.transfers_scroll = QScrollArea(self); self.transfers_scroll.setWidgetResizable(True)
        self.transfers_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.transfers_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.transfers_scroll.setMinimumHeight(104)
        self.transfers_body = QWidget(self); self.transfers_layout = QVBoxLayout(self.transfers_body)
        self.transfers_layout.setContentsMargins(0, 0, 0, 0); self.transfers_layout.setSpacing(8)
        self.transfers_layout.addStretch(1); self.transfers_scroll.setWidget(self.transfers_body)
        self.transfers_body.setStyleSheet('background:transparent;')
        self.transfers_scroll.viewport().setAutoFillBackground(False)
        layout.addWidget(self.transfers_scroll, 1); self.transfers_scroll.hide()
        self.status_label = _label('正在发现附近的 FlickDock…', self, muted=True)
        layout.addWidget(self.status_label)
        self.service.peers_changed.connect(self._peers_changed)
        self.service.offer_received.connect(self._offer)
        self.service.progress.connect(self._progress)
        self.service.completed.connect(self._completed)
        self.service.status_changed.connect(self.status_label.setText)
        self._peers_changed(self.service.peers()); self.queue_paths([],replace=True); self.apply_theme(theme)

    def set_paths_provider(self, callback): self._paths_provider = callback

    def _load_history(self):
        if not self._history_path: return []
        try:
            value = json.loads(self._history_path.read_text(encoding='utf-8'))
            if isinstance(value, list):
                return [row for row in value[:12] if isinstance(row, dict)
                        and all(isinstance(row.get(key), str) for key in ('name', 'peer', 'state', 'direction', 'when'))]
        except (OSError, ValueError, UnicodeError): pass
        return []

    def _save_history(self):
        if not self._history_path: return
        self._history_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._history_path.with_name('.lan-history-' + str(time.time_ns()) + '.tmp')
        try:
            with temporary.open('x', encoding='utf-8') as stream:
                json.dump(self._history[:12], stream, ensure_ascii=False)
                stream.flush(); os.fsync(stream.fileno())
            os.replace(temporary, self._history_path)
        finally:
            if temporary.exists(): temporary.unlink()

    def _render_history(self):
        if not self._history:
            self.history_label.setText('还没有传输记录')
            return
        rows = self._history[:4]
        self.history_label.setText('\n'.join(
            f"{row['when']}   {row['name'][:35]}   {'已发送' if row['direction']=='send' else '已接收'}"
            + ('' if row['state']=='完成' else ' · '+row['state']) for row in rows))

    def queue_paths(self, paths, *, replace=False):
        if replace: self._queued_paths = []
        for value in paths:
            path = Path(value)
            if path.exists() and str(path) not in self._queued_paths: self._queued_paths.append(str(path))
        count = len(self._queued_paths)
        self.drop_zone.title.setText(f'已选择 {count} 项' if count else '拖入文件或文件夹')
        self.drop_zone.hint.setText('、'.join(Path(p).name for p in self._queued_paths[:3])[:80]
            + ('…' if count > 3 else '') if count else '保留原文件和目录结构 · 发送前可核对设备')
        self.clear_queue.setVisible(bool(count))
        self.send_button.setText(f'发送 {count} 项' if count else '发送文件')

    def _pick_files(self):
        paths, _ = QFileDialog.getOpenFileNames(self, '选择要发送的文件')
        self.queue_paths(paths)

    def _pick_folder(self):
        path = QFileDialog.getExistingDirectory(self, '选择要发送的文件夹')
        if path: self.queue_paths([path])

    def _choose_destination(self):
        path = QFileDialog.getExistingDirectory(self, '选择接收位置', self.receive_path.text())
        if not path: return
        try:
            if self.settings: self.settings.set_lan_receive_dir(path)
            self.receive_path.setText(path)
        except (ValueError, OSError) as error: self.status_label.setText(str(error))

    def apply_theme(self, theme):
        self._dark = theme == 'dark'
        color = '#edf2fa' if self._dark else '#172537'
        muted = '#a4b3c7' if self._dark else '#697b93'
        background = '#272e38' if self._dark else '#f8fafd'
        border = '#3a4554' if self._dark else '#e1e7f0'
        field = '#202630' if self._dark else '#ffffff'
        accent = '#75adff' if self._dark else '#216dd2'
        self.setStyleSheet(f'''
            QFrame#lanPanel {{ background:transparent; border:0; }}
            QFrame#lanPanel QLabel {{ color:{color}; background:transparent; border:0; }}
            QFrame#lanPanel QLabel#lanTitle {{ font-size:17px; font-weight:600; }}
            QFrame#lanPanel QLabel#lanSection {{ font-size:13px; font-weight:600; }}
            QFrame#lanPanel QLabel#lanMuted {{ color:{muted}; }}
            QFrame#lanDropZone {{ background:{field}; border:1px dashed {border}; border-radius:14px; }}
            QFrame#lanDropZone[dragActive="true"] {{ background:{background}; border:2px solid {accent}; }}
            QFrame#lanDropZone QLabel#lanDropTitle {{ font-size:17px; font-weight:600; color:{accent}; }}
            QFrame#lanPanel QPushButton {{ color:{color}; background:{field}; border:1px solid {border}; border-radius:9px; padding:5px 12px; }}
            QFrame#lanPanel QPushButton#lanPrimary {{ color:white; background:#2679e9; border-color:#2679e9; font-weight:600; }}
            QFrame#lanPanel QPushButton:hover {{ border-color:{accent}; }}
            QFrame#lanPanel QPushButton:pressed {{ background:{border}; }}
            QFrame#lanPanel QPushButton:disabled {{ color:{muted}; }}
            QFrame#lanPanel QComboBox {{ color:{color}; background:{field}; border:1px solid {border}; border-radius:9px; padding:4px 10px; }}
            QFrame#lanPanel QComboBox QAbstractItemView {{ background:{field}; color:{color}; selection-background-color:#286dd0; selection-color:white; }}
            QFrame#lanPanel QLineEdit {{ color:{color}; background:{field}; border:1px solid {border}; border-radius:8px; padding:5px 9px; }}
            QFrame#lanPanel QScrollArea {{ background:transparent; border:0; }}
            QFrame#lanPanel QScrollBar:vertical {{width:5px;background:transparent;}}
            QFrame#lanPanel QScrollBar::handle:vertical {{background:{border};border-radius:2px;min-height:20px;}}
            QFrame#lanPanel QScrollBar::add-line:vertical,QFrame#lanPanel QScrollBar::sub-line:vertical {{height:0;}}
            QFrame#lanPanel QScrollBar::add-page:vertical,QFrame#lanPanel QScrollBar::sub-page:vertical {{background:transparent;}}
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
        paths = list(self._queued_paths)
        if not paths: paths = list(self._paths_provider() or []) if self._paths_provider else []
        if not paths:
            paths, _ = QFileDialog.getOpenFileNames(self, '选择要发送的文件')
        if not paths: return
        try:
            self.service.send(peer, paths)
            self.queue_paths([],replace=True)
        except (ValueError, OSError) as error: self.status_label.setText(str(error))

    def send_paths(self, peer_id, paths):
        """Overview/context-menu entry can send selected files and folders."""
        try: return self.service.send(peer_id, paths)
        except (ValueError, OSError) as error: self.status_label.setText(str(error)); return ''

    def _row(self, identifier, peer='', direction='send'):
        if identifier in self._rows: return self._rows[identifier]
        self.setMinimumHeight(580); self.transfer_started.emit()
        frame = QWidget(self.transfers_body); outer = QVBoxLayout(frame); outer.setContentsMargins(0, 5, 0, 5); outer.setSpacing(5)
        self.transfers_scroll.show()
        heading = QHBoxLayout(); title = _label(('发送至 ' if direction == 'send' else '接收自 ') + peer, frame)
        heading.addWidget(title, 1); cancel = _CancelButton(frame); cancel.dark = self._dark
        cancel.clicked.connect(lambda: self.service.cancel(identifier)); heading.addWidget(cancel); outer.addLayout(heading)
        detail = _label('', frame, muted=True); outer.addWidget(detail)
        bar = QProgressBar(frame); bar.setRange(0, 1000); bar.setTextVisible(False); outer.addWidget(bar)
        self.transfers_layout.insertWidget(0,frame)
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
        folder = self.receive_path.text()
        if not Path(folder).is_dir():
            self.status_label.setText('接收位置不可用，请先选择一个有效文件夹。'); return
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
            if not row.get('recorded'):
                row['recorded'] = True
                self._history.insert(0, {'when':time.strftime('%m-%d %H:%M'),
                    'name':str(data.get('name') or row['title'].text())[:100],
                    'peer':str(data.get('peer') or '')[:80],
                    'state':{'complete':'完成','cancelled':'已取消','error':'失败'}[state],
                    'direction':str(data.get('direction') or 'send')})
                self._history = self._history[:12]; self._render_history()
                try:self._save_history()
                except OSError:pass
        self.status_label.setText('局域网加密直传，不占用云空间。')

    def _completed(self, result):
        if result.get('direction') == 'receive': self.received_paths.emit(list(result.get('paths') or []))


class LanDialog(QDialog):
    """A dedicated layered transfer window opened from the overview."""

    def __init__(self, service, settings, parent=None):
        super().__init__(parent, Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint)
        self.setObjectName('lanDialog'); self.setWindowTitle('FlickDock · 局域网互传')
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setModal(False); self.resize(628, 640); self.setMinimumSize(500, 600)
        root = QVBoxLayout(self); root.setContentsMargins(22, 22, 22, 22)
        self.card = QFrame(self); self.card.setObjectName('lanDialogCard')
        shadow = QGraphicsDropShadowEffect(self.card)
        shadow.setBlurRadius(32); shadow.setOffset(0, 9); shadow.setColor(QColor(0, 0, 0, 90))
        self.card.setGraphicsEffect(shadow); root.addWidget(self.card)
        content = QVBoxLayout(self.card); content.setContentsMargins(24, 18, 24, 22); content.setSpacing(16)
        title_row = QHBoxLayout()
        title = _label('局域网互传', self.card); title.setObjectName('lanDialogTitle')
        title_row.addWidget(title, 1)
        close = _button('×', self.card); close.setObjectName('lanDialogClose')
        close.setFixedSize(30, 30); close.setAccessibleName('关闭局域网传输窗口')
        close.clicked.connect(self.close); title_row.addWidget(close)
        content.addLayout(title_row)
        self.panel = LanPanel(service, self.card, theme=settings.theme, settings=settings)
        self.body_scroll=QScrollArea(self.card);self.body_scroll.setWidgetResizable(True)
        self.body_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.body_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.body_scroll.setStyleSheet('''QScrollArea {background:transparent;border:0;}
            QScrollArea > QWidget > QWidget {background:transparent;}
            QScrollBar:vertical {width:5px;background:transparent;}
            QScrollBar::handle:vertical {background:#aab8cb;border-radius:2px;min-height:20px;}
            QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical {height:0;}
            QScrollBar::add-page:vertical,QScrollBar::sub-page:vertical {background:transparent;}''')
        self.body_scroll.viewport().setAutoFillBackground(False)
        self.body_scroll.setWidget(self.panel);content.addWidget(self.body_scroll, 1)
        self.panel.transfer_started.connect(self._fit_active)
        self.apply_theme(settings.theme)

    def _fit_active(self):
        screen=QGuiApplication.screenAt(self.frameGeometry().center()) or QGuiApplication.primaryScreen()
        available=screen.availableGeometry() if screen else None
        height=min(700,available.height()-20) if available else 700
        if height>self.height():
            self.resize(self.width(),height)
            if available and self.frameGeometry().bottom()>available.bottom()-8:
                self.move(self.x(),available.bottom()-self.height()-8)

    def apply_theme(self, theme):
        dark = theme == 'dark'
        surface = '#242a34' if dark else '#ffffff'
        ink = '#edf2fa' if dark else '#192636'
        hover = '#364254' if dark else '#edf2f8'
        self.card.setStyleSheet(f'''
            QFrame#lanDialogCard {{ background:{surface}; border:0; border-radius:19px; }}
            QLabel#lanDialogTitle {{ color:{ink}; font:600 20px 'HarmonyOS Sans SC'; background:transparent; }}
            QPushButton#lanDialogClose {{ background:transparent; color:{ink}; border:0; border-radius:15px;
                                          font:18px 'HarmonyOS Sans SC'; padding:0; }}
            QPushButton#lanDialogClose:hover {{ background:{hover}; }}
        ''')
        self.panel.apply_theme(theme)
