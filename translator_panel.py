"""Independent native translator; business state lives in TranslatorSession."""
from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path
from PySide6.QtCore import QEasingCurve, QEvent, QPoint, QRect, QSize, Qt, QSignalBlocker, QTimer
from PySide6.QtGui import QColor, QPixmap, QTextCursor
from PySide6.QtWidgets import (QApplication, QButtonGroup, QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit,
    QMenu, QPushButton, QScrollArea, QSizePolicy, QTextEdit, QVBoxLayout, QWidget)

from app_settings import ReferenceModalDialog, SettingsCloseButton, SettingsSlider, SettingsToggleSwitch
from display_frames import DisplayAnimation
from flickdock_icons import app_icon
from native_ui_common import RoundedProgressBar, RoundedScrollBar, colors, font_css_stack, reduced_motion
from reference_widgets import SegmentedControl, ToolbarButton, button, css, label, refresh_tree
from translation_api import TranslationApi
from translation_languages import SPEECH_LANGUAGES, TARGETS
from translator_session import TranslatorSession
from translator_store import TranslatorStore
from translator_widgets import (ComparisonEditor, LanguageButton, LanguagePopup, PolishPane, PresetButton, RoundSwapButton, TranslatorPopup,
                                PolishArrival, ReadAlongCaption, StreamingResultEditor, ThinkingText, TranslatorSplitAction, TranslatorSurface, TranslatorToolButton, VoiceListPopup,
                                translator_action, ACTION_SIZE)
from window_chrome import WindowChrome
import ui_theme

WINDOW_SIZE=QSize(1040,720)
LANGUAGE_CAPTION_IDLE_MS=480
COMPACT_FOOTER_WIDTH=944
TRANSLATED_INK={'dark':'#CCCCCC','light':'#464646'}


class TranslatorDialog(ReferenceModalDialog):
    def __init__(self,manager,*,api=None,store=None,detector=None,speech=None):
        self._layout_ready=False
        super().__init__(None,surface_width=1000,modal=False)
        self.manager=manager;self._theme=None;self._theme_key=None;self._closing=False;self._pinning=False;self._transport=None
        self._preset_key=None;self._shown_result=None;self._shown_source=None;self._active_preset=''
        self._compact_footer=None;self._notice='';self._notice_role='secondary';self._shown_notice_role='red'
        self._was_translating=False
        self._was_polishing=False
        self._last_polish_revision={}
        self._polish_arrival=PolishArrival(self)
        self._popups=[];self._managers={}
        self.setFont(manager.shelf.font());self.setWindowTitle('FlickDock · 翻译器');self.setMinimumSize(800,570)
        if api is None:
            from cloud_api import CloudApi
            self._transport=CloudApi(manager.account.api.origin,self);api=TranslationApi(self._transport,self)
        self.store=store or TranslatorStore(manager.settings.base,self)
        self.session=TranslatorSession(api,self.store,self,**({'detector':detector} if detector else {}))
        from translator_speech import TranslatorSpeech
        from breeze_speech_api import BreezeSpeechApi
        self.speech=speech or TranslatorSpeech(self,api=BreezeSpeechApi(getattr(manager,'account',None),self))
        if self.speech.parent() is None:self.speech.setParent(self)
        self.speech.error.connect(self._message)
        self.speech_bars={};self.speech_buttons={};self.speech_playback={};self._speech_seen={};self._speech_dismissed={};self._tts_side='target';self._await_source_speech=None
        self._tts_language=None
        self.surface.layout().removeWidget(self.scroll);self.scroll.hide()
        self.body.removeEventFilter(self);self._fit_timer.stop()
        self.title_bar=QWidget(self.surface);self.title_bar.setFixedHeight(50)
        head=QHBoxLayout(self.title_bar);head.setContentsMargins(16,0,8,0);head.setSpacing(7)
        from reference_widgets import overview_back_button
        head.addWidget(label('翻译器',16,600,parent=self.title_bar))
        self.back_button=overview_back_button(self.title_bar,lambda:manager.return_to_overview(self));head.addWidget(self.back_button);head.addStretch(1)
        self.glossary_button=button('术语保护','shield',variant='secondary',parent=self.title_bar);self.glossary_button.clicked.connect(lambda:self.show_manager('glossary'));head.addWidget(self.glossary_button)
        self.presets_button=button('润色预设','tag',variant='secondary',parent=self.title_bar);self.presets_button.clicked.connect(lambda:self.show_manager('presets'));head.addWidget(self.presets_button)
        self.history_button=button('历史','status-clock',variant='secondary',parent=self.title_bar);self.history_button.clicked.connect(self.toggle_history);head.addWidget(self.history_button)
        for control in (self.glossary_button,self.presets_button,self.history_button):
            control.setFixedHeight(ACTION_SIZE);control.setIconSize(QSize(14,14))
            css(control,*[f"QPushButton{{background:transparent;color:{colors(t)['text']};border:0;padding:3px 8px;font:500 10px {font_css_stack()};}}" for t in ('dark','light')])
        self.pin_button=ToolbarButton('pin',self.title_bar,motion=False);self.pin_button.setFixedSize(30,30)
        self.pin_button.setCheckable(True);self.pin_button.setToolTip('置顶窗口');self.pin_button.clicked.connect(self._pin);head.addWidget(self.pin_button)
        self.close_button=SettingsCloseButton(self.title_bar,glyph_size=14);self.close_button.setFixedSize(30,30)
        self.close_button.setAccessibleName('关闭翻译器');self.close_button.setToolTip('关闭翻译器');self.close_button.clicked.connect(self.close);head.addWidget(self.close_button)
        self.surface.layout().insertWidget(0,self.title_bar)
        self.workspace=QWidget(self.surface);self.surface.layout().addWidget(self.workspace,1)
        body=QVBoxLayout(self.workspace);body.setContentsMargins(16,4,16,16);body.setSpacing(12)
        panes=QGridLayout();panes.setSpacing(12);panes.setColumnStretch(0,1);panes.setColumnStretch(1,1)
        panes.setRowStretch(1,1);body.addLayout(panes,1)
        self._language_groups={}
        for column,side in enumerate(('source','target')):
            group=QWidget(self.workspace);row=QHBoxLayout(group);row.setContentsMargins(0,0,0,0);row.setSpacing(8)
            group.setSizePolicy(QSizePolicy.Ignored,QSizePolicy.Fixed)
            control=LanguageButton(group);control.setSizePolicy(QSizePolicy.Maximum,QSizePolicy.Fixed);control.setMaximumWidth(178);control.setToolTip('选择原文语言' if side=='source' else '选择译文语言');control.setAccessibleName(control.toolTip());control.clicked.connect(lambda checked=False,side=side:self.show_language(side));row.addWidget(control)
            setattr(self,side+'_language',control)
            favorites=QHBoxLayout();favorites.setSpacing(5);row.addLayout(favorites)
            setattr(self,'source_favorites_row' if side=='source' else 'favorites_row',favorites)
            row.addStretch(1);panes.addWidget(group,0,column)
            self._language_groups[side]=(group,control,favorites);group.installEventFilter(self)
        self.swap_button=RoundSwapButton(self.workspace);self.swap_button.setToolTip('交换语言和文本');self.swap_button.clicked.connect(self.session.swap)
        self.source_card=PolishPane(self.workspace);source=QVBoxLayout(self.source_card);source.setContentsMargins(14,12,14,10);source.setSpacing(8)
        self.source_card.setSizePolicy(QSizePolicy.Ignored,QSizePolicy.Expanding)
        self.source_task=ThinkingText(self.source_card)
        source.addWidget(self.source_task)
        self.source_version_row=QHBoxLayout();self.source_version_label=label('原始原文',10,400,'secondary',self.source_card)
        self.source_version_row.addWidget(self.source_version_label);self.source_version_row.addStretch(1)
        self.source_compare_button=translator_action('对比原文','compare',self.source_card);self.source_compare_button.setCheckable(True)
        self.source_compare_button.setToolTip('对比润色前的原文');self.source_compare_button.toggled.connect(lambda _checked:self.refresh())
        self.source_version_row.addWidget(self.source_compare_button)
        self.source_undo_button=self._tool('undo','撤销上次润色原文',lambda:self.session.undo('source'),self.source_card);self.source_version_row.addWidget(self.source_undo_button)
        self.source_regenerate_button=self._tool('refresh','重新生成原文润色',lambda:self.session.regenerate('source'),self.source_card);self.source_version_row.addWidget(self.source_regenerate_button)
        self.source_replace_button=translator_action('确认并翻译','check',self.source_card,variant='primary')
        self.source_replace_button.setToolTip('确认将当前原文作为基准并翻译')
        self.source_replace_button.clicked.connect(lambda:self.session.replace_original('source'));self.source_version_row.addWidget(self.source_replace_button)
        source.addLayout(self.source_version_row)
        self.source_comparison=TranslatorSurface(self.source_card,role='control',radius=8);comparison=QVBoxLayout(self.source_comparison)
        comparison.setContentsMargins(10,7,10,7)
        head=QHBoxLayout();head.addWidget(label('基准原文',10,400,'secondary',self.source_comparison),1)
        restore=button('恢复原文',parent=self.source_comparison);restore.clicked.connect(lambda:self.session.restore_original('source'));head.addWidget(restore)
        self.close_source_comparison=self._tool('close','关闭原文对照',lambda:self.source_compare_button.setChecked(False),self.source_comparison)
        head.addWidget(self.close_source_comparison);comparison.addLayout(head)
        self.source_baseline_view=self._editor(self.source_comparison,'',comparison=True)
        comparison.addWidget(self.source_baseline_view)
        self.source_comparison.hide();source.addWidget(self.source_comparison)
        self.source_editor=self._editor(self.source_card,'输入或粘贴需要翻译的文字…',result=True);source.addWidget(self.source_editor,1)
        self.source_baseline_view.set_reveal_column(self.source_editor)
        source.addWidget(self._speech_bar('source',self.source_card))
        self.source_editor.textChanged.connect(lambda:self.session.edit_source(self.source_editor.toPlainText()))
        foot=QHBoxLayout();foot.setSpacing(4);self.counter=label('0 字符',10,400,'muted',self.source_card);foot.addWidget(self.counter,1)
        self.delay_group=TranslatorSplitAction('自动翻译','status-clock',self.source_card)
        self.translate_button=self.delay_group.primary
        self.translate_button.clicked.connect(self._translate_action)
        self.delay_button=self.delay_group.secondary;self.delay_button.clicked.connect(self.show_delay);foot.addWidget(self.delay_group)
        self.counter.setMinimumWidth(0);self.counter.setSizePolicy(QSizePolicy.Ignored,QSizePolicy.Preferred)
        foot.addWidget(self._speech_group('source',self.source_card))
        self.clear_source_button=self._tool('close','清空原文',self._clear_or_undo_source,self.source_card)
        foot.addWidget(self.clear_source_button)
        self.source_copy_button=translator_action('复制原文','copy',self.source_card)
        self.source_copy_button.clicked.connect(lambda:self._copy(False));foot.addWidget(self.source_copy_button)
        source.addLayout(foot);panes.addWidget(self.source_card,1,0)
        self.output_card=PolishPane(self.workspace);output=QVBoxLayout(self.output_card);output.setContentsMargins(14,12,14,10);output.setSpacing(8)
        self.output_card.setSizePolicy(QSizePolicy.Ignored,QSizePolicy.Expanding)
        self.output_task=ThinkingText(self.output_card)
        output.addWidget(self.output_task)
        version=QHBoxLayout();self.version_label=label('原始译文',10,400,'secondary',self.output_card);version.addWidget(self.version_label);version.addStretch(1)
        self.compare_button=translator_action('对比原译文','compare',self.output_card);self.compare_button.setCheckable(True);self.compare_button.toggled.connect(lambda _checked:self.refresh());version.addWidget(self.compare_button)
        self.undo_button=self._tool('undo','撤销上次润色',lambda:self.session.undo('target'),self.output_card);version.addWidget(self.undo_button)
        self.regenerate_button=self._tool('refresh','重新生成上次润色',lambda:self.session.regenerate('target'),self.output_card);version.addWidget(self.regenerate_button)
        self.replace_button=self._tool('check','将当前版本设为基准译文',lambda:self.session.replace_original('target'),self.output_card);version.addWidget(self.replace_button);output.addLayout(version)
        self.comparison=TranslatorSurface(self.output_card,role='control',radius=8);comparison=QVBoxLayout(self.comparison);comparison.setContentsMargins(10,7,10,7)
        compare_head=QHBoxLayout();self.compare_head_label=label('基准原译文',10,400,'secondary',self.comparison);compare_head.addWidget(self.compare_head_label,1)
        self.restore_compare_button=button('恢复原译文',parent=self.comparison);self.restore_compare_button.clicked.connect(lambda:self.session.restore_original('target'));compare_head.addWidget(self.restore_compare_button)
        self.close_target_comparison=self._tool('close','关闭译文对照',lambda:self.compare_button.setChecked(False),self.comparison)
        compare_head.addWidget(self.close_target_comparison);comparison.addLayout(compare_head)
        self.original_view=self._editor(self.comparison,'',comparison=True);self.original_view.setObjectName('translatorResult');comparison.addWidget(self.original_view);self.comparison.hide();output.addWidget(self.comparison)
        self.output_editor=self._editor(self.output_card,'译文将在这里显示，也可以直接修改…',result=True);self.output_editor.setObjectName('translatorResult');self.output_editor.textChanged.connect(lambda:self.session.edit_result(self.output_editor.toPlainText()));output.addWidget(self.output_editor,1)
        self.original_view.set_reveal_column(self.output_editor)
        output.addWidget(self._speech_bar('target',self.output_card))
        target_footer=QHBoxLayout();target_footer.setSpacing(4);self.status=label('就绪',10,400,'muted',self.output_card);target_footer.addWidget(self.status,1)
        self.status.setMinimumWidth(0);self.status.setSizePolicy(QSizePolicy.Ignored,QSizePolicy.Preferred)
        target_footer.addWidget(self._speech_group('target',self.output_card))
        self.favorite_button=self._tool('star','收藏这条翻译',self.session.toggle_favorite,self.output_card);target_footer.addWidget(self.favorite_button)
        self.copy_button=translator_action('复制译文','copy',self.output_card);self.copy_button.clicked.connect(lambda:self._copy(True));target_footer.addWidget(self.copy_button);output.addLayout(target_footer)
        panes.addWidget(self.output_card,1,1)
        panes.addWidget(self.swap_button,1,0,1,2,Qt.AlignCenter);self.swap_button.raise_()
        self.error_label=label('',11,400,'red',self.workspace);self.error_label.setWordWrap(True);body.addWidget(self.error_label)
        polish=QHBoxLayout();polish.addWidget(label('AI 润色',12,600,parent=self.workspace));polish.addStretch();self.scope_hint=label('基于当前译文',10,400,'secondary',self.workspace);polish.addWidget(self.scope_hint);body.addLayout(polish)
        preset_line=QHBoxLayout();preset_line.setSpacing(6)
        self.preset_scroll=QScrollArea(self.workspace);self.preset_scroll.setWidgetResizable(True);self.preset_scroll.setFrameShape(QFrame.NoFrame);self.preset_scroll.setFixedHeight(32);self.preset_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.preset_body=QWidget(self.preset_scroll);self.preset_layout=QHBoxLayout(self.preset_body);self.preset_layout.setContentsMargins(0,0,0,0);self.preset_layout.setSpacing(7);self.preset_scroll.setWidget(self.preset_body);preset_line.addWidget(self.preset_scroll,1)
        self.preset_body.setAutoFillBackground(False);self.preset_scroll.viewport().setAutoFillBackground(False)
        body.addLayout(preset_line)
        composer=QHBoxLayout();composer.setSpacing(8);self.composer_surface=TranslatorSurface(self.workspace,role='card',radius=20);self.composer_surface.setFixedHeight(40)
        edit_row=QHBoxLayout(self.composer_surface);edit_row.setContentsMargins(4,4,8,4);edit_row.setSpacing(6)
        self.polish_scope_segment=SegmentedControl(self.composer_surface);self.polish_scope_group=QButtonGroup(self);self.polish_scope_group.setExclusive(True);self.polish_scope_buttons={}
        self.polish_scope_segment.set_surface_background('subcard','subcard',16,stroke_role='border')
        self.polish_scope_segment.setToolTip('选择润色文字的范围')
        for scope,title in (('source','原文'),('target','译文')):
            control=button(title,parent=self.polish_scope_segment);self.polish_scope_segment.add_button(control,48);self.polish_scope_group.addButton(control)
            control.clicked.connect(lambda checked=False,scope=scope:self._set_polish_scope(scope));self.polish_scope_buttons[scope]=control
        edit_row.addWidget(self.polish_scope_segment)
        self.instruction=QLineEdit(self.composer_surface);self.instruction.setMaxLength(1000);self.instruction.setPlaceholderText('告诉 AI 如何修改这段译文…');self.instruction.setFixedHeight(30);self.instruction.setMinimumWidth(0)
        css(self.instruction,*[f'QLineEdit{{background:transparent;color:{colors(t)["text"]};border:0;padding:3px 7px;font:12px {font_css_stack()};}}' for t in ('dark','light')]);edit_row.addWidget(self.instruction,1)
        self.clear_instruction=self._tool('close','清空润色要求',self.instruction.clear,self.composer_surface)
        edit_row.addWidget(self.clear_instruction);composer.addWidget(self.composer_surface,1)
        self.save_prompt_button=button('存为预设',parent=self.workspace);self.save_prompt_button.clicked.connect(self.save_prompt);composer.addWidget(self.save_prompt_button)
        self.polish_button=button('润色',variant='primary',parent=self.workspace)
        self.polish_button.setFixedSize(76,40);self.polish_button.setProperty('pillShape',True)
        self.polish_button.clicked.connect(self._polish);composer.addWidget(self.polish_button);body.addLayout(composer)
        self.instruction.textChanged.connect(lambda value:self.save_prompt_button.setVisible(bool(value.strip())))
        self.instruction.textChanged.connect(lambda value:self.clear_instruction.setVisible(bool(value)))
        self.instruction.returnPressed.connect(self.polish_button.click)
        self.instruction.installEventFilter(self)
        self.delay_popup=TranslatorPopup(self,width=290);self._popups.append(self.delay_popup)
        delay_row=QHBoxLayout();delay_row.addWidget(label('自动翻译',12,500,parent=self.delay_popup.surface),1)
        self.live_toggle=SettingsToggleSwitch('',self.delay_popup.surface);self.live_toggle.setToolTip('自动翻译');self.live_toggle.toggled.connect(self.session.set_live);delay_row.addWidget(self.live_toggle);self.delay_popup.body.addLayout(delay_row)
        self.delay_value=label('',11,400,'secondary',self.delay_popup.surface);self.delay_popup.body.addWidget(self.delay_value)
        self.delay_slider=SettingsSlider(Qt.Horizontal,self.delay_popup.surface);self.delay_slider.setToolTip('自动翻译延迟');self.delay_slider.setRange(1,50);self.delay_slider.valueChanged.connect(lambda value:self.session.set_delay(value*100));self.delay_popup.body.addWidget(self.delay_slider)
        self.delay_slider.valueCaption=lambda value:f'{value/10:g} 秒'
        self.translate_now_button=button('翻译一次',variant='secondary',parent=self.delay_popup.surface);self.translate_now_button.clicked.connect(lambda:(self.delay_popup.hide(),self.session.translate()));self.delay_popup.body.addWidget(self.translate_now_button)
        self._build_tts_popup()
        self.language_popups={}
        for side,control in (('source',self.source_language),('target',self.target_language)):
            popup=LanguagePopup(self.store,self,side=side);popup.selected.connect(lambda code,side=side:self.session.set_language(side,code));control.chevron.attach(popup);self.language_popups[side]=popup;self._popups.append(popup)
        from translator_history import TranslatorHistory
        self.history=TranslatorHistory(self,self.surface)
        # Keep a translucent window on Qt's client resize path on Windows.
        # A system resize adds a native thick frame and a second resize loop.
        self.chrome=WindowChrome(self,self.title_bar,manager.settings,'translator',surface=self.surface,system_resize=sys.platform!='win32');self.chrome.default_size=QSize(WINDOW_SIZE)
        if sys.platform=='darwin':
            from window_chrome import mac_caption_controls
            self.close_button.hide()
            self.caption_controls=mac_caption_controls(self,self.title_bar)
            head.insertWidget(0,self.caption_controls)
        for control in self.title_bar.findChildren(QPushButton):
            control.setAutoDefault(False);control.setDefault(False)
        self.resize(WINDOW_SIZE)
        self.session.changed.connect(self.refresh);self.store.changed.connect(self._store_changed);manager.settings.changed.connect(self.refresh)
        self.session.result_ready.connect(self._auto_speak);self.session.source_detected.connect(self._source_speech_ready)
        self.session.source_replaced.connect(self._replace_source)
        self.speech.changed.connect(self._speech_changed);self.speech.voices_changed.connect(self._voices_changed)
        self._copy_feedback=QTimer(self);self._copy_feedback.setSingleShot(True);self._copy_feedback.setInterval(1400);self._copy_feedback.timeout.connect(lambda:self._copy_appearance(True,False))
        self._source_copy_feedback=QTimer(self);self._source_copy_feedback.setSingleShot(True);self._source_copy_feedback.setInterval(1400);self._source_copy_feedback.timeout.connect(lambda:self._copy_appearance(False,False))
        self._notice_timer=QTimer(self);self._notice_timer.setSingleShot(True);self._notice_timer.setInterval(6000);self._notice_timer.timeout.connect(self._expire_notice)
        self._caption_text='';self._caption_language=''
        self._caption_idle=QTimer(self);self._caption_idle.setSingleShot(True);self._caption_idle.setInterval(LANGUAGE_CAPTION_IDLE_MS)
        self._caption_idle.timeout.connect(self._refresh_language_caption)
        self._speech_dots=0;self._speech_pulse=QTimer(self);self._speech_pulse.setInterval(380);self._speech_pulse.timeout.connect(self._speech_tick)
        from translation_alignment import SelectionAlignment
        self.alignment=SelectionAlignment(self.session.document,TranslationApi(self._transport,self) if self._transport else None,self)
        self.alignment.presentation.connect(self._present_alignment);self._alignment_pair=None
        self._alignment_side=None;self._alignment_status=''
        self._favorite_key=None
        self.source_editor.selectionChanged.connect(lambda:self._mirror_selection('source'))
        self.output_editor.selectionChanged.connect(lambda:self._mirror_selection('target'))
        for editor in (self.source_editor,self.output_editor):editor.viewport().installEventFilter(self)
        self._layout_ready=True
        self._update_compact_footer()
        self.refresh()
        if self.store.ready:self._apply_pin(self.store.data['pinned'])

    def _rebuild_favorites(self):
        key={side:tuple(self.store.favorites_for(side)[:4]) for side in ('source','target')}
        previous=self._favorite_key or {}
        if key==previous:return
        self._favorite_key=key
        for side,row in (('source',self.source_favorites_row),('target',self.favorites_row)):
            if key[side]==previous.get(side):continue
            while row.count():
                item=row.takeAt(0);widget=item.widget()
                if widget is not None:widget.hide();widget.deleteLater()
            for code in key[side]:
                title=TARGETS[code][0]
                chip=translator_action(title,'',self._language_groups[side][0],padding=11)
                chip.setProperty('pillShape',True)
                chip.setToolTip(('常用原文语言' if side=='source' else '常用译文语言')+' · '+title)
                chip.setAccessibleName(chip.toolTip())
                chip.clicked.connect(lambda checked=False,code=code,side=side:self.session.set_language(side,code))
                row.addWidget(chip)
            self._fit_favorites(side)

    def _fit_favorites(self,side):
        group,menu,row=self._language_groups[side]
        available=group.width()-max(menu.minimumWidth(),min(menu.maximumWidth(),menu.sizeHint().width()))-8
        for index in range(row.count()):
            chip=row.itemAt(index).widget();width=chip.sizeHint().width()
            chip.setVisible(width<=available)
            available-=width+row.spacing()

    def _mirror_selection(self,side):
        from translation_document import editor_to_text_position
        editor=self.source_editor if side=='source' else self.output_editor
        source,target=self.session.source_text,self.session.result
        text=source if side=='source' else target;cursor=editor.textCursor()
        if cursor.hasSelection():
            self._alignment_side=side
            other=self.output_editor if side=='source' else self.source_editor
            previous=other.textCursor()
            if previous.hasSelection():
                previous.clearSelection()
                with QSignalBlocker(other):other.setTextCursor(previous)
        elif self._alignment_side not in (None,side):return
        else:self._alignment_side=None
        self.alignment.select(side,editor_to_text_position(text,cursor.selectionStart()),
            editor_to_text_position(text,cursor.selectionEnd()),source,target)

    def _present_alignment(self,side,exact,context,status):
        self._selection_highlight(side,exact,context)
        self._alignment_status=status;self._refresh_output_status()

    def _selection_highlight(self,side,ranges,context=(),*,scroll=True):
        self._alignment_highlights=(side,ranges,context) if ranges or context else None
        from translation_document import text_to_editor_position
        other=self.source_editor if side=='source' else self.output_editor
        text=self.session.source_text if side=='source' else self.session.result
        highlights=[]
        selected=self.output_editor if side=='source' else self.source_editor
        if selected.extraSelections():selected.setExtraSelections([])
        theme=colors(self.manager.settings.theme)
        for spans,exact in ((context,False),(ranges,True)):
            colour=QColor(theme['action'])
            if not exact:colour.setAlpha(33)
            for begin,end in spans:
                selection=QTextEdit.ExtraSelection();selection.format.setBackground(colour)
                if exact:selection.format.setForeground(QColor(theme['on_action']))
                cursor=other.textCursor()
                cursor.setPosition(text_to_editor_position(text,begin));cursor.setPosition(text_to_editor_position(text,end),QTextCursor.KeepAnchor)
                selection.cursor=cursor;highlights.append(selection)
        if highlights:other.finish_reveal()
        other.setExtraSelections(highlights)
        if highlights and scroll:
            cursor=QTextCursor(highlights[-1 if ranges else 0].cursor)
            cursor.clearSelection()
            rect=other.cursorRect(cursor)
            if rect.top()<0 or rect.bottom()>other.viewport().height():
                # QPlainTextEdit scroll units are lines, not pixels. Let Qt
                # position this cursor, then restore the existing caret.
                bar=other.verticalScrollBar();original=other.textCursor()
                with QSignalBlocker(other):
                    other.setTextCursor(cursor);other.centerCursor();position=bar.value()
                    other.setTextCursor(original);bar.setValue(position)

    def _download_speech(self,side):
        text=self.session.result if side=='target' else self.session.source_text
        if not text.strip():return
        code=self._speech_language(side)
        if code not in SPEECH_LANGUAGES:self._message('当前语言暂不支持语音。');return
        voice=self.store.voice_for(side,code).get('id','')
        from PySide6.QtWidgets import QFileDialog
        default=str(Path.home()/'FlickDock-语音.mp3')
        path,_=QFileDialog.getSaveFileName(self,'保存语音文件',default,'MP3 音频 (*.mp3)')
        if not path:return
        if not path.lower().endswith('.mp3'):path+='.mp3'
        self._message('正在生成语音文件…',role='secondary')
        self.speech.export_audio(path,text,code,self._speech_exported,voice=voice)

    def _speech_exported(self,path):
        self._message('语音已保存：'+str(path),role='green')

    def _editor(self,parent,placeholder,*,comparison=False,result=False):
        edit=ComparisonEditor(parent) if comparison else StreamingResultEditor(parent) if result else QPlainTextEdit(parent)
        edit.setPlaceholderText(placeholder);edit.setFrameShape(QFrame.NoFrame)
        if not comparison:edit.setMinimumSize(0,80)
        edit.setVerticalScrollBar(RoundedScrollBar(Qt.Vertical,edit))
        css(edit,'QScrollBar:vertical{width:6px;background:transparent;} QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical{height:0;} QScrollBar::handle:vertical{min-height:18px;background:transparent;}')
        edit.setSizePolicy(QSizePolicy.Ignored,QSizePolicy.Expanding);return edit

    def _tool(self,glyph,title,callback,parent):
        control=TranslatorToolButton(glyph,parent);control.setToolTip(title);control.setAccessibleName(title);control.clicked.connect(callback);return control

    def eventFilter(self,watched,event):
        if event.type()==QEvent.Resize and self._layout_ready:
            for side,(group,_,_) in self._language_groups.items():
                if watched is group:self._fit_favorites(side);break
        if (self._layout_ready and event.type()==QEvent.MouseButtonRelease and event.button()==Qt.LeftButton
                and watched in (self.source_editor.viewport(),self.output_editor.viewport())):
            QTimer.singleShot(0,self,self.alignment.pointer_released)
        if watched is getattr(self,'instruction',None) and event.type()==QEvent.KeyPress and event.key() in (Qt.Key_Return,Qt.Key_Enter):
            if not event.isAutoRepeat():self.polish_button.click()
            return True
        for surface,status,subtitle,progress,duration,replay,download in getattr(self,'speech_bars',{}).values():
            if watched is not progress:continue
            if event.type() in (QEvent.MouseButtonPress,QEvent.MouseButtonDblClick):
                self._seek_speech(progress,event.position().x());return True
            if event.type()==QEvent.MouseMove and event.buttons() & Qt.MouseButton.LeftButton:
                self._seek_speech(progress,event.position().x());return True
        return super().eventFilter(watched,event)

    def _seek_speech(self,progress,x):
        width=max(1,progress.width());self.speech.seek(max(0.,min(1.,x/width)))

    def _speech_group(self,side,parent):
        group=TranslatorSplitAction('转语音','play',parent)
        play,tune=group.primary,group.secondary;tune.setFixedWidth(ACTION_SIZE-2)
        play.setToolTip('朗读');play.setAccessibleName('朗读');play.clicked.connect(lambda:self._toggle_speech(side))
        tune.setToolTip('语音设置');tune.setAccessibleName('语音设置');tune.clicked.connect(lambda:self.show_tts(side))
        self.speech_buttons[side]=(play,tune);return group

    def _toggle_speech(self,side):
        if self.speech.active and self.speech.side==('target' if side=='target' else 'source'):
            self.speech.toggle_pause();return
        self._speak(side=='target')

    def _speech_bar(self,side,parent):
        surface=TranslatorSurface(parent,role='control',radius=8,border=False)
        box=QVBoxLayout(surface);box.setContentsMargins(10,7,10,12);box.setSpacing(6)
        row=QHBoxLayout();row.setSpacing(4);status=label('正在生成语音…',10,400,'secondary',surface);status.setMinimumWidth(0);status.setSizePolicy(QSizePolicy.Ignored,QSizePolicy.Preferred);row.addWidget(status,1)
        replay=self._tool('refresh','从头重新朗读',lambda:self._speak(side=='target',force=True),surface);row.addWidget(replay)
        play=self._tool('pause','暂停朗读',lambda:self._toggle_speech(side),surface);row.addWidget(play)
        self.speech_playback[side]=play
        download=self._tool('download','下载语音文件',lambda:self._download_speech(side),surface);row.addWidget(download)
        stop=self._tool('close','停止并关闭朗读卡片',lambda checked=False,side=side:self._dismiss_speech(side),surface);row.addWidget(stop);box.addLayout(row)
        subtitle=ReadAlongCaption(surface)
        subtitle.seek_requested.connect(self.speech.seek_word)
        box.addWidget(subtitle)
        progress=RoundedProgressBar(surface);progress.setTextVisible(False);progress.setFixedHeight(4)
        progress.setCursor(Qt.PointingHandCursor);progress.setToolTip('拖动调整朗读位置');progress.installEventFilter(self)
        box.addWidget(progress)
        footer=QHBoxLayout();footer.setSpacing(6)
        duration=label('0:00',10,400,'muted',surface);footer.addWidget(duration,1);box.addLayout(footer)
        self.speech_bars[side]=(surface,status,subtitle,progress,duration,replay,download);surface.hide();return surface

    def _build_tts_popup(self):
        self.tts_popup=TranslatorPopup(self,width=330);self._popups.append(self.tts_popup);body=self.tts_popup.body;surface=self.tts_popup.surface
        heading=QHBoxLayout();heading.addWidget(label('文本转语音',12,600,parent=surface),1);heading.addWidget(label('BreezeBlue',10,400,'secondary',surface));body.addLayout(heading)
        self.tts_rate_value=label('语速 1×',11,400,parent=surface);body.addWidget(self.tts_rate_value)
        self.tts_rate=SettingsSlider(Qt.Horizontal,surface);self.tts_rate.setToolTip('朗读语速');self.tts_rate.setRange(50,200);self.tts_rate.setSingleStep(5);self.tts_rate.valueChanged.connect(lambda value:self._tts_preference(tts_rate=value/100));body.addWidget(self.tts_rate)
        self.tts_rate.valueCaption=lambda value:f'{value/100:g}×'
        presets=QHBoxLayout();presets.setSpacing(5)
        for rate in (.75,1.,1.25,1.5):
            control=button(f'{rate:g}×',parent=surface);control.clicked.connect(lambda checked=False,rate=rate:self.tts_rate.setValue(round(rate*100)));presets.addWidget(control)
        body.addLayout(presets)
        self.tts_pitch_value=label('音调偏好 · 标准',11,400,parent=surface);body.addWidget(self.tts_pitch_value)
        self.tts_pitch=SettingsSlider(Qt.Horizontal,surface);self.tts_pitch.setToolTip('朗读音调');self.tts_pitch.setRange(80,120);self.tts_pitch.setSingleStep(5);self.tts_pitch.valueChanged.connect(lambda value:self._tts_preference(tts_pitch=value/100));body.addWidget(self.tts_pitch)
        self.tts_pitch.valueCaption=lambda value:f'{value/100:g}×'
        voices=QHBoxLayout();voices.addWidget(label('音色',11,400,parent=surface),1);self.tts_audition=button('试听音色',parent=surface);self.tts_audition.clicked.connect(lambda:self._audition(self._voice_selection().get('id','')));voices.addWidget(self.tts_audition);body.addLayout(voices)
        self.voice_popup=VoiceListPopup(self);self._popups.append(self.voice_popup)
        self.voice_popup.chosen.connect(self._choose_voice)
        self.voice_popup.audition.connect(self._audition)
        self.tts_voice_button=button('自动选择音色',variant='secondary',parent=surface)
        self.tts_voice_button.setToolTip('选择朗读音色');self.tts_voice_button.setAccessibleName(self.tts_voice_button.toolTip())
        self.tts_voice_button.clicked.connect(self._show_voices);body.addWidget(self.tts_voice_button)
        auto=QHBoxLayout();auto.addWidget(label('翻译完成后自动朗读',11,400,parent=surface),1);self.tts_auto=SettingsToggleSwitch('',surface);self.tts_auto.setToolTip('翻译完成后自动朗读');self.tts_auto.toggled.connect(lambda value:self._tts_preference(tts_auto=value));auto.addWidget(self.tts_auto);body.addLayout(auto)
        body.addWidget(label('语速与音调更改在下次朗读生效',10,400,'muted',surface))

    def _tts_preference(self,**values):self.store.update_preferences(**values)

    def _speech_language(self, side):
        state=self.session
        if side=='target':
            return state.target
        return state.detected_code if state.source=='auto' else state.source

    def _voice_selection(self):
        return self.store.voice_for(self._tts_side,self._speech_language(self._tts_side))

    def _choose_voice(self, key):
        code=self._speech_language(self._tts_side)
        if code!=self._tts_language or code not in SPEECH_LANGUAGES:
            return
        row=next((row for row in self.speech.catalog if row['key']==key),None)
        if row is not None or not key:
            self.store.set_voice(self._tts_side,code,key,row['name'] if row else '')

    def _browse_voices(self, *, refresh=False):
        code=self._speech_language(self._tts_side)
        if code!=self._tts_language or refresh:
            self.voice_popup.hide()
            self._tts_language=code
            self.speech.browse_language(code if code in SPEECH_LANGUAGES else None)

    def show_tts(self,side):
        if self.tts_popup.isVisible() and self._tts_side==side:
            self.tts_popup.hide();return
        for popup in self._popups:popup.hide()
        self._tts_side=side
        self._browse_voices(refresh=True)
        self.tts_popup.popup_below(self.speech_buttons[side][1])
        if side=='source' and self.session.source=='auto' and self._tts_language is None:
            self.session.detect_only()
    def _show_voices(self):
        if self.voice_popup.isVisible():self.voice_popup.hide();return
        self.voice_popup.configure(self.speech.catalog,self._voice_selection().get('id',''))
        self.voice_popup.popup_below(self.tts_voice_button)

    def _audition(self,key):
        if self.speech.active and self.speech.side=='audition' and self.speech.audition_key==key:self.speech.stop();return
        self.speech.audition(key)

    def _voices_changed(self):
        if not hasattr(self,'tts_voice_button'):return
        selection=self._voice_selection()
        current=selection.get('id','')
        code=self._speech_language(self._tts_side)
        catalog=self.speech.catalog_for(code)
        name=next((voice['name'] for voice in catalog or () if voice['key']==current),'')
        if code is None:
            title='请先输入原文或选择语言'
        elif code not in SPEECH_LANGUAGES:
            title='当前语言暂不支持语音'
        elif catalog is not None and not catalog:
            title='当前语言没有可选音色'
        else:
            title=name or (selection.get('name') if catalog is None else '') or '自动选择音色'
        self.tts_voice_button.setText(title)
        self.tts_voice_button.setEnabled(bool(catalog))
        self.tts_pitch.setEnabled(self.speech.pitch_supported)
        self.tts_pitch.setToolTip('通过 BreezeBlue 音调指令调节，支持中文和英文' if self.speech.pitch_supported else '当前语言不支持音调指令')
        if self.voice_popup.isVisible():self.voice_popup.configure(self.speech.catalog,current)
    def _speech_tick(self):
        self._speech_dots=(self._speech_dots+1)%4
        self._speech_changed()

    @staticmethod
    def _clock(seconds):
        seconds=max(0,int(round(seconds or 0)))
        return f'{seconds//60}:{seconds%60:02d}'

    def _speech_changed(self):
        if not hasattr(self,'speech_bars'):return
        speech=self.speech
        audition=self.speech.active and self.speech.side=='audition'
        self.voice_popup.set_playback(self.speech.audition_key if audition else '',self.speech.phase if audition else 'idle')
        self.tts_audition.setText('加载中…' if audition and self.speech.phase=='generating' else '停止试听' if audition else '试听音色')
        self.tts_audition.setEnabled(bool(speech.catalog_for(self._speech_language(self._tts_side))) and not (audition and self.speech.phase=='generating'))
        generating=False
        for side,(surface,status,subtitle,progress,duration,replay,download) in self.speech_bars.items():
            current=self.session.result if side=='target' else self.session.source_text
            spoken=getattr(self,'_speech_seen',{}).get(side)
            active=speech.active and speech.side==side
            finished=bool(spoken) and speech.finished and speech.completed_side==side and speech.completed_text==current and spoken==current and not speech.active and not self._speech_dismissed.get(side)
            surface.setVisible(bool((active or finished) and self.isVisible()))
            has_text=bool(current)
            replay.setEnabled(has_text);download.setEnabled(has_text)
            if not active and finished:
                status.setText('朗读完成 · '+self._clock(speech.completed_duration))
                subtitle.finish_caption()
                progress.setRange(0,1000);progress.setValue(1000)
                duration.setText(self._clock(speech.completed_duration))
            if active:
                busy=speech.phase=='generating' and not speech.paused
                generating=generating or (busy and side==speech.side)
                if speech.paused:
                    status.setText('已暂停')
                elif busy:
                    dots='.'*(self._speech_dots%4)
                    status.setText('正在生成语音 · %d/%d 段%s'%(speech.index+1,max(1,len(speech.parts)),dots))
                elif speech.phase=='ending':
                    status.setText('朗读完成')
                else:
                    status.setText('正在朗读 · %d/%d 段'%(speech.index+1,max(1,len(speech.parts))))
                subtitle.set_caption(*speech.caption(),complete=speech.phase=='ending')
                if busy:progress.setRange(0,0)
                else:progress.setRange(0,1000);progress.setValue(round((speech.progress or 0)*1000))
                duration.setText(self._clock(speech.total_duration)+' · '+str(len(speech.parts))+' 段')
            play,tune=self.speech_buttons[side]
            player_button=self.speech_playback[side]
            if active:
                glyph='play' if speech.paused else 'pause'
                title='继续朗读' if speech.paused else '暂停朗读'
            else:
                glyph='play';title='播放朗读'
            for control in (play,player_button):
                control.set_glyph(glyph);control.setToolTip(title);control.setAccessibleName(title)
            play.setEnabled(has_text)
            player_button.setEnabled(has_text and (active or finished))
        if generating and self.isVisible():
            if not self._speech_pulse.isActive():self._speech_pulse.start()
        else:self._speech_pulse.stop()

    def _auto_speak(self):
        if self.store.data['tts_auto'] and self.isVisible() and not self._closing:self._speak(True,force=True)
    def _source_speech_ready(self):
        epoch=self._await_source_speech;self._await_source_speech=None
        if epoch is None or epoch!=self.session.epoch or not self.isVisible():return
        if self.session.detected_code in TARGETS:self._speak(False)
        else:self._message('未能识别原文语言，请先指定原文语言后再朗读。')

    def sizeHint(self):return QSize(WINDOW_SIZE) if self._layout_ready else super().sizeHint()
    def adjustSize(self):
        if not self._layout_ready:return super().adjustSize()
        if not self.isVisible() and not self.isMaximized():self.chrome.fit_screen(self.chrome.default_size)
    def resizeEvent(self,event):
        if self._layout_ready:self._update_compact_footer()
        super().resizeEvent(event)
        if hasattr(self,'history') and self.history.isVisible():self.history.reposition()

    def _update_compact_footer(self):
        compact=self.width()<COMPACT_FOOTER_WIDTH
        if self._compact_footer==compact:return
        self._compact_footer=compact
        for play,_ in self.speech_buttons.values():play.setText('' if compact else '转语音')
        self._update_delay_action()
        self._copy_appearance(False,self._source_copy_feedback.isActive())
        self._copy_appearance(True,self._copy_feedback.isActive())

    def _update_delay_action(self):
        live=self.store.data['live'];compact=self._compact_footer
        title='自动翻译设置' if live else '立即翻译'
        self.translate_button.setText('' if compact else '自动翻译' if live else '手动翻译')
        self.translate_button.set_glyph('status-clock' if live else 'translate')
        self.translate_button.setToolTip(title);self.translate_button.setAccessibleName(title)
        show_delay=live and not compact
        self.delay_button.setFixedWidth(46 if show_delay else ACTION_SIZE-2)
        self.delay_button.set_glyph('' if show_delay else 'settings')
        self.delay_button.setText(f'{self.session.delay_seconds:g}s' if show_delay else '')
        title=f'自动翻译延迟 · {self.session.delay_seconds:g} 秒' if live else '翻译设置'
        self.delay_button.setAccessibleName(title);self.delay_button.setToolTip(title)
    def keyPressEvent(self,event):
        # No translator shortcuts or implicit default-button submission.
        if event.key()==Qt.Key_Escape:event.ignore();return
        super().keyPressEvent(event)
    def showEvent(self,event):
        super().showEvent(event)
        if hasattr(self,'session') and not self._pinning:self.session.suspend(False)
        self.refresh()
    def hideEvent(self,event):
        if hasattr(self,'_polish_arrival'):self._polish_arrival.stop()
        if hasattr(self,'session') and not self._pinning:self.session.suspend()
        if hasattr(self,'speech'):self.speech.stop()
        if hasattr(self,'alignment'):
            self.alignment.cancel();self._alignment_status='';self._alignment_highlights=None
            self.source_editor.setExtraSelections([]);self.output_editor.setExtraSelections([])
        for popup in getattr(self,'_popups',()):popup.hide()
        for dialog in getattr(self,'_managers',{}).values():dialog.hide()
        if hasattr(self,'history'):self.history.set_open(False,animate=False)
        if hasattr(self,'_copy_feedback'):
            for target,timer in ((True,self._copy_feedback),(False,self._source_copy_feedback)):
                timer.stop();self._copy_appearance(target,False)
        super().hideEvent(event)
    def closeEvent(self,event):
        if hasattr(self,'session'):self.session.suspend()
        if hasattr(self,'speech'):self.speech.stop()
        if hasattr(self,'alignment'):self.alignment.cancel()
        super().closeEvent(event)

    def _message(self,value,*,role='red'):
        self._notice=value;self._notice_role=role;self._notice_timer.start();self._refresh_notice()

    def _expire_notice(self):
        self._notice='';self._refresh_notice()

    def _refresh_notice(self):
        error=self.session.error or self.session.storage_error
        message=error or self._notice;role='red' if error else self._notice_role
        if message and role!=self._shown_notice_role:
            self._shown_notice_role=role
            css(self.error_label,*[f"color:{colors(theme)[role]};background:transparent;border:0;font:400 11px {font_css_stack()};" for theme in ('dark','light')])
        if self.error_label.text()!=message:self.error_label.setText(message)
        self.error_label.setVisible(bool(message))
    def _refresh_comparison(self,frame,toggle,editor,baseline,count,*,in_scope):
        valid=bool(baseline and count)
        previous=getattr(toggle,'_comparison_count',0)
        toggle._comparison_count=count
        with QSignalBlocker(toggle):
            if not valid:toggle.setChecked(False)
            elif count>previous:toggle.setChecked(True)
        toggle.setVisible(in_scope and valid)
        text=baseline if valid else ''
        if text!=getattr(editor,'_comparison_text',None):
            editor._comparison_text=text
            with QSignalBlocker(editor):editor.setPlainText(text)
        frame.setVisible(in_scope and valid and toggle.isChecked())

    def show_delay(self):
        if self.delay_popup.isVisible():self.delay_popup.hide()
        else:self.delay_popup.popup_below(self.delay_button)
    def _translate_action(self):
        if self.store.data['live']:self.show_delay()
        else:self.session.translate()
    def show_language(self,side):
        popup=self.language_popups[side]
        if popup.isVisible():popup.hide();return
        for other in self._popups:other.hide()
        popup.configure(getattr(self.session,side),allow_auto=side=='source');popup.popup_below(getattr(self,side+'_language'));popup.search.setFocus()
    def _store_changed(self):
        if self.store.ready:self._apply_pin(self.store.data['pinned'])
        self._refresh_presets();self.history.refresh();self.refresh()
    def _refresh_presets(self):
        key=tuple((row['id'],row['label'],row['instruction']) for row in self.store.data['presets'])
        if key==self._preset_key:return
        self._preset_key=key
        while self.preset_layout.count():
            item=self.preset_layout.takeAt(0)
            if item.widget():item.widget().hide();item.widget().deleteLater()
        for preset in self.store.data['presets']:
            control=PresetButton(preset['label'],self.preset_body);control.setProperty('presetId',preset['id'])
            control.clicked.connect(lambda checked=False,preset=dict(preset):self._preset(preset));self.preset_layout.addWidget(control)
        self.preset_layout.addStretch(1)
    def _preset(self,preset):
        self._active_preset=preset['id'];self.session.polish(preset['instruction']);self.refresh()
    def _polish(self):
        if self.session.phase=='polishing':self.session.cancel();return
        self._active_preset='';self.session.polish(self.instruction.text())
    def _set_polish_scope(self,scope):
        self._active_preset='';self.session.set_polish_scope(scope)
    def _replace_source(self,text):
        blocker=QSignalBlocker(self.source_editor);cursor=self.source_editor.textCursor();cursor.beginEditBlock();cursor.select(QTextCursor.Document);cursor.insertText(text);cursor.endEditBlock();self.source_editor.setTextCursor(cursor);del blocker

    def _clear_or_undo_source(self):
        if self.session.can_undo_clear:self.session.undo_clear_source()
        else:self.session.clear_source()

    def save_prompt(self):
        self.show_manager('presets');self._managers['presets'].instruction.setPlainText(self.instruction.text())
    def show_manager(self,kind):
        from translator_manager import TranslatorManager
        dialog=self._managers.get(kind)
        if dialog is None:dialog=self._managers[kind]=TranslatorManager(self,kind)
        dialog.refresh();dialog.show();dialog.raise_();dialog.activateWindow()
    def toggle_history(self):self.history.set_open(not self.history.opened)
    def _pin(self,checked):
        self._apply_pin(checked)
        self.store.update_preferences(pinned=bool(checked))
    def _apply_pin(self,checked):
        from platform_native import set_window_topmost
        checked=bool(checked)
        if set_window_topmost(self,checked):return
        if bool(self.windowFlags() & Qt.WindowStaysOnTopHint)==checked:return
        geometry=self.geometry();visible=self.isVisible();self._pinning=True
        try:
            self.setWindowFlag(Qt.WindowStaysOnTopHint,checked);self.setGeometry(geometry)
            if visible:self.show();self.raise_()
        finally:self._pinning=False
    def _dismiss_speech(self,side):
        self._speech_seen.pop(side,None);self._speech_dismissed[side]=True
        if self.speech.side==side:self.speech.stop()
        self._speech_changed()

    def _speak(self,target,*,force=False):
        side='target' if target else 'source'
        code=self._speech_language(side)
        if code not in SPEECH_LANGUAGES:
            if code is None and self.session.source=='auto':
                self._await_source_speech=self.session.epoch;self.session.detect_only()
            else:self._message('当前语言暂不支持语音。')
            return
        text=self.session.result if target else self.session.source_text
        self._speech_dismissed['target' if target else 'source']=False
        self._speech_seen['target' if target else 'source']=text
        self.speech.speak(text,code,side,force=force,voice=self.store.voice_for(side,code).get('id',''))
    def _copy(self,target):
        text=self.session.result if target else self.session.source_text
        if not text:return
        from platform_native import native_input_available
        if sys.platform=='win32' and native_input_available():
            from native_clipboard_reader import foreign_clipboard_lock
            if foreign_clipboard_lock():self._message('剪贴板被其他应用占用，请稍后再试。');return
        invitation=getattr(self.manager,'translation',None)
        if invitation is not None:invitation.silence_output_copy(text)
        QApplication.clipboard().setText(text)
        self._copy_appearance(target,True)
        (self._copy_feedback if target else self._source_copy_feedback).start()

    def _copy_appearance(self,target,copied):
        control=self.copy_button if target else self.source_copy_button
        title='已复制' if copied else '复制译文' if target else '复制原文'
        control.setText('' if self._compact_footer else title)
        control.set_glyph('check' if copied else 'copy')
        control.setAccessibleName(title);control.setToolTip(title)
    def _paste(self):
        from platform_native import native_input_available
        if sys.platform=='win32' and native_input_available():
            from native_clipboard_reader import foreign_clipboard_lock
            if foreign_clipboard_lock():self._message('剪贴板被其他应用占用，请稍后再试。');return
        self.source_editor.paste()

    def _refresh_language_caption(self):
        state=self.session
        if state.source!='auto' or not state.source_text.strip():
            self._caption_text='';self._caption_language='';self._caption_idle.stop()
        elif self._caption_text!=state.source_text:
            self._caption_text=state.source_text;self._caption_idle.start()
        elif not self._caption_idle.isActive():
            self._caption_language=state.detected if state.detected_code else ''
        self.source_language.set_language(state.source,self._caption_language)
        self._fit_favorites('source')

    def refresh(self):
        if not self._layout_ready or self._closing:return
        theme=self.manager.settings.theme
        from theme_accent import current
        key=(theme,current())
        if self._theme_key!=key:
            self._theme_key=key;self._theme=theme;self.set_surface_theme(theme);self.close_button.dark=theme=='dark';c=colors(theme)
            self._applied_settings_theme=theme
            for popup in self._popups:popup._applied_settings_theme=theme
            from native_ui_common import apply_widget_theme_style
            apply_widget_theme_style(self,
                f'QPlainTextEdit{{background:transparent;border:0;color:{c["text"]};font:400 15px {font_css_stack()};selection-background-color:{c["action"]};selection-color:{c["on_action"]};}}'
                f'QPlainTextEdit#translatorResult{{color:{TRANSLATED_INK[theme]};}}'
                f'QLineEdit{{background:{c["input"]};color:{c["text"]};border:1px solid {c["input_border"]};border-radius:8px;padding:8px 11px;font:400 12px {font_css_stack()};}} QLineEdit:focus{{border-color:{c["ring"]};}}'
                f'QScrollArea{{background:transparent;border:0;}} QListWidget{{background:transparent;color:{c["text"]};border:0;outline:0;}} QListWidget::item{{background:transparent;border:0;}}'
                f'QComboBox{{background:transparent;color:{c["text"]};border:0;padding:5px 28px 5px 9px;font:11px {font_css_stack()};min-height:22px;}} QComboBox::drop-down{{border:0;width:28px;background:transparent;}} QComboBox::down-arrow{{image:none;}}'
                'QSlider::groove:horizontal{height:4px;background:transparent;border:0;} QSlider::sub-page:horizontal{background:transparent;} QSlider::handle:horizontal{width:16px;margin:-6px 0;background:transparent;border:0;}'
            )
            refresh_tree(self,theme)
            if alignment:=getattr(self,'_alignment_highlights',None):
                self._selection_highlight(*alignment,scroll=False)
            self.source_task.set_theme(theme);self.output_task.set_theme(theme)
            for control in (self.live_toggle,self.delay_slider,self.tts_rate,self.tts_pitch,self.tts_auto):control.dark=theme=='dark';control.update()
            for dialog in self._managers.values():dialog.refresh()
            if hasattr(self,'history'):self.history.refresh()
        state=self.session
        reveal_result=self._was_translating and state.phase=='ready' and not state.error
        finished_polish=self._was_polishing and state.phase in ('idle','ready') and not state.error
        self._was_polishing=state.phase=='polishing'
        self._was_translating=state.phase=='translating'
        if state.busy:
            self.source_editor.finish_reveal()
            self.output_editor.finish_reveal()
            self._polish_arrival.stop()
        source_scope=state.polish_scope=='source'
        self.source_card.set_polish_active(source_scope)
        self.output_card.set_polish_active(not source_scope)
        revisions=state.revisions
        source_revisions=state.source_revisions
        if self.speech.active and self.speech.side in ('source','target') and self.speech.text!=(state.result if self.speech.side=='target' else state.source_text):self.speech.stop()
        pair=(state.source_text,state.result)
        self.alignment.document=state.document
        pair_changed=self._alignment_pair!=pair
        if pair_changed:
            self._alignment_highlights=None
            self._alignment_pair=pair;self.alignment.cancel();self.source_editor.setExtraSelections([]);self.output_editor.setExtraSelections([])
            self._alignment_status=''
        target_scope=not source_scope and not state.source_unconfirmed
        for control in (self.undo_button,self.regenerate_button,self.replace_button,self.version_label):
            control.setVisible(target_scope and bool(revisions))
        for control in (self.source_undo_button,self.source_regenerate_button):
            control.setVisible(bool(source_revisions))
        self.source_replace_button.setVisible(state.source_unconfirmed)
        self.source_replace_button.setEnabled(not state.busy)
        self.source_version_label.setVisible(source_scope or bool(source_revisions))
        self.source_version_label.setText('原文待确认' if state.source_unconfirmed else '原始原文')
        source_baseline=state.source_original if state.source_original!=state.source_text else ''
        target_baseline=state.translation_baseline
        if target_baseline==state.result:target_baseline=''
        self._refresh_comparison(self.source_comparison,self.source_compare_button,self.source_baseline_view,
            source_baseline,len(source_revisions),in_scope=source_scope or state.source_unconfirmed)
        self._refresh_comparison(self.comparison,self.compare_button,self.original_view,
            target_baseline,len(revisions),in_scope=target_scope)
        polish_arrivals=[]
        for editor,value,versions,side,controls in (
            (self.source_editor,state.source_text,source_revisions,'source',
             (self.source_version_label,self.source_compare_button,self.source_undo_button,
              self.source_regenerate_button,self.source_replace_button,self.source_comparison)),
            (self.output_editor,state.result,revisions,'target',
             (self.version_label,self.compare_button,self.undo_button,self.regenerate_button,
              self.replace_button,self.comparison)),
        ):
            updated=editor.toPlainText()!=value
            if updated:
                editor.finish_reveal()
                blocker=QSignalBlocker(editor);editor.setPlainText(value);del blocker
            version=versions[-1] if versions else None
            polished=(finished_polish and version is not None and version is not self._last_polish_revision.get(side)
                      and version['after']==value)
            self._last_polish_revision[side]=version
            if polished:
                editor.start_reveal(value)
                polish_arrivals.extend(controls)
            elif updated and side=='target' and reveal_result:editor.start_reveal(value)
        if polish_arrivals:self._polish_arrival.start(polish_arrivals)
        if pair_changed and self._alignment_side is not None:self._mirror_selection(self._alignment_side)
        self.source_editor.setEnabled(self.store.ready)
        self._refresh_language_caption()
        self.target_language.set_language(state.target)
        self._fit_favorites('target')
        self.counter.setText(f'{len(state.source_text):,} 字符')
        self.counter.setMinimumWidth(self.counter.fontMetrics().horizontalAdvance(self.counter.text()))
        delay=f'{state.delay_seconds:g} 秒'
        live=self.store.data['live']
        delay_key=(live,state.delay_seconds)
        if delay_key!=getattr(self,'_delay_key',None):
            self._delay_key=delay_key;self._update_delay_action()
        can_translate=bool(state.source_text.strip()) and not state.busy and not state.source_unconfirmed
        self.translate_button.setEnabled(live or can_translate)
        self.delay_value.setText('停止输入后 '+delay+'开始翻译')
        for control,value in ((self.delay_slider,round(state.delay_seconds*10)),(self.live_toggle,self.store.data['live']),(self.pin_button,self.store.data['pinned'])):
            blocker=QSignalBlocker(control)
            if control is self.delay_slider:control.setValue(value)
            else:control.setChecked(value)
            del blocker
        if revisions:self.version_label.setText('AI 润色 · 第 '+str(len(revisions))+' 版')
        else:self.version_label.setText('原始译文')
        busy_side=state.busy_side
        self.source_task.set_task('正在润色原文…' if busy_side=='source' else '')
        if state.phase=='translating':
            self.output_task.set_task('正在翻译…','正在生成译文…')
        else:
            self.output_task.set_task('正在润色译文…' if busy_side=='target' else '')
        self._refresh_output_status()
        self._refresh_notice()
        self.copy_button.setEnabled(bool(state.result));self.favorite_button.setEnabled(bool(state.result));self.favorite_button.set_glyph('star-filled' if state.favorite else 'star');self.favorite_button.set_glyph_tint('#E5AA17' if state.favorite else None)
        self.source_copy_button.setEnabled(bool(state.source_text))
        glyph='undo' if state.can_undo_clear else 'close'
        if self.clear_source_button.glyph!=glyph:self.clear_source_button.set_glyph(glyph)
        title='撤回清空' if state.can_undo_clear else '清空原文'
        self.clear_source_button.setToolTip(title);self.clear_source_button.setAccessibleName(title)
        self.clear_source_button.setEnabled(bool(state.source_text) or state.can_undo_clear)
        polish_ready=bool(state.source_text if state.polish_scope=='source' else state.result)
        self.polish_button.setText('停止' if state.phase=='polishing' else '润色');self.polish_button.setEnabled(polish_ready)
        for scope,control in self.polish_scope_buttons.items():control.setChecked(scope==state.polish_scope)
        self.scope_hint.setText('基于原文' if state.polish_scope=='source' else '基于当前译文');self.instruction.setPlaceholderText('告诉 AI 如何修改这段原文…' if state.polish_scope=='source' else '告诉 AI 如何修改这段译文…')
        self.translate_now_button.setEnabled(can_translate)
        self.save_prompt_button.setVisible(bool(self.instruction.text().strip()))
        self.clear_instruction.setVisible(bool(self.instruction.text()))
        self.glossary_button.setText('术语保护 '+str(len(self.store.data['glossary'])))
        self.history_button.setText('历史 '+str(len(self.store.data['history'])))
        self._refresh_presets();self._rebuild_favorites()
        for control in self.preset_body.findChildren(PresetButton):control.setChecked(control.property('presetId')==self._active_preset);control.setEnabled(polish_ready)
        preferences=self.store.data;self.speech.configure(rate=preferences['tts_rate'],pitch=preferences['tts_pitch'])
        self.tts_rate_value.setText(f"语速 {preferences['tts_rate']:g}×")
        pitch=preferences['tts_pitch'];self.tts_pitch_value.setText('音调偏好 · '+('标准' if abs(pitch-1)<.025 else '偏低沉' if pitch<1 else '偏明亮'))
        for control,value in ((self.tts_rate,round(preferences['tts_rate']*100)),(self.tts_pitch,round(pitch*100)),(self.tts_auto,preferences['tts_auto'])):
            blocker=QSignalBlocker(control)
            if control is self.tts_auto:control.setChecked(value)
            else:control.setValue(value)
            del blocker
        if self.tts_popup.isVisible() or self.voice_popup.isVisible():
            self._browse_voices()
        self._voices_changed()
        for side,(play,tune) in self.speech_buttons.items():
            play.setEnabled(bool(state.result if side=='target' else state.source_text))
        self.tts_audition.setEnabled(bool(self.speech.catalog));self._speech_changed()

    def _refresh_output_status(self):
        state=self.session
        status=('已完成 · '+f'{state.elapsed_ms/1000:.1f} 秒') if state.result else '就绪'
        if state._dirty and state.result and not state.busy:status='原文已修改 · 待翻译'
        message=self._alignment_status or ('' if state.busy_side else status)
        count=f'{len(state.result):,} 字符'
        self.status.setText(count+(' · '+message if message else ''))
        self.status.setAccessibleDescription(message)
        self.status.setMinimumWidth(self.status.fontMetrics().horizontalAdvance(count))

    def shutdown(self):
        if self._closing:return
        self._closing=True
        self.speech.close();self.history.set_open(False,animate=False)
        for popup in self._popups:popup.hide()
        for dialog in self._managers.values():dialog.hide()
        self.session.close()
        if self._transport is not None:self._transport.close()
        if hasattr(self,'_preference_writer'):self._preference_writer.close()
        self.hide()
