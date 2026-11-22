"""Floating preview for opted-in translation of the foreground editor."""
import logging
import sys

from PySide6.QtCore import QEvent, QObject, QPoint, QRect, QSize, Qt, QThreadPool, QTimer, Signal
from PySide6.QtGui import QColor, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import QApplication, QHBoxLayout, QLabel, QTextEdit, QVBoxLayout, QWidget

from flickdock_motion import Spring
from live_translation_draft import LiveDraft
from live_translation_editor import EditorSelection, focused_editor
from native_ui_common import colors, font_css_stack, reduced_motion
from platform_native import mouse_buttons_mask
from reference_widgets import ToolbarButton
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
CARD_SIZE = QSize(400, 158)


def fit_card(area, obstacle, size, preferred=None):
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
    for region in available:
        if region.width() < size.width() or region.height() < size.height():
            continue
        target = preferred or QPoint(obstacle.left(), obstacle.bottom() + CARD_GAP + 1)
        point = QPoint(max(region.left(), min(target.x(), region.right() - size.width() + 1)),
                       max(region.top(), min(target.y(), region.bottom() - size.height() + 1)))
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
        self.resize(CARD_SIZE)
        self.setMinimumWidth(252)
        self.setAccessibleName('实时翻译')
        self.anchor_area = QRect()
        self.obstacle = QRect()
        self.desired = None
        self.goal = QRect()
        self.target = 'en'
        self.drag_origin = None
        self.deferred_hide = False
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.surface = TranslatorSurface(self, role='popup_surface', radius=24)
        outer.addWidget(self.surface)
        body = QVBoxLayout(self.surface)
        body.setContentsMargins(14, 11, 14, 12)
        body.setSpacing(5)
        self.handle = QLabel('实时翻译', self.surface)
        self.handle.setCursor(Qt.SizeAllCursor)
        self.handle.installEventFilter(self)
        body.addWidget(self.handle)
        self.result = QTextEdit(self.surface)
        self.result.setFrameShape(QTextEdit.NoFrame)
        self.result.setReadOnly(True)
        self.result.setFocusPolicy(Qt.NoFocus)
        self.result.setTextInteractionFlags(Qt.NoTextInteraction)
        body.addWidget(self.result, 1)
        footer = QHBoxLayout()
        footer.setSpacing(6)
        self.language = LanguageButton(self.surface)
        self.language.setFocusPolicy(Qt.NoFocus)
        self.language.setAccessibleName('目标语言')
        footer.addWidget(self.language)
        self.sample = ToolbarButton('pipette', self.surface)
        self.sample.setFocusPolicy(Qt.NoFocus)
        self.sample.setFixedSize(32, 32)
        self.sample.setCheckable(True)
        self.sample.setToolTip('从下一次划词识别目标语言')
        self.sample.setAccessibleName(self.sample.toolTip())
        footer.addWidget(self.sample)
        footer.addStretch(1)
        self.insert = translator_action('插入', 'return', self.surface, variant='primary')
        self.insert.setFixedHeight(32)
        self.insert.setFocusPolicy(Qt.NoFocus)
        footer.addWidget(self.insert)
        body.addLayout(footer)
        self.popup = LanguagePopup(store, self)
        self.language.clicked.connect(self._choose_target)
        self.sample.clicked.connect(self.sample_requested)
        self.insert.clicked.connect(self.insert_requested)
        self.popup.selected.connect(self.target_changed)
        self.motion = Spring(self, {'y': 12., 'opacity': 0.}, self._frame)
        self.motion.settled.connect(self._settled)
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
        self.handle.setStyleSheet(f"color:{palette['secondary']};font:600 12px {family};")
        self.result.setStyleSheet(f"background:transparent;color:{palette['text']};border:0;font:400 16px {family};")
        self.surface.update()
        self.language.update()

    def set_target(self, code):
        self.target = code
        self.language.set_language(code)

    def set_bounds(self, bounds):
        obstacle, screen = _editor_geometry(bounds)
        if screen is None:
            self.withdraw(immediate=True)
            return False
        self.obstacle = obstacle
        self.anchor_area = screen.availableGeometry().adjusted(10, 10, -10, -10)
        width = min(CARD_SIZE.width(), self.anchor_area.width())
        for candidate in dict.fromkeys((width, min(width, 300), 252)):
            size = QSize(candidate, CARD_SIZE.height())
            if candidate < 252:
                continue
            goal = fit_card(self.anchor_area, self.obstacle, size, self.desired)
            if not goal.isEmpty():
                if goal.size() != self.size():
                    self.resize(goal.size())
                self.goal = goal
                return True
        self.withdraw(immediate=True)
        return False

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
        frame = fit_card(self.anchor_area, self.obstacle, self.size(), requested)
        if not frame.isEmpty() and self.pos() != frame.topLeft():
            self.move(frame.topLeft())

    def eventFilter(self, source, event):
        if source is self.handle:
            if event.type() == QEvent.MouseButtonPress and event.button() == Qt.LeftButton:
                self.drag_origin = event.globalPosition().toPoint() - self.pos()
                return True
            if self.drag_origin is not None and event.type() == QEvent.MouseMove:
                self.desired = event.globalPosition().toPoint() - self.drag_origin
                self.goal = fit_card(self.anchor_area, self.obstacle, self.size(), self.desired)
                self._place(0)
                return True
            if event.type() == QEvent.MouseButtonRelease:
                self.drag_origin = None
                return True
        return super().eventFilter(source, event)

    def _choose_target(self):
        if self.popup.isVisible():
            self.popup.hide()
        else:
            self.popup.configure(self.target, allow_auto=False)
            self.popup.popup_below(self.language)
            self.popup.search.setFocus()

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


class LiveEditorTranslation(QObject):
    stopped = Signal()
    POLL_MS = 80

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
        self.job = None
        self.insertion = None
        self._applied_selection = None
        self._sample_selection = None
        self._sample_active = False
        self._sample_pressed = False
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
        self._submitting = False
        self._cancel_sample()
        self.draft.reset()
        self.alignment.cancel()
        self.card.withdraw(immediate=True)
        self.stopped.emit()

    def _poll(self):
        if self.job is not None or self._submitting or self.card.popup.isVisible():
            return
        sampling = self._sample_active
        job = DiskWork(lambda cancel: self.probe(cancel, sample=True) if sampling else self.probe(cancel))
        self.job = job
        job.signals.done.connect(lambda value, error: self._observed(value, error, sampling), Qt.QueuedConnection)
        self.pool.start(job)

    def _observed(self, value, error, sampling):
        job, self.job = self.job, None
        if job is None or job.cancel.is_set() or not self._running or self.card.popup.isVisible():
            return
        if error is not None:
            LOG.debug('Focused translation probe failed: %s', type(error).__name__)
            value = None
        if sampling:
            if self._sample_active:
                self._observed_sample(value)
            return
        previous, self.editor = self.editor, value
        if value is None or previous is None or value.identity != previous.identity:
            self._applied_selection = None
            self._cancel_sample()
            self.draft.reset()
            return
        if value.text != previous.text:
            self._applied_selection = None
            self.alignment.cancel()
            if not self._manual_language and value.text.strip():
                source = quick_content_language(value.text)
                target = self.rules().get(source, self._default_target)
                if target in TARGETS and target != self.draft.target:
                    self.draft.reset()
                    self.draft.set_target(target)
                    self.card.set_target(target)
            submitted_boundary = (bool(value.text) and value.text.startswith(previous.text)
                                  and value.text[-1] in '。！？!?;；\n.')
            self.draft.edit(value.text, immediate=submitted_boundary)
        self._render()

    def _toggle_sample(self):
        if self.card.sample.isChecked() and self.editor is not None:
            begin, end = self.editor.selection
            self._sample_selection = EditorSelection(self.editor.identity, self.editor.text[begin:end])
            self._sample_active = True
            self.card.insert.setEnabled(False)
        else:
            self._cancel_sample()
            self._render()

    def _observed_sample(self, value):
        if mouse_buttons_mask() & 1:
            self._sample_pressed = True
            return
        pressed, self._sample_pressed = self._sample_pressed, False
        if isinstance(value, EditorSelection) and value.text.strip() and (pressed or value != self._sample_selection):
            self._sample_language(value.text)

    def _cancel_sample(self):
        self._sample_active = False
        self._sample_pressed = False
        self._sample_selection = None
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
                    self.card.handle.setText('未识别选中文字的语言')

        job.signals.done.connect(detected, Qt.QueuedConnection)
        self.pool.start(job)

    def _choose_language(self, code):
        self._manual_language = True
        self._cancel_sample()
        self._applied_selection = None
        self.draft.set_target(code)
        self.card.set_target(code)

    def _render(self):
        observed = self.editor
        if not self._running or observed is None or not self.draft.text.strip():
            self.card.withdraw(immediate=observed is None)
            return
        if not self.card.set_bounds(observed.bounds):
            return
        text = self.draft.result or self.draft.error or '正在翻译…'
        if text != self.card.result.toPlainText():
            self.card.result.setPlainText(text)
        self.card.handle.setText('实时翻译 · 正在更新' if self.draft.loading and self.draft.result else '实时翻译')
        self.card.insert.setEnabled(self.draft.ready and not self._submitting and not self._sample_active)
        self.card.present()
        if self.draft.ready and not mouse_buttons_mask() & 1:
            key = observed.selection, observed.text, self.draft.result
            if key != self._applied_selection:
                self._applied_selection = key
                self.alignment.document = self.draft.document
                self.alignment.select('source', *observed.selection, observed.text, self.draft.result)

    def _insert(self):
        if not self.draft.ready or self.editor is None or self._submitting:
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
                self.card.handle.setText('无法插入 · 请回到原输入框')
                if error is not None:
                    LOG.debug('Live translation insertion failed: %s', type(error).__name__)

        job.signals.done.connect(inserted, Qt.QueuedConnection)
        self.pool.start(job)
