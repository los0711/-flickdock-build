"""Floating preview for opted-in translation of the foreground editor."""
from collections import OrderedDict
import logging
import sys
from time import monotonic

from PySide6.QtCore import QEvent, QObject, QPoint, QRect, QSize, Qt, QThreadPool, QTimer, Signal
from PySide6.QtGui import QColor, QCursor, QFontMetrics, QRegion, QTextCharFormat, QTextCursor, QTextOption
from PySide6.QtWidgets import QApplication, QHBoxLayout, QLabel, QTextEdit, QVBoxLayout, QWidget

from flickdock_motion import Spring
from live_translation_draft import LiveDraft
from live_translation_editor import EditorSelection, focused_editor
from native_ui_common import RoundedScrollBar, colors, font_css_stack, reduced_motion
from platform_native import mouse_buttons_mask
from reference_widgets import PixelScrollTextEdit, ToolbarButton
from sync_files import DiskWork
from translation_alignment import SelectionAlignment
from translation_api import TranslationApi
from translation_document import text_to_editor_position
from translation_languages import TARGETS, content_language, quick_content_language
from translator_widgets import LanguageButton, LanguagePopup, TranslatorSurface, translator_action
from ui_transitions import CARD_REBOUND_SPRING
import ui_theme


LOG = logging.getLogger(__name__)
CARD_GAP = 10
CARD_WIDTH = 620
CARD_MIN_HEIGHT = 38
CARD_MAX_HEIGHT = 116
FOOTER_HEIGHT = 42


def fit_card(area, obstacle, size, preferred=None, *, below_first=False):
    """Project the drag target onto a screen region with no editor overlap."""
    reserved = obstacle.adjusted(-CARD_GAP, -CARD_GAP, CARD_GAP, CARD_GAP).intersected(area)
    if reserved.isEmpty():
        return QRect()
    available = (
        QRect(area.left(), reserved.bottom() + 1, area.width(), area.bottom() - reserved.bottom()),
        QRect(area.left(), area.top(), area.width(), reserved.top() - area.top()),
        QRect(reserved.right() + 1, area.top(), area.right() - reserved.right(), area.height()),
        QRect(area.left(), area.top(), reserved.left() - area.left(), area.height()),
    )
    candidates = []
    for index, region in enumerate(available):
        if region.width() < size.width() or region.height() < size.height():
            continue
        target = preferred or QPoint(obstacle.left(), obstacle.bottom() + CARD_GAP + 1)
        point = QPoint(max(region.left(), min(target.x(), region.right() - size.width() + 1)),
                       max(region.top(), min(target.y(), region.bottom() - size.height() + 1)))
        if below_first and index == 0:
            return QRect(point, size)
        candidates.append(((point - target).manhattanLength(), QRect(point, size)))
    return min(candidates, key=lambda item: item[0])[1] if candidates else QRect()


def _editor_geometry(bounds):
    rect = QRect(*bounds)
    screens = QApplication.screens()
    if sys.platform != 'win32':
        screen = QApplication.screenAt(rect.center())
        return rect, screen
    for screen in screens:
        logical = screen.geometry()
        scale = screen.devicePixelRatio()
        physical = QRect(logical.topLeft(), QSize(round(logical.width() * scale), round(logical.height() * scale)))
        if physical.contains(rect.center()):
            origin = logical.topLeft()
            result = QRect(origin + QPoint(round((rect.x() - origin.x()) / scale),
                                           round((rect.y() - origin.y()) / scale)),
                           QSize(round(rect.width() / scale), round(rect.height() / scale)))
            return result, screen
    return rect, None


class LiveTranslationCard(QWidget):
    target_changed = Signal(str)
    insert_requested = Signal()
    sample_requested = Signal()

    def __init__(self, store, parent=None):
        flags = Qt.Tool | Qt.FramelessWindowHint | Qt.WindowDoesNotAcceptFocus | Qt.WindowStaysOnTopHint
        super().__init__(parent, flags)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        if sys.platform == 'darwin':
            self.setAttribute(Qt.WA_MacAlwaysShowToolWindow)
        self.setObjectName('liveTranslationCard')
        self.resize(CARD_WIDTH, CARD_MIN_HEIGHT)
        self.setMinimumWidth(252)
        self.setAccessibleName('实时翻译')
        self.anchor_area = QRect()
        self.obstacle = QRect()
        self.anchor = QPoint()
        self.drag_offset = QPoint()
        self.goal = QRect()
        self.target = 'en'
        self.drag_origin = None
        self.deferred_hide = False
        self._measure_key = None
        self._body_height = 20
        self._base_height = CARD_MIN_HEIGHT
        self._footer_open = False
        self.surface = TranslatorSurface(self, role='popup_surface', radius=14)
        self.content = QWidget(self.surface)
        body = QVBoxLayout(self.content)
        body.setContentsMargins(14, 8, 14, 9)
        body.setSpacing(3)
        self.handle = QLabel(self.content)
        self.handle.hide()
        self.handle.setCursor(Qt.SizeAllCursor)
        self.handle.installEventFilter(self)
        body.addWidget(self.handle)
        self.result = PixelScrollTextEdit(self.content)
        self.result.setProperty('scrollEdgeBlur', False)
        self.result.setVerticalScrollBar(RoundedScrollBar(Qt.Vertical, self.result))
        self.result.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.result.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.result.setWordWrapMode(QTextOption.WrapAtWordBoundaryOrAnywhere)
        self.result.document().setDocumentMargin(0)
        self.result.setFrameShape(QTextEdit.NoFrame)
        self.result.setReadOnly(True)
        self.result.setMinimumHeight(18)
        self.result.setFocusPolicy(Qt.NoFocus)
        self.result.setTextInteractionFlags(Qt.NoTextInteraction)
        self._result_viewport = self.result.viewport()
        self._result_viewport.setCursor(Qt.SizeAllCursor)
        self._result_viewport.installEventFilter(self)
        self.surface.installEventFilter(self)
        body.addWidget(self.result, 1)
        self.footer_clip = QWidget(self.surface)
        self.footer_clip.hide()
        self.footer = QWidget(self.footer_clip)
        footer = QHBoxLayout(self.footer)
        footer.setContentsMargins(14, 0, 14, 0)
        footer.setSpacing(6)
        self.language = LanguageButton(self.footer)
        self.language.setFocusPolicy(Qt.NoFocus)
        self.language.setAccessibleName('目标语言')
        footer.addWidget(self.language)
        self.sample = ToolbarButton('pipette', self.footer)
        self.sample.setFocusPolicy(Qt.NoFocus)
        self.sample.setFixedSize(32, 32)
        self.sample.setCheckable(True)
        self.sample.setToolTip('从下一次划词识别目标语言')
        self.sample.setAccessibleName(self.sample.toolTip())
        footer.addWidget(self.sample)
        footer.addStretch(1)
        self.insert = translator_action('插入', 'return', self.footer, variant='primary')
        self.insert.setFixedHeight(32)
        self.insert.setFocusPolicy(Qt.NoFocus)
        footer.addWidget(self.insert)
        self.popup = LanguagePopup(store, self)
        self.popup.installEventFilter(self)
        self.popup.setWindowFlag(Qt.WindowStaysOnTopHint, True)
        self.language.clicked.connect(self._choose_target)
        self.sample.clicked.connect(self.sample_requested)
        self.insert.clicked.connect(self.insert_requested)
        self.popup.selected.connect(self.target_changed)
        self.motion = Spring(self, {'y': 12., 'opacity': 0.}, self._frame)
        self.motion.settled.connect(self._settled)
        self.footer_motion = Spring(self, {'h': 0.}, self._layout_surface)
        self.theme = None
        self.set_target(self.target)
        self.refresh_theme()

    def refresh_theme(self):
        theme = ui_theme.current_theme()
        if theme == self.theme:
            return
        self.theme = theme
        palette = colors(theme)
        family = font_css_stack()
        self.surface.setProperty('uiTheme', theme)
        self.handle.setStyleSheet(f"color:{palette['secondary']};font:600 11px {family};")
        self.result.setStyleSheet(
            f"QTextEdit{{background:transparent;color:{palette['text']};border:0;font:400 13px {family};}}"
            "QScrollBar:vertical{width:6px;background:transparent;}"
            "QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical{height:0;}"
            "QScrollBar::handle:vertical{min-height:18px;background:transparent;}"
        )
        self.result.set_viewport_fill(QColor(palette['popup_surface']))
        self.surface.update()
        self.language.update()

    def set_target(self, code):
        self.target = code
        self.language.set_language(code)

    def _card_height(self, width):
        text = self.result.toPlainText()
        room = max(1, width - 34)
        key = text, room, self.result.font().key()
        if key != self._measure_key:
            self._measure_key = key
            rect = QFontMetrics(self.result.font()).boundingRect(
                QRect(0, 0, room, 10000), Qt.TextWordWrap | Qt.TextWrapAnywhere, text)
            self._body_height = max(18, rect.height() + 4)
        status_height = 16 if not self.handle.isHidden() else 0
        return min(CARD_MAX_HEIGHT, max(CARD_MIN_HEIGHT, 17 + status_height + self._body_height))

    def set_bounds(self, bounds):
        obstacle, screen = _editor_geometry(bounds)
        if screen is None:
            self.withdraw(immediate=True)
            return False
        self.obstacle = obstacle
        self.anchor = obstacle.bottomLeft() + QPoint(0, CARD_GAP + 1)
        self.anchor_area = screen.availableGeometry().adjusted(10, 10, -10, -10)
        width = min(CARD_WIDTH, self.anchor_area.width())
        for candidate in dict.fromkeys((width, min(width, 430), 252)):
            if candidate < 252:
                continue
            base = self._card_height(candidate)
            size = QSize(candidate, base + FOOTER_HEIGHT)
            goal = fit_card(self.anchor_area, obstacle, size, self.anchor + self.drag_offset, below_first=True)
            if not goal.isEmpty():
                self._base_height = base
                self.goal = goal
                if size != self.size():
                    self.resize(size)
                self._layout_surface(self.footer_motion.values)
                return True
        self.withdraw(immediate=True)
        return False

    def _layout_surface(self, values):
        extent = max(0, min(FOOTER_HEIGHT, round(values['h'])))
        height = self._base_height + extent
        self.content.setGeometry(0, 0, self.width(), self._base_height)
        self.footer_clip.setGeometry(0, self._base_height, self.width(), extent)
        self.footer.setGeometry(0, 4 + (FOOTER_HEIGHT - extent) // 5, self.width(), 32)
        if self.surface.size() != QSize(self.width(), height):
            self.surface.resize(self.width(), height)
            # Keep the native host stable while the visible surface grows.
            self.setMask(QRegion(self.surface.rect()))
        self.footer_clip.setVisible(extent > 0)

    def _set_footer(self, expanded):
        if self._footer_open == expanded:
            return
        self._footer_open = expanded
        target = {'h': float(FOOTER_HEIGHT if expanded else 0)}
        if reduced_motion():
            self.footer_motion.jump(target)
        else:
            self.footer_motion.animate(target, response=.195, damping=.92)

    def enterEvent(self, event):
        super().enterEvent(event)
        self._set_footer(True)

    def leaveEvent(self, event):
        super().leaveEvent(event)
        if self.drag_origin is None and not self.popup.isVisible():
            self._set_footer(False)

    def _restore_hover(self):
        if self.isVisible() and not self.popup.isVisible():
            self._set_footer(self.surface.rect().contains(self.mapFromGlobal(QCursor.pos())))

    def present(self):
        appearing = self.deferred_hide or not self.isVisible()
        self.deferred_hide = False
        self.refresh_theme()
        if not self.isVisible():
            self.motion.jump({'y': 0. if reduced_motion() else 14., 'opacity': 0.})
            self.show()
            self.raise_()
        if appearing:
            self.motion.animate_physical({'y': 0., 'opacity': 1.}, *CARD_REBOUND_SPRING)
        self._place(self.motion.values['y'])

    def withdraw(self, *, immediate=False):
        self.popup.hide()
        self.drag_origin = None
        if not self.isVisible():
            return
        if immediate:
            self.motion.stop()
            self.hide()
            self._footer_open = False
            self.footer_motion.jump({'h': 0.})
        elif not self.deferred_hide:
            self.deferred_hide = True
            self.motion.animate_physical({'y': 8., 'opacity': 0.}, 280, 22)

    def _settled(self):
        if self.deferred_hide:
            self.hide()

    def _frame(self, values):
        self._place(values['y'])
        self.setWindowOpacity(max(0., min(1., values['opacity'])))

    def _place(self, offset):
        if self.goal.isEmpty():
            return
        above = self.goal.bottom() <= self.obstacle.top()
        requested = self.goal.topLeft() + QPoint(0, round(-offset if above else offset))
        # Keep the placement anchor across drag/focus updates and use the
        # unobstructed side picked for this editor.
        frame = fit_card(self.anchor_area, self.obstacle, self.size(), requested)
        if not frame.isEmpty() and self.pos() != frame.topLeft():
            self.move(frame.topLeft())

    def eventFilter(self, source, event):
        if source is getattr(self, 'popup', None) and event.type() == QEvent.Hide:
            QTimer.singleShot(0, self, self._restore_hover)
        if source is self.handle or source is self.surface or source is getattr(self, '_result_viewport', None):
            if event.type() == QEvent.MouseButtonPress and event.button() == Qt.LeftButton:
                self.drag_origin = event.globalPosition().toPoint() - self.pos()
                return True
            if self.drag_origin is not None and event.type() == QEvent.MouseMove:
                requested = event.globalPosition().toPoint() - self.drag_origin
                self.drag_offset = requested - self.anchor
                self.goal = fit_card(self.anchor_area, self.obstacle, self.size(), requested, below_first=True)
                self._place(0)
                return True
            if event.type() == QEvent.MouseButtonRelease and self.drag_origin is not None:
                self.drag_origin = None
                self._restore_hover()
                return True
        return super().eventFilter(source, event)

    def _choose_target(self):
        if self.popup.isVisible():
            self.popup.hide()
        else:
            self.popup.configure(self.target, allow_auto=False)
            for list_height in (222, 154, 104):
                self.popup.list.setFixedHeight(list_height)
                self.popup.adjustSize()
                anchor = self.language.mapToGlobal(QPoint(0, self.language.height())) - QPoint(8, 0)
                preferred = anchor - QPoint(0, self.popup.height())
                frame = fit_card(self.anchor_area, self.obstacle, self.popup.size(), preferred)
                if not frame.isEmpty():
                    self.popup.popup_below(self.language, position=frame.topLeft())
                    self.popup.search.setFocus()
                    break

    def highlight(self, _side, ranges, context, _status):
        selections = []
        text = self.result.toPlainText()
        palette = colors(ui_theme.current_theme())
        for spans, opacity in ((context, 46), (ranges, 94)):
            for begin, end in spans:
                cursor = self.result.textCursor()
                cursor.setPosition(text_to_editor_position(text, min(begin, len(text))))
                cursor.setPosition(text_to_editor_position(text, min(end, len(text))), QTextCursor.KeepAnchor)
                fill = QColor(palette['action'])
                fill.setAlpha(opacity)
                formatting = QTextCharFormat()
                formatting.setBackground(fill)
                item = QTextEdit.ExtraSelection()
                item.cursor, item.format = cursor, formatting
                selections.append(item)
        self.result.setExtraSelections(selections)
        focus = ranges or context
        if focus:
            self.reveal(focus[0][0])

    def reveal(self, offset):
        if self.result.verticalScrollBar().maximum() <= 0:
            return
        text = self.result.toPlainText()
        cursor = self.result.textCursor()
        cursor.setPosition(text_to_editor_position(text, max(0, min(offset, len(text)))))
        self.result.setTextCursor(cursor)
        self.result.ensureCursorVisible()


class LiveEditorTranslation(QObject):
    stopped = Signal()
    POLL_MS = 80
    SAMPLE_POLL_MS = 24
    RETAINED_EDITORS = 6

    def __init__(self, transport, store, parent=None, *, probe=focused_editor, rules=dict):
        super().__init__(parent)
        self.probe = probe
        self.rules = rules
        self.card = LiveTranslationCard(store)
        self.draft = LiveDraft(TranslationApi(transport, self), self)
        self.alignment = SelectionAlignment(self.draft.document, TranslationApi(transport, self), self)
        self.alignment.presentation.connect(self.card.highlight)
        self.draft.changed.connect(self._render)
        self.card.target_changed.connect(self._choose_language)
        self.card.sample_requested.connect(self._toggle_sample)
        self.card.insert_requested.connect(self._insert)
        self.pool = QThreadPool(self, maxThreadCount=1)
        self.poll = QTimer(self, interval=self.POLL_MS)
        self.poll.timeout.connect(self._poll)
        self.editor = None
        self.retained = OrderedDict()
        self.job = None
        self.insertion = None
        self._applied_selection = None
        self._edit_anchor = 0
        self._language_focus_grace = 0.
        self._sample_active = False
        self._sample_pressed = False
        self._sample_until = 0.
        self._sample_watch = 0
        self._sample_message = ''
        self._sample_feedback = QTimer(self, singleShot=True, interval=1700)
        self._sample_feedback.timeout.connect(lambda: self._sample_status(''))
        self._manual_language = False
        self._default_target = 'en'
        self._submitting = False
        self._running = False

    @property
    def running(self):
        return self._running

    def start(self, target):
        if self._running:
            return
        self._running = True
        self._default_target = target
        self._manual_language = False
        self.draft.set_target(target)
        self.card.set_target(target)
        self.poll.start()
        self._poll()

    def stop(self):
        if not self._running:
            return
        self._running = False
        self.poll.stop()
        if self.job is not None:
            self.job.cancel.set()
        if self.insertion is not None:
            self.insertion.cancel.set()
        self.editor = None
        self.retained.clear()
        self._language_focus_grace = 0.
        self._submitting = False
        self._cancel_sample()
        self._sample_feedback.stop()
        self._sample_message = ''
        self.draft.reset()
        self.alignment.cancel()
        self.card.withdraw(immediate=True)
        self.stopped.emit()

    def _poll(self):
        if self.job is not None or self._submitting or self.card.popup.isVisible():
            return
        sampling = self._sample_active
        checking_empty = False
        if sampling:
            if mouse_buttons_mask() & 1:
                self._sample_pressed = True
                self._sample_until = 0.
                return
            if not self._sample_pressed:
                self._sample_watch = (self._sample_watch + 1) % 4
                if self._sample_watch:
                    return
                sampling = False
                checking_empty = True
            else:
                now = monotonic()
                if not self._sample_until:
                    self._sample_until = now + .55
                    return
                if now >= self._sample_until:
                    self._sample_pressed = False
                    self._sample_until = 0.
                    self._sample_status('没有读到选区，请再划一次')
                    return
        job = DiskWork(lambda cancel: self.probe(cancel, sample=True, at_pointer=True) if sampling else self.probe(cancel))
        self.job = job
        job.signals.done.connect(lambda value, error: self._observed(value, error, sampling, checking_empty), Qt.QueuedConnection)
        self.pool.start(job)

    def _observed(self, value, error, sampling, checking_empty=False):
        job, self.job = self.job, None
        if job is None or job.cancel.is_set() or not self._running or self.card.popup.isVisible():
            return
        if error is not None:
            LOG.debug('Focused translation probe failed: %s', type(error).__name__)
            value = None
        if checking_empty:
            if (self._sample_active and value is not None and self.editor is not None
                    and value.identity == self.editor.identity and not value.text.strip()):
                self.editor = value
                self._cancel_sample()
                self.draft.edit('')
            return
        if sampling:
            if self._sample_active:
                self._observed_sample(value)
            return
        if value is None and self.editor is not None and self._language_focus_grace:
            now = monotonic()
            if self.draft.loading:
                self._language_focus_grace = now + 1.25
            if now < self._language_focus_grace:
                return
        previous, self.editor = self.editor, value
        if value is None and previous is None:
            return
        if value is None or previous is None or value.identity != previous.identity:
            self._change_editor(previous, value)
            return
        if value.text != previous.text:
            self._language_focus_grace = 0.
            self._applied_selection = None
            self.alignment.cancel()
            self._edit_anchor = self._last_edit_position(previous.text, value.text)
            if not self._manual_language and value.text.strip():
                source = quick_content_language(value.text)
                target = self.rules().get(source, self._default_target)
                if target in TARGETS and target != self.draft.target:
                    self.card.set_target(target)
                    self.draft.set_target(target, source=value.text)
                    return
            submitted_boundary = (bool(value.text) and value.text.startswith(previous.text)
                                  and value.text[-1] in '。！？!?;；\n.')
            self.draft.edit(value.text, immediate=submitted_boundary)
        self._render()

    @staticmethod
    def _last_edit_position(before, after):
        # Native providers may expose the text without exposing the caret.
        # The unchanged suffix anchors a mid-document edit without jumping
        # to the end of the whole translation.
        prefix = 0
        for old, new in zip(before, after):
            if old != new:
                break
            prefix += 1
        suffix = 0
        limit = min(len(before), len(after)) - prefix
        while suffix < limit and before[-suffix - 1] == after[-suffix - 1]:
            suffix += 1
        return len(after) - suffix

    def _change_editor(self, previous, value):
        self._applied_selection = None
        self._edit_anchor = len(value.text) if value is not None else 0
        self._language_focus_grace = 0.
        if value is not None and (previous is None or previous.identity != value.identity):
            self.card.drag_offset = QPoint()
        self._cancel_sample()
        self.alignment.cancel()
        if previous is not None and previous.text.strip() and (self.draft.document.target or self._manual_language):
            self.retained.pop(previous.identity, None)
            self.retained[previous.identity] = (
                self.draft.document, self.draft.target, self.draft.result_target, self._manual_language)
            if len(self.retained) > self.RETAINED_EDITORS:
                self.retained.popitem(last=False)
        cached = self.retained.pop(value.identity, None) if value is not None else None
        if cached is not None and value.text.strip():
            document, target, result_target, self._manual_language = cached
            self.card.set_target(target)
            self.draft.restore(document, target, result_target, value.text)
        else:
            self.draft.reset()

    def _toggle_sample(self):
        if self.card.sample.isChecked() and self.editor is not None:
            self._sample_active = True
            if self.job is not None:
                self.job.cancel.set()
            self._sample_feedback.stop()
            self.poll.setInterval(self.SAMPLE_POLL_MS)
            self.card.insert.setEnabled(False)
            self._render()
        else:
            self._cancel_sample()
            self._render()

    def _observed_sample(self, value):
        if not mouse_buttons_mask() & 1 and isinstance(value, EditorSelection) and value.text.strip():
            self._sample_language(value.text)

    def _sample_status(self, text):
        self._sample_message = text
        self._render()

    def _cancel_sample(self):
        self._sample_active = False
        self._sample_pressed = False
        self._sample_until = 0.
        self._sample_message = ''
        self.poll.setInterval(self.POLL_MS)
        self.card.sample.setChecked(False)

    def _sample_language(self, text):
        self._cancel_sample()
        if not text.strip():
            return
        self._submitting = True
        job = DiskWork(lambda cancel: None if cancel.is_set() else content_language(text))
        self.insertion = job

        def detected(code, error):
            if self.insertion is not job:
                return
            self.insertion = None
            self._submitting = False
            if self._running and not job.cancel.is_set():
                if error is None and code in TARGETS:
                    self._choose_language(code)
                else:
                    self._sample_status('未识别选中文字的语言')
                    self._sample_feedback.start()

        job.signals.done.connect(detected, Qt.QueuedConnection)
        self.pool.start(job)

    def _choose_language(self, code):
        self._manual_language = True
        self._cancel_sample()
        self._applied_selection = None
        self._language_focus_grace = monotonic() + 1.25
        self.alignment.cancel()
        self.card.result.setExtraSelections([])
        self.card.set_target(code)
        self.draft.set_target(code)
        self._render()

    def _render(self):
        observed = self.editor
        if not self._running or observed is None or not observed.text.strip() or not self.draft.text.strip():
            self.card.withdraw(immediate=True)
            return
        text = self.draft.result or self.draft.error
        if text != self.card.result.toPlainText():
            scroll = self.card.result.verticalScrollBar()
            previous_scroll = scroll.value()
            self.card.result.setPlainText(text)
            scroll.setValue(previous_scroll)
        if self.card.handle.text() != self._sample_message:
            self.card.handle.setText(self._sample_message)
        self.card.handle.setVisible(bool(self._sample_message))
        if not self.card.set_bounds(observed.bounds):
            return
        self.card.insert.setEnabled(
            self.draft.ready and self.draft.text == observed.text and not self._submitting and not self._sample_active)
        self.card.present()
        selection = observed.selection or (self._edit_anchor, self._edit_anchor)
        aligns_visible_text = selection[0] != selection[1] and self.draft.result_target == self.draft.target
        if (self.draft.ready or aligns_visible_text) and not mouse_buttons_mask() & 1:
            key = selection, observed.text, self.draft.result
            if key != self._applied_selection:
                self._applied_selection = key
                begin, end = selection
                document = self.draft.document
                self.alignment.document = document
                self.alignment.select('source', begin, end, observed.text, self.draft.result)
                if begin == end:
                    self._follow_caret(document, end)

    def _follow_caret(self, document, offset):
        if document.source == document.target:
            self.card.reveal(offset)
            return
        for start, end, target_start, target_end in document.spans:
            if offset <= end:
                self.card.reveal(target_end if offset >= start else target_start)
                return
        self.card.reveal(len(document.target))

    def _insert(self):
        if (not self.draft.ready or self.editor is None or self.draft.text != self.editor.text
                or self._submitting or self._sample_active):
            return
        if self.job is not None:
            self.job.cancel.set()
        self._submitting = True
        expected, text = self.editor, self.draft.result
        job = DiskWork(lambda cancel: self.probe(cancel, expected=expected, insertion=text))
        self.insertion = job

        def inserted(ok, error):
            if self.insertion is not job:
                return
            self.insertion = None
            self._submitting = False
            if not self._running or job.cancel.is_set():
                return
            if ok:
                self.editor = None
                self.draft.reset()
            else:
                self._sample_status('无法替换，请回到原输入框')
                self._sample_feedback.start()
                if error is not None:
                    LOG.debug('Live translation insertion failed: %s', type(error).__name__)

        job.signals.done.connect(inserted, Qt.QueuedConnection)
        self.pool.start(job)
