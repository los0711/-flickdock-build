"""Translation capsule sharing the existing notice host and motion infrastructure."""
from pathlib import Path
from PySide6.QtCore import QPoint, QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QCursor, QPainter, QPainterPath, QPixmap, QRegion, QTextDocument
from PySide6.QtWidgets import QFrame, QMenu, QPlainTextEdit, QPushButton
from PySide6.QtSvg import QSvgRenderer
from intake_notice import CapsuleNotice, COMPACT_SIDE_PX, PILL_HEIGHT_PX
from native_ui_common import colors, icon, rounded_popup
from translation_languages import LANGUAGES, TARGETS
from transfer_visuals import draw_ring

EXPANDED_HEIGHT_PX = 300
EXPANDED_WIDTH_PX = 388
INVITATION_DURATION_MS = 8000
HOST_HEIGHT_PX = EXPANDED_HEIGHT_PX + 20


class TranslationNotice(CapsuleNotice):
    HOST_WIDTH = 412
    HOST_HEIGHT = HOST_HEIGHT_PX
    MAX_CAPSULE_WIDTH = 300
    NOTICE_ORDER = 3

    def __init__(self, manager, controller):
        super().__init__(manager)
        self.controller = controller
        self.expanded = False
        self._expanded_height = EXPANDED_HEIGHT_PX
        self._flags = {}
        self._document = None
        self.setWindowFlags(self.windowFlags() & ~Qt.WindowDoesNotAcceptFocus)
        self.setWindowTitle('FlickDock · 翻译')
        self.editor = QPlainTextEdit(self)
        self.editor.setReadOnly(True)
        text_font=self.font();text_font.setPixelSize(13);self.editor.setFont(text_font)
        self._measure=QTextDocument(self);self._measure.setDefaultFont(text_font)
        self.editor.setAccessibleName('原文与译文')
        self.editor.setFrameShape(QFrame.NoFrame)
        self.editor.hide()
        self.flag_button = QPushButton(self)
        self.middle_button = QPushButton(self)
        self.action_button = QPushButton(self)
        self.flag_button.setAccessibleName('选择目标语言')
        self.middle_button.setAccessibleName('展开原文与译文')
        for button in (self.flag_button, self.middle_button, self.action_button):
            button.setStyleSheet('QPushButton{background:transparent;border:0;border-radius:16px;} QPushButton:hover,QPushButton:focus{background:rgba(127,127,127,28);} QPushButton:pressed{background:rgba(127,127,127,46);}')
            button.setCursor(Qt.PointingHandCursor)
            button.setAutoDefault(False)
        self.flag_button.clicked.connect(self.show_languages)
        self.middle_button.clicked.connect(self.toggle_content)
        self.action_button.clicked.connect(self._action)
        self.controller.changed.connect(self.refresh)
        self._morph.finished.connect(self._editor_visibility)

    def present(self):
        self.expanded = False
        self.editor.hide()
        self._text = self.controller.source + ' / ' + TARGETS[self.manager.settings.translation_target][0]
        screen = self.manager.app.screenAt(QCursor.pos()) or self.manager.app.primaryScreen()
        self._begin_presentation(screen.availableGeometry())
        self.refresh()
        self.timer.start(INVITATION_DURATION_MS)

    def capsule_rect(self):
        height = min(self.height() - 4, self._shape.y())
        width = min(self.width() - 4, max(20, self._shape.x()))
        return QRectF((self.width() - width) / 2, self.height() - 10 - height, width, height)

    def _clip_finished(self):
        if not self.isVisible():
            return
        path = QPainterPath()
        path.addRoundedRect(self.capsule_rect().adjusted(-2, -2, 2, 2), 24, 24)
        self.setMask(QRegion(path.toFillPolygon().toPolygon()))
        self._layout_controls()

    def _layout_controls(self):
        rect = self.capsule_rect()
        y = round(rect.bottom() - PILL_HEIGHT_PX)
        self.flag_button.setGeometry(round(rect.left() + 6), y + 2, 40, 40)
        self.action_button.setGeometry(round(rect.right() - 42), y + 4, 36, 36)
        self.middle_button.setGeometry(round(rect.left() + 49), y + 2, max(1, round(rect.width() - 98)), 40)
        for button in (self.flag_button, self.middle_button, self.action_button):
            button.setVisible(not self._hiding and rect.width() > 120)
        if self.expanded and self.editor.isVisible():self.editor.move(self.editor.x(),round(rect.top()+16))

    def _editor_visibility(self):
        self.editor.setVisible(self.expanded and not self._hiding)

    def toggle_content(self):
        self.timer.stop()
        self.expanded = not self.expanded
        self.editor.hide()
        self._animate_shape(min(EXPANDED_WIDTH_PX, self.width() - 4) if self.expanded else self._full_width,
                            self._expanded_height if self.expanded else PILL_HEIGHT_PX)
        self.middle_button.setAccessibleName('收起原文与译文' if self.expanded else '展开原文与译文')
        if not self._morph.state():self._editor_visibility()
        if not self.expanded and not self.controller.busy:self.timer.start(INVITATION_DURATION_MS)

    def refresh(self):
        state = self.controller
        self._text = state.source + ' / ' + TARGETS[self.manager.settings.translation_target][0]
        self._description = state.error or ('翻译完成，点击右侧复制完整译文' if state.result else '点击右侧翻译，点击文字展开内容')
        self.action_button.setAccessibleName('复制完整译文' if state.result else '翻译这段文字')
        self.action_button.setEnabled(state.phase != 'loading')
        self.setToolTip(state.error)
        self._phase = 'syncing' if state.phase == 'loading' else 'idle'
        self._set_progress(dict(received=0, total=0) if state.phase == 'loading' else None)
        self._sync_ring_clock()
        if state.phase == 'loading':
            self.timer.stop()
        elif state.phase in ('ready', 'error', 'copied') and not self.expanded:
            self.timer.start(INVITATION_DURATION_MS)
        document = '原文 · ' + state.source + '\n\n' + state.text
        if state.result:
            document += '\n\n译文 · ' + TARGETS[self.manager.settings.translation_target][0] + '\n\n' + state.result
        elif state.error:
            document += '\n\n' + state.error
        if document != self._document:
            self._document = document
            self.editor.setPlainText(document)
        c = colors(self.manager.settings.theme)
        self.editor.setStyleSheet(f'QPlainTextEdit{{background:{c["surface"]};color:{c["text"]};border:0;font-size:13px;selection-background-color:#147EFB;}}')
        width = min(EXPANDED_WIDTH_PX, self.width() - 4)
        self._measure.setTextWidth(width-36)
        self._measure.setPlainText(document)
        height=min(EXPANDED_HEIGHT_PX,self.height()-8,max(118,round(self._measure.size().height())+PILL_HEIGHT_PX+36))
        previous_height=self._expanded_height;self._expanded_height=height
        self.editor.setGeometry(round((self.width()-width)/2+18), round(self.height()-10-height+16), round(width-36), round(height-PILL_HEIGHT_PX-28))
        if self.expanded and previous_height!=height:
            self.editor.move(self.editor.x(),round(self.capsule_rect().top()+16))
            self._animate_shape(min(EXPANDED_WIDTH_PX,self.width()-4),height)
        self._sync_accessibility()
        self.update()

    def _action(self):
        self.timer.stop()
        if self.controller.result:
            self.controller.copy_result()
        else:
            self.controller.translate()

    def _flag(self, region):
        if region not in self._flags:
            path = Path(__file__).resolve().parent / 'assets' / 'flags' / (region + '.svg')
            renderer = QSvgRenderer(str(path))
            if not renderer.isValid():
                raise ValueError('Invalid bundled flag: ' + region)
            pixmap = QPixmap(64, 64)
            pixmap.fill(Qt.transparent)
            painter = QPainter(pixmap)
            renderer.render(painter, QRectF(-10, 0, 84, 64))
            painter.end()
            self._flags[region] = pixmap
        return self._flags[region]

    def show_languages(self):
        self.timer.stop()
        menu = QMenu(self)
        rounded_popup(menu, 14)
        c = colors(self.manager.settings.theme)
        menu.setStyleSheet(f'QMenu{{background:{c["surface"]};color:{c["text"]};border:0;padding:6px;}} QMenu::item{{padding:7px 22px 7px 12px;border-radius:8px;}} QMenu::item:selected{{background:{c["control"]};}}')
        from PySide6.QtGui import QIcon
        for code, label, region in LANGUAGES:
            action = menu.addAction(QIcon(self._flag(region)), label)
            action.setCheckable(True)
            action.setChecked(code == self.manager.settings.translation_target)
            action.triggered.connect(lambda checked=False, code=code: self.manager.settings.update(translation_target=code))
        # A fresh menu with the first action active always opens at the top,
        # while the check mark still represents the persisted target.
        menu.setActiveAction(menu.actions()[0])
        origin = self.mapToGlobal(self.flag_button.geometry().topLeft())
        area = self.screen().availableGeometry()
        menu.popup(QPoint(origin.x(), max(area.top()+8, origin.y()-min(menu.sizeHint().height(), area.height()-40)-8)))
        menu.aboutToHide.connect(menu.deleteLater)
        menu.aboutToHide.connect(lambda: self.timer.start(INVITATION_DURATION_MS) if not self.expanded and not self.controller.busy else None)

    def dismiss(self):
        self.editor.hide()
        super().dismiss()

    def keyPressEvent(self,event):
        if event.key()==Qt.Key_Escape:self.dismiss();event.accept()
        else:super().keyPressEvent(event)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setCompositionMode(QPainter.CompositionMode_Source)
        painter.fillRect(self.rect(), Qt.transparent)
        painter.setCompositionMode(QPainter.CompositionMode_SourceOver)
        painter.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform)
        painter.setOpacity(max(0., min(1., self._alpha)))
        rect = self.capsule_rect()
        c = colors(self.manager.settings.theme)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(c['surface']))
        painter.drawRoundedRect(rect, 22, 22)
        bar = QRectF(rect.left(), rect.bottom()-PILL_HEIGHT_PX, rect.width(), PILL_HEIGHT_PX)
        path = QPainterPath()
        path.addRoundedRect(rect, 22, 22)
        painter.setClipPath(path)
        painter.setFont(self._notice_font())
        if rect.width() > 120:
            flag = QRectF(bar.left()+13, bar.center().y()-12, 24, 24)
            painter.save()
            clip = QPainterPath()
            clip.addEllipse(flag)
            painter.setClipPath(clip, Qt.IntersectClip)
            painter.drawPixmap(flag, self._flag(TARGETS[self.manager.settings.translation_target][1]), QRectF(0, 0, 64, 64))
            painter.restore()
            painter.setPen(QColor(c['text']))
            text_rect = bar.adjusted(52, 0, -50, 0)
            label = self.controller.error or self._text
            painter.drawText(text_rect, Qt.AlignCenter, painter.fontMetrics().elidedText(label, Qt.ElideRight, round(text_rect.width())))
        ring = QRectF(bar.right()-38, bar.center().y()-16, 32, 32)
        state = self.controller
        if state.phase == 'loading':
            draw_ring(painter, ring, dict(received=0, total=0))
        else:
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(c['green'] if state.phase == 'copied' else c['link']))
            painter.drawEllipse(ring)
            if state.result:
                icon('check' if state.phase == 'copied' else 'copy', '#FFFFFF').paint(painter, ring.adjusted(8, 8, -8, -8).toAlignedRect())
            else:
                painter.setPen(QColor('#FFFFFF'))
                painter.drawText(ring, Qt.AlignCenter, '译')
        painter.end()
