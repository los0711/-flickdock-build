"""Translation capsule sharing the existing notice host and motion infrastructure."""
import math
from pathlib import Path
from PySide6.QtCore import QEasingCurve, QEvent, QPoint, QPointF, QRectF, QSize, Qt, QTimer
from PySide6.QtGui import QColor, QCursor, QFontMetricsF, QIcon, QPainter, QPainterPath, QPen, QPixmap, QTextCharFormat, QTextCursor, QTextDocument
from PySide6.QtWidgets import QApplication, QAbstractItemView, QFrame, QListWidget, QListWidgetItem, QMenu, QPlainTextEdit, QPushButton, QWidgetAction
from PySide6.QtSvg import QSvgRenderer
from intake_notice import CapsuleNotice, COMPACT_SIDE_PX, PILL_HEIGHT_PX, NOTICE_ACTION_COLOR
from native_ui_common import colors, icon, rounded_popup, screen_for_widget, RADIUS_CARD_PX, RADIUS_SUBCONTROL_PX
from translation_languages import LANGUAGES, TARGETS
from transfer_visuals import draw_ring
import notice_cluster

EXPANDED_HEIGHT_PX = 300
EXPANDED_WIDTH_PX = 388
TRANSLATION_NOTICE_DURATION_MS = 5000
HOST_HEIGHT_PX = EXPANDED_HEIGHT_PX + 20
LANGUAGE_VISIBLE_ROWS = 8
LANGUAGE_ROW_HEIGHT_PX = 32
LANGUAGE_MENU_WIDTH_PX = 208
FOCUS_COLLAPSE_DELAY_MS = 2000
CONTENT_SIDE_INSET_PX = 18
CONTENT_TOP_INSET_PX = 16
CONTENT_BOTTOM_GAP_PX = 12
MIDDLE_CONTROL_VERTICAL_INSET_PX = 6
MIDDLE_CONTROL_EDGE_PX = 48
MIDDLE_TEXT_PADDING_PX = 7


class LanguageList(QListWidget):
    def keyPressEvent(self, event):
        # macOS does not activate embedded menu item views with Return by
        # default. Both physical Enter keys commit the highlighted language.
        if event.key() in (Qt.Key_Return, Qt.Key_Enter) and self.currentItem() is not None:
            self.itemActivated.emit(self.currentItem())
            event.accept()
        else:
            super().keyPressEvent(event)


class TranslationNotice(CapsuleNotice):
    HOST_WIDTH = 412
    HOST_HEIGHT = HOST_HEIGHT_PX
    MAX_CAPSULE_WIDTH = 300
    NOTICE_ORDER = 3

    def __init__(self, manager, controller):
        super().__init__(manager)
        self.controller = controller
        self.expanded = False
        self.language_menu = None
        self._watching_content = False
        self._pointer_buttons = 0
        self._pointer_inside = None
        self._window_deactivated = False
        self.focus_timer = QTimer(self)
        self.focus_timer.setSingleShot(True)
        self.focus_timer.setInterval(FOCUS_COLLAPSE_DELAY_MS)
        self.focus_timer.timeout.connect(self._collapse_unfocused)
        self._expanded_height = EXPANDED_HEIGHT_PX
        self._flags = {}
        self._document = None
        self._document_theme = None
        self._translation_position = None
        self._locate_translation_pending = False
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
        manager.app.focusChanged.connect(self._focus_changed)
        manager.app.applicationStateChanged.connect(self._focus_changed)

    def present(self):
        self._stop_content_watch()
        self.expanded = False
        self._close_languages()
        self.editor.hide()
        self._text = self._invitation_text()
        screen = self.manager.app.screenAt(QCursor.pos()) or self.manager.app.primaryScreen()
        self._begin_presentation(screen.availableGeometry())
        self.refresh()
        self._start_idle_timeout()

    def _start_idle_timeout(self):
        self.timer.start(TRANSLATION_NOTICE_DURATION_MS)

    def capsule_rect(self):
        height = min(self.height() - 4, self._shape.y())
        width = min(self.width() - 4, max(20, self._shape.x()))
        return QRectF((self.width() - width) / 2, self.height() - 10 - height, width, height)

    def _clip_finished(self):
        if not self.isVisible():
            return
        path = QPainterPath()
        path.addRoundedRect(self.capsule_rect().adjusted(-2, -2, 2, 2), 24, 24)
        self._set_notice_mask(notice_cluster.shape(self,path,mask=True))

    def _shape_changed(self, value):
        super()._shape_changed(value)
        self._layout_controls()

    def _content_width(self):
        # The compact state must reserve real space for both circular controls,
        # their gaps and the middle button's own text padding.
        text_width = QFontMetricsF(self._notice_font()).horizontalAdvance(self._text)
        side = MIDDLE_CONTROL_EDGE_PX + MIDDLE_TEXT_PADDING_PX
        return min(self.MAX_CAPSULE_WIDTH, self.width()-4, max(132, math.ceil(text_width+2*side)))

    def _middle_rect(self, bar):
        return QRectF(bar.left()+MIDDLE_CONTROL_EDGE_PX, bar.top()+MIDDLE_CONTROL_VERTICAL_INSET_PX,
                      max(1, bar.width()-2*MIDDLE_CONTROL_EDGE_PX),
                      PILL_HEIGHT_PX-2*MIDDLE_CONTROL_VERTICAL_INSET_PX)

    def _label_rect(self, bar):
        # Do not re-elide/reflow the label as the details panel changes width.
        # Its layout is fixed at the compact width and clipped by the current
        # middle button while the entrance or dismissal is still very narrow.
        width = max(1, min(self._full_width, self.width()-4)-2*(MIDDLE_CONTROL_EDGE_PX+MIDDLE_TEXT_PADDING_PX))
        return QRectF(bar.center().x()-width/2, bar.top(), width, bar.height())

    def _layout_controls(self):
        rect = self.capsule_rect()
        y = round(rect.bottom() - PILL_HEIGHT_PX)
        self.flag_button.setGeometry(round(rect.left() + 4), y + 4, 36, 36)
        self.action_button.setGeometry(round(rect.right() - 42), y + 4, 36, 36)
        self.middle_button.setGeometry(self._middle_rect(QRectF(rect.left(), y, rect.width(), PILL_HEIGHT_PX)).toAlignedRect())
        for button in (self.flag_button, self.middle_button, self.action_button):
            button.setVisible(not self._hiding and rect.width() > 120)
        if self.expanded:
            # A result can grow the document while the expansion is still
            # running. Lay out against this frame, not its final height, so
            # the editor can never paint over the capsule controls.
            self.editor.setGeometry(round(rect.left()+CONTENT_SIDE_INSET_PX),round(rect.top()+CONTENT_TOP_INSET_PX),
                max(0,round(rect.width()-2*CONTENT_SIDE_INSET_PX)),
                max(0,round(rect.height()-PILL_HEIGHT_PX-CONTENT_TOP_INSET_PX-CONTENT_BOTTOM_GAP_PX)))

    def _editor_visibility(self):
        self._layout_controls()
        self.editor.setVisible(self.expanded and not self._hiding)
        self._locate_translation()

    def _locate_translation(self):
        if not self._locate_translation_pending or not self.editor.isVisible():
            return
        cursor=self.editor.textCursor()
        cursor.setPosition(self._translation_position)
        self.editor.setTextCursor(cursor)
        self.editor.ensureCursorVisible()
        bar=self.editor.verticalScrollBar()
        bar.setValue(min(bar.maximum(),bar.value()+self.editor.cursorRect(cursor).top()))
        self._locate_translation_pending=False

    def toggle_content(self):
        self.controller.open_translator()

    @property
    def target(self):return getattr(self.controller,'target',self.manager.settings.translation_target)

    def _invitation_text(self):
        return '是否翻译成' + TARGETS[self.target][0]

    def _set_expanded(self, expanded):
        if self.expanded == expanded:
            return
        self.timer.stop()
        self.expanded = expanded
        if expanded:
            self.manager.app.installEventFilter(self)
            self._watching_content = True
            self._pointer_buttons = int(QApplication.mouseButtons().value)
            self.activateWindow()
            self.middle_button.setFocus(Qt.OtherFocusReason)
        else:
            self._stop_content_watch()
        self.editor.hide()
        self._animate_shape(min(EXPANDED_WIDTH_PX, self.width() - 4) if self.expanded else self._full_width,
                            self._expanded_height if self.expanded else PILL_HEIGHT_PX)
        # Never overshoot below the compact width when closing the details:
        # the side controls must not pass over the fixed middle label.
        if self._morph.state()==self._morph.Running:
            self._morph.setEasingCurve(QEasingCurve.OutCubic)
        self.middle_button.setAccessibleName('收起原文与译文' if self.expanded else '展开原文与译文')
        if self._morph.state()!=self._morph.Running:self._editor_visibility()
        if not self.expanded and not self.controller.busy:self._start_idle_timeout()
        if self.expanded:self._focus_changed()

    def _collapse_content(self):
        self._close_languages()
        self._set_expanded(False)

    def _interaction_active(self):
        if self._pointer_inside is False:
            return False
        return (self.manager.app.applicationState() == Qt.ApplicationActive
                and (self.language_menu is not None and self.language_menu.isVisible()
                     or not self._window_deactivated and (self._pointer_inside is True or self.isActiveWindow())))

    def _collapse_unfocused(self):
        if not self._interaction_active():self._collapse_content()

    def _stop_content_watch(self):
        self.focus_timer.stop()
        self._pointer_inside = None
        self._window_deactivated = False
        if self._watching_content:
            self.manager.app.removeEventFilter(self)
            self._watching_content = False

    def _contains_interaction(self, point):
        return (self.capsule_rect().contains(self.mapFromGlobal(point))
                or self.language_menu is not None and self.language_menu.isVisible()
                and self.language_menu.frameGeometry().contains(point))

    def reconcile_pointer(self, point, buttons):
        pressed = buttons & ~self._pointer_buttons
        self._pointer_buttons = buttons
        if self.expanded and self.isVisible():
            self._pointer_inside = self._contains_interaction(point)
            if pressed and not self._pointer_inside:
                self._collapse_content()
            else:
                self._focus_changed()

    def _focus_changed(self, *_):
        if not self.expanded or not self.isVisible() or self._hiding:
            self.focus_timer.stop()
            return
        if self._interaction_active():
            self.focus_timer.stop()
        elif not self.focus_timer.isActive():
            self.focus_timer.start()

    def eventFilter(self, watched, event):
        kind = event.type()
        if kind in (QEvent.MouseButtonPress, QEvent.MouseButtonDblClick, QEvent.NonClientAreaMouseButtonPress):
            point = event.globalPosition().toPoint()
            if self.language_menu is not None and watched is self.language_menu:
                if self.flag_button.rect().contains(self.flag_button.mapFromGlobal(point)):
                    self.language_menu.setAttribute(Qt.WA_NoMouseReplay)
                    self.language_menu.hide()
                    return True
            if self.expanded and not self._contains_interaction(point):
                self._collapse_content()
        elif watched is self and kind == QEvent.WindowDeactivate:
            # Native deactivation can arrive before Qt updates activeWindow.
            # Retain that event until activation, rather than cancelling the
            # deadline against stale Qt focus state at timeout.
            if not (self.language_menu is not None and self.language_menu.isVisible()):
                self._window_deactivated = True
            self._focus_changed()
        elif watched is self and kind == QEvent.WindowActivate:
            self._window_deactivated = False
            self._focus_changed()
        return False

    def refresh(self):
        state = self.controller
        self._text = self._invitation_text()
        if state.result:
            self._text = '已翻译成' + TARGETS[self.target][0]
        self.middle_button.setAccessibleName(self._text + '，点击打开翻译器')
        compact_width = self._content_width()
        if self.isVisible() and not self.expanded and not self._hiding and compact_width != self._full_width:
            self._full_width = compact_width
            self._animate_shape(compact_width)
        self._description = state.error or ('翻译完成，点击右侧复制完整译文' if state.result else '点击右侧翻译，点击文字打开翻译器')
        self.action_button.setAccessibleName('复制完整译文' if state.result else '翻译这段文字')
        self.action_button.setEnabled(state.phase != 'loading')
        self.setToolTip(state.error)
        self._phase = 'syncing' if state.phase == 'loading' else 'idle'
        self._set_progress(dict(received=0, total=0) if state.phase == 'loading' else None)
        self._sync_ring_clock()
        if state.phase == 'loading':
            self.timer.stop()
        elif state.phase in ('ready', 'error', 'copied') and not self.expanded:
            self._start_idle_timeout()
        document = '原文\n\n' + state.text
        result_position = None
        if state.result:
            result_position=len((document+'\n\n').encode('utf-16-le'))//2
            document += '\n\n译文 · ' + TARGETS[self.target][0] + '\n\n' + state.result
        elif state.error:
            document += '\n\n' + state.error
        changed=document != self._document
        if changed:
            self._document = document
            self.editor.setPlainText(document)
        c = colors('dark')
        if changed or self._document_theme != self.manager.settings.theme:
            self._document_theme=self.manager.settings.theme
            if result_position is not None:
                cursor=QTextCursor(self.editor.document())
                cursor.setPosition(result_position)
                cursor.movePosition(QTextCursor.End,QTextCursor.KeepAnchor)
                formatting=QTextCharFormat();formatting.setForeground(QColor(c['green']))
                cursor.mergeCharFormat(formatting)
        if changed:
            self._translation_position=result_position
            self._locate_translation_pending=result_position is not None
        self.editor.setStyleSheet(f'QPlainTextEdit{{background:{c["dialog_surface"]};color:{c["text"]};border:0;font-size:13px;selection-background-color:{c["action"]};selection-color:{c["on_action"]};}} QScrollBar:vertical{{background:transparent;width:6px;margin:0;}} QScrollBar::handle:vertical{{background:#55555C;border-radius:3px;min-height:24px;}} QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical{{height:0;}} QScrollBar::add-page:vertical,QScrollBar::sub-page:vertical{{background:transparent;}}')
        width = min(EXPANDED_WIDTH_PX, self.width() - 4)
        self._measure.setTextWidth(width-2*CONTENT_SIDE_INSET_PX)
        self._measure.setPlainText(document)
        height=min(EXPANDED_HEIGHT_PX,self.height()-8,max(118,round(self._measure.size().height())+PILL_HEIGHT_PX+36))
        previous_height=self._expanded_height;self._expanded_height=height
        if self.expanded and previous_height!=height:
            self._animate_shape(min(EXPANDED_WIDTH_PX,self.width()-4),height)
        self._layout_controls()
        if self._morph.state()!=self._morph.Running:self._locate_translation()
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
            if not region:
                self._flags[region] = icon('web', colors('dark')['secondary']).pixmap(64,64)
                return self._flags[region]
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
        if self.language_menu is not None and self.language_menu.isVisible():
            self.language_menu.hide()
            return
        self.timer.stop()
        self.focus_timer.stop()
        from shelf_ui_common import RoundedMenu
        menu = RoundedMenu(self);menu.surface_theme='dark'
        self.language_menu = menu
        menu.installEventFilter(self)
        rounded_popup(menu)
        c = colors('dark')
        menu.setStyleSheet(f'QMenu{{background:transparent;color:{c["text"]};border:0;padding:7px;}}')
        languages = LanguageList(menu)
        languages.setProperty('scrollEdgeBlur',False)
        languages.setProperty('scrollBackdropColor',c['popup_surface'])
        languages.setAccessibleName('目标语言')
        languages.setFrameShape(QFrame.NoFrame)
        languages.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        languages.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        languages.setIconSize(QSize(22, 22))
        languages.setSpacing(2)
        languages.setStyleSheet(f'QListWidget{{background:transparent;color:{c["text"]};border:0;outline:0;}} QListWidget::item{{padding:0 8px;border:0;outline:0;border-radius:{RADIUS_SUBCONTROL_PX}px;}} QListWidget::item:hover{{background:{c["control_hover"]};}} QListWidget::item:selected{{background:{c["action"]};color:{c["on_action"]};}} QScrollBar:vertical{{width:6px;background:transparent;}} QScrollBar::handle:vertical{{background:{c["secondary"]};border-radius:3px;min-height:24px;}} QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical{{height:0;}} QScrollBar::add-page:vertical,QScrollBar::sub-page:vertical{{background:transparent;}}')
        selected=None
        for code, label, region in LANGUAGES:
            item = QListWidgetItem(QIcon(self._flag(region)), label, languages)
            item.setTextAlignment(Qt.AlignLeft|Qt.AlignVCenter)
            item.setData(Qt.UserRole, code)
            item.setSizeHint(QSize(LANGUAGE_MENU_WIDTH_PX, LANGUAGE_ROW_HEIGHT_PX))
            if code==self.target:selected=item
        area = screen_for_widget(self).availableGeometry()
        languages.setFixedSize(min(LANGUAGE_MENU_WIDTH_PX,area.width()-28), min(LANGUAGE_VISIBLE_ROWS*LANGUAGE_ROW_HEIGHT_PX,area.height()-40))
        action = QWidgetAction(menu)
        action.setDefaultWidget(languages)
        menu.addAction(action)
        def select(item):
            menu.hide()
            self.manager.settings.update(translation_target=item.data(Qt.UserRole))
        languages.itemClicked.connect(select)
        languages.itemActivated.connect(select)
        menu.aboutToHide.connect(self._languages_hidden)
        origin = self.mapToGlobal(self.flag_button.geometry().topLeft())
        menu.popup(QPoint(origin.x(), max(area.top()+8, origin.y()-min(menu.sizeHint().height(), area.height()-40)-8)))
        if selected is not None:
            languages.setCurrentItem(selected)
            languages.scrollToItem(selected)
        languages.setFocus(Qt.PopupFocusReason)

    def _languages_hidden(self):
        menu = self.language_menu
        # Qt replays an outside press after a popup closes unless it is
        # suppressed here; replaying a flag press would reopen the chooser.
        menu.setAttribute(Qt.WA_NoMouseReplay)
        self.language_menu = None
        menu.deleteLater()
        if not self.expanded and not self.controller.busy:self._start_idle_timeout()
        self._focus_changed()

    def _close_languages(self):
        if self.language_menu is not None:self.language_menu.hide()

    def dismiss(self):
        self._close_languages()
        self._stop_content_watch()
        self.editor.hide()
        super().dismiss()

    def hideEvent(self, event):
        self._stop_content_watch()
        self._close_languages()
        super().hideEvent(event)

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
        c = colors('dark')
        painter.setPen(QPen(QColor(c['qt_border']), 1))
        painter.setBrush(QColor(c['dialog_surface']))
        background=QPainterPath();background.addRoundedRect(rect.adjusted(.5,.5,-.5,-.5),21.5,21.5)
        painter.drawPath(notice_cluster.shape(self,background))
        bar = QRectF(rect.left(), rect.bottom()-PILL_HEIGHT_PX, rect.width(), PILL_HEIGHT_PX)
        path = QPainterPath()
        path.addRoundedRect(rect, 22, 22)
        painter.setClipPath(path)
        painter.setFont(self._notice_font())
        if rect.width() > 120:
            flag_background=QRectF(bar.left()+6,bar.center().y()-16,32,32)
            painter.setPen(Qt.NoPen);painter.setBrush(QColor(c['control']));painter.drawEllipse(flag_background)
            flag = flag_background.adjusted(4,4,-4,-4)
            painter.save()
            clip = QPainterPath()
            clip.addEllipse(flag)
            painter.setClipPath(clip, Qt.IntersectClip)
            painter.drawPixmap(flag, self._flag(TARGETS[self.target][1]), QRectF(0, 0, 64, 64))
            painter.restore()
            painter.save()
            painter.setClipRect(self._middle_rect(bar).adjusted(MIDDLE_TEXT_PADDING_PX,0,-MIDDLE_TEXT_PADDING_PX,0),Qt.IntersectClip)
            painter.setPen(QColor(c['text']))
            text_rect = self._label_rect(bar)
            label = self.controller.error or self._text
            painter.drawText(text_rect, Qt.AlignCenter, painter.fontMetrics().elidedText(label, Qt.ElideRight, round(text_rect.width())))
            painter.restore()
        ring = QRectF(bar.right()-38, bar.center().y()-16, 32, 32)
        state = self.controller
        if state.phase == 'loading':
            draw_ring(painter, ring, dict(received=0, total=0),color=c['action'])
        else:
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(c['green'] if state.phase == 'copied' else c['action']))
            painter.drawEllipse(ring)
            if state.result:
                if state.phase=='copied':
                    painter.setPen(QPen(QColor(c['on_action']),2.5,Qt.SolidLine,Qt.RoundCap,Qt.RoundJoin))
                    center=ring.center()
                    painter.drawLine(QPointF(center.x()-8,center.y()),QPointF(center.x()-2,center.y()+6))
                    painter.drawLine(QPointF(center.x()-2,center.y()+6),QPointF(center.x()+9,center.y()-6))
                else:icon('copy',c['on_action']).paint(painter,ring.adjusted(8,8,-8,-8).toAlignedRect())
            else:
                painter.setPen(QColor(c['on_action']))
                painter.drawText(ring, Qt.AlignCenter, '译')
        painter.end()
