"""Independent translator state, cancellation and revision history.

No shelf/clipboard-sync requests, widget references or synchronous disk access.
An epoch fences every request and detector result, including rapid edits/hides.
"""
from __future__ import annotations

from datetime import datetime, timezone
import time

from PySide6.QtCore import QObject, QThreadPool, QTimer, Signal, Qt

from sync_files import DiskWork
from translation_languages import MAX_TEXT_CHARS, TARGETS, UNKNOWN_SOURCE, content_language, detect_source, language_code, quick_content_language, script_family, script_changed
from translation_document import TranslationDocument

# A preset describes tone or length, never a different language. Keep this
# prefix inside the provider's 1000-character instruction limit.
POLISH_LANGUAGE_GUARD = '保持输入文本的语言不变，不要翻译或切换语言。'


class TranslatorSession(QObject):
    changed = Signal()
    result_ready = Signal()
    source_detected = Signal()
    source_replaced = Signal(str)

    def __init__(self, api, store, parent=None, *, detector=detect_source, rules=dict):
        super().__init__(parent)
        self.api=api;self.store=store;self.detector=detector;self.rules=rules;self._manual_target=False;self._rule_target=False;self.closed=False;self.suspended=False
        self.source='auto';self.target='en';self.source_text='';self.result='';self.original=''
        self.detected=UNKNOWN_SOURCE;self.detected_code=None;self.phase='idle';self.error='';self.storage_error=''
        self.revisions=[];self.source_revisions=[];self.source_original=''
        self._draft_origin=None;self._draft_edit_pending=False
        self._clear_undo=None
        self.favorite=False;self.history_id=None;self.epoch=0;self.elapsed_ms=0;self.polish_scope='target'
        self._pending=None;self._pending_input=None;self._started=0.;self._dirty=False;self._record_dirty=False
        self.document=TranslationDocument();self._detection_text=''
        self.pool=QThreadPool(self);self.pool.setMaxThreadCount(1);self.jobs=set()
        self.debounce=QTimer(self);self.debounce.setSingleShot(True);self.debounce.timeout.connect(self.translate)
        self.detection_timer=QTimer(self);self.detection_timer.setSingleShot(True);self.detection_timer.setInterval(120)
        self.detection_timer.timeout.connect(lambda:self._detect(self.epoch,self.source_text))
        self.history_timer=QTimer(self);self.history_timer.setSingleShot(True);self.history_timer.setInterval(600);self.history_timer.timeout.connect(self.remember)
        self.api.completed.connect(self._completed)
        if hasattr(api,'units_ready'):api.units_ready.connect(self._units_completed)
        self.store.loaded.connect(self._loaded);self.store.error.connect(self._storage_failed)
        if store.ready:self._loaded()

    @property
    def busy(self):return self.phase in ('translating','polishing')

    @property
    def source_unconfirmed(self):
        return self._draft_origin is not None

    @property
    def translation_baseline(self):
        """Only restore a translation baseline for the current source."""
        return '' if self.source_unconfirmed or self._dirty else self.original

    @property
    def can_undo_clear(self):
        return self._clear_undo is not None

    def clear_source(self):
        """Keep one in-memory snapshot of the pair and its editing state."""
        if self.closed or not self.source_text:return
        fields=('source_text','result','original','source_original','revisions','source_revisions',
                '_draft_origin','_dirty','_record_dirty','history_id','favorite','elapsed_ms',
                'detected','detected_code','_detection_text','document','_manual_target','_rule_target')
        self._clear_undo={name:getattr(self,name) for name in fields}
        # Clearing the live document leaves the previous alignment cache intact.
        self.document=TranslationDocument()
        self.edit_source('',rewrite=True)

    def undo_clear_source(self):
        if self.closed or self.source_text or not self.can_undo_clear:return
        self._invalidate()
        snapshot,self._clear_undo=self._clear_undo,None
        for name,value in snapshot.items():setattr(self,name,value)
        self.error='';self.phase='ready' if self.result else 'idle'
        self.source_replaced.emit(self.source_text)
        if self._record_dirty and self.result:self.history_timer.start()
        self.changed.emit()

    @property
    def busy_side(self):
        if self.phase=='translating':
            return 'target'
        if self.phase=='polishing' and self._pending is not None:
            return self._pending['scope']
        return None

    @property
    def delay_seconds(self):return self.store.data['debounce_ms']/1000

    def _loaded(self):
        if self.closed:return
        self.source=self.store.data['source'];self.target=self.store.data['target']
        pending,self._pending_input=self._pending_input,None
        if pending is not None:
            self.open_input(*pending)
        if self._dirty:self._schedule()
        self.changed.emit()

    def open_input(self, text, target):
        """Apply an invitation after initial settings have been restored."""
        if not self.store.ready:
            self._pending_input=(text,target)
            return
        if text is not None:
            self._draft_origin=None;self._draft_edit_pending=False
            self._clear_undo=None
        if target is not None:
            self.set_language('target',target)
        if text is not None:
            self.edit_source(text,rewrite=True)

    def _storage_failed(self,message):self.storage_error=message;self.changed.emit()

    def _invalidate(self):
        self.epoch+=1;self.debounce.stop();self.detection_timer.stop();self.history_timer.stop();self.api.cancel();self._pending=None
        for job in self.jobs:job.cancel.set()
        if self.busy:self.phase='ready' if self.result else 'idle'

    def edit_source(self,text,*,rewrite=False):
        if self.closed or text==self.source_text:return
        if text:self._clear_undo=None
        if self.source_unconfirmed and not rewrite:
            self._draft_origin=None
            self._draft_edit_pending=True
        self._invalidate();self.source_text=text
        if not self.source_unconfirmed:
            self.original='';self.revisions=[];self.source_revisions=[];self.source_original=''
        self.error='';self.favorite=False;self.history_id=None;self.phase='idle';self._dirty=True;self._record_dirty=False
        self.detected=UNKNOWN_SOURCE;self.detected_code=None
        if not text.strip():
            self._manual_target=False
            self._draft_origin=None;self._draft_edit_pending=False
            self.result=self.original=self.source_original=''
            self.revisions=[];self.source_revisions=[];self._dirty=False
            self.document.pair('','')
        elif len(text)>MAX_TEXT_CHARS:self.error='原文最多支持 16,000 个字符，请缩短后再翻译。'
        elif '\0' in text:self.error='原文包含不支持的空字符，请删除后再翻译。'
        else:
            self.detection_timer.start();self._schedule()
        if rewrite:self.source_replaced.emit(text)
        self.changed.emit()

    def _schedule(self):
        live=self.store.data['live'] or self._draft_edit_pending
        if not self.source_unconfirmed and self.store.ready and live and self.source_text.strip() and not self.suspended:
            self.debounce.start(self.store.data['debounce_ms'])

    def set_language(self,side,code):
        if side not in ('source','target') or code not in TARGETS and not (side=='source' and code=='auto'):
            raise ValueError('语言无效。')
        if self.source_unconfirmed:
            self.error='请先确认或恢复润色后的原文，再切换语言。';self.changed.emit();return
        if side=='target':self._manual_target=True;self._rule_target=False
        if getattr(self,side)==code:return
        self._clear_undo=None
        self._invalidate();setattr(self,side,code);self.result='';self.original='';self.revisions=[];self.error=''
        self.history_id=None;self.favorite=False;self._dirty=True
        recent=[code]+[value for value in self.store.data['recent'] if value!=code and value in TARGETS]
        self.store.update_preferences(**{side:code,'recent':[v for v in recent if v in TARGETS][:8]})
        self._schedule();self.changed.emit()

    def set_delay(self,milliseconds):
        milliseconds=max(100,min(5000,int(milliseconds)))
        self.store.update_preferences(debounce_ms=milliseconds)
        if self.debounce.isActive():self._schedule()
        self.changed.emit()

    def set_live(self,enabled):
        self.store.update_preferences(live=bool(enabled));self.debounce.stop()
        if enabled and self._dirty:self._schedule()
        self.changed.emit()

    def swap(self):
        if self.source_unconfirmed:
            self.error='请先确认或恢复润色后的原文，再交换语言。';self.changed.emit();return False
        source=self.detected_code if self.source=='auto' else self.source
        if source not in TARGETS:
            self.error='请先指定原文语言，再交换语言。';self.changed.emit();return False
        original=self.source_text;translated=self.result
        self._clear_undo=None
        self._invalidate();self._manual_target=True;self.source,self.target=self.target,source
        self.source_text=translated or original;self.result='';self.original='';self.revisions=[];self.source_revisions=[];self.source_original=''
        self.history_id=None;self.favorite=False;self.error='';self._dirty=True
        self.store.update_preferences(source=self.source,target=self.target);self._schedule();self.changed.emit();return True

    def translate(self):
        if self.closed or self.suspended or not self.store.ready or self.source_unconfirmed or not self.source_text.strip():return False
        if len(self.source_text)>MAX_TEXT_CHARS or '\0' in self.source_text:return False
        self._draft_edit_pending=False
        self._invalidate();self.phase='translating';self.error='';self._started=time.perf_counter()
        self._pending=dict(kind='translate',source=self.source_text,target=self.target)
        epoch=self.epoch
        if (hasattr(self.api,'translate_units') or self.rules()) and self.source=='auto' and self._detection_text!=self.source_text:
            self._pending['await_detection']=True;self._detect(epoch,self.source_text)
        else:
            if self.source=='auto':self._detect(epoch,self.source_text)
            self._begin_translation()
        self.changed.emit();return True

    def _begin_translation(self):
        if self._pending is None:return
        code=self.detected_code if self.source=='auto' else self.source
        if not self._manual_target:
            rule=self.rules().get(code)
            if rule or self._rule_target:
                self.target=rule or self.store.data['target']
                self._pending['target']=self.target
                self._rule_target=bool(rule)
        if code==self.target:
            self.document.pair(self.source_text,self.source_text);self._completed(self.epoch,self.source_text,'');return
        if not hasattr(self.api,'translate_units'):
            self.api.translate(self.epoch,self.source_text,self.target,source=self.source,glossary=self.store.data['glossary']);return
        options=(self.source,self.target,tuple(self.store.data['glossary']))
        self._pending.update(units=self.document.plan(self.source_text,options),options=options)
        self._request_units()

    def _request_units(self):
        pending=self._pending;units=pending['units'];missing=[i for i,u in enumerate(units) if u.translation is None]
        if not missing:
            if sum(len(u.translation or '')+len(u.gap)+1 for u in units)>24000:
                self._completed(self.epoch,'','译文超过 24,000 个字符，请缩短原文；已有译文已保留。');return
            result=self.document.commit(self.source_text,units,pending['options'])
            self._completed(self.epoch,result,'');return
        rows=[];characters=0
        for index in missing[:128]:
            unit=units[index]
            if rows and characters+len(unit.text)>12000:break
            rows.append(dict(id=str(index),text=unit.text,before=units[index-1].text[-180:] if index else '',
                after=units[index+1].text[:180] if index+1<len(units) else ''));characters+=len(unit.text)
        selected={int(row['id']) for row in rows}
        # Neighbors in the same batch already supply context. Do not pay for
        # two additional copies of the initial document in before/after fields.
        for row in rows:
            index=int(row['id'])
            if index-1 in selected:row['before']=''
            if index+1 in selected:row['after']=''
        pending['batch']={row['id'] for row in rows}
        self.api.translate_units(self.epoch,rows,self.target,source=self.source,glossary=self.store.data['glossary'])

    def _units_completed(self,epoch,value,error):
        if self.closed or epoch!=self.epoch or not self._pending or 'units' not in self._pending:return
        if error:self._completed(epoch,'',error);return
        for row in value['units']:self._pending['units'][int(row['id'])].translation=row['text']
        self._request_units()

    def _detect(self,epoch,text):
        detector=self.detector;job=DiskWork(lambda cancel:None if cancel.is_set() else detector(text));self.jobs.add(job)
        def ready(value,error):
            job.signals.done.disconnect(ready)
            self.jobs.discard(job)
            if self.closed or epoch!=self.epoch:return
            if error is not None:
                import logging
                logging.getLogger(__name__).warning('Translator language detector failed: %s',type(error).__name__)
                self.detected=UNKNOWN_SOURCE;self.detected_code=None
            else:
                self.detected=value
                self.detected_code=language_code(value)
            self._detection_text=text
            self.changed.emit();self.source_detected.emit()
            if self._pending and self._pending.pop('await_detection',False):self._begin_translation()
        job.signals.done.connect(ready,Qt.QueuedConnection);self.pool.start(job)

    def detect_only(self):
        if not self.closed and self.source_text.strip():self._detect(self.epoch,self.source_text)

    def set_polish_scope(self,scope):
        if scope not in ('source','target'):raise ValueError('润色范围无效。')
        if scope==self.polish_scope:return
        if self.phase=='polishing':self._invalidate()
        self.polish_scope=scope;self.changed.emit()

    def polish(self,instruction,*,regenerate=False,scope=None):
        scope=scope or self.polish_scope
        instruction=str(instruction).strip()
        text=self.source_text if scope=='source' else self.result
        if self.closed or self.suspended or not text.strip():return False
        if not instruction or len(instruction)>1000:
            self.error='请输入 1–1000 个字符的润色要求。';self.changed.emit();return False
        history=self.source_revisions if scope=='source' else self.revisions
        base=history[-1]['before'] if regenerate and history else text
        self._invalidate();self.phase='polishing';self.error='';self._started=time.perf_counter()
        self._pending=dict(kind='polish',scope=scope,before=base,instruction=instruction,regenerate=regenerate)
        preferred=(self.detected_code if self.source=='auto' else self.source) if scope=='source' else self.target
        actual=quick_content_language(base,preferred)
        if actual or preferred in ('en',None) and script_family(base) in ('latin',None):
            self._send_polish(actual,preferred)
        else:
            epoch=self.epoch;detector=self.detector
            job=DiskWork(lambda cancel:None if cancel.is_set() else content_language(base,preferred,detector=detector))
            self.jobs.add(job)
            def ready(code,error):
                job.signals.done.disconnect(ready);self.jobs.discard(job)
                if self.closed or epoch!=self.epoch:return
                if error:
                    import logging
                    logging.getLogger(__name__).warning('Polish language detection failed: %s',type(error).__name__)
                self._send_polish(code,preferred)
            job.signals.done.connect(ready,Qt.QueuedConnection);self.pool.start(job)
        self.changed.emit();return True

    def _send_polish(self,actual,preferred):
        pending=self._pending
        if pending is None or pending['kind']!='polish':return
        scope=pending['scope']
        options=dict(original=self.source_text if scope=='target' else '',glossary=self.store.data['glossary'])
        if scope=='source':options['scope']='source'
        language='输入语言：'+TARGETS[actual][0]+'。' if actual in TARGETS else ''
        guarded=(POLISH_LANGUAGE_GUARD+language+pending['instruction'])[:1000]
        self.api.polish(self.epoch,pending['before'],actual or preferred or self.target,guarded,**options)

    def _completed(self,epoch,result,error):
        if self.closed or epoch!=self.epoch or self._pending is None:return
        pending,self._pending=self._pending,None
        self.elapsed_ms=round((time.perf_counter()-self._started)*1000)
        if error:
            self.phase='error';self.error=error;self.changed.emit();return
        if pending.get('kind')=='polish' and script_changed(pending.get('before',''),result):
            self.phase='error';self.error='润色结果的语言与输入不一致，已保留原内容。';self.changed.emit();return
        if pending.get('scope')=='source':
            if len(result)>MAX_TEXT_CHARS:
                self.phase='error';self.error='润色后的原文超过 16,000 个字符，原文已保留。';self.changed.emit();return
            entry=dict(before=pending['before'],after=result,instruction=pending['instruction'])
            if pending.get('regenerate') and self.source_revisions:self.source_revisions[-1]=entry
            else:self.source_revisions.append(entry)
            self.source_revisions=self.source_revisions[-30:]
            if not self.source_original:self.source_original=pending['before']
            # Keep the approved source and existing translation until the user confirms.
            if self._draft_origin is None and result!=self.source_text:
                self._draft_origin=(self.source_text,self._dirty)
            self.source_text=result
            self.detected=UNKNOWN_SOURCE;self.detected_code=None
            if self.source=='auto':self.detection_timer.start()
            self.phase='ready' if self.result else 'idle';self.error=''
            self.source_replaced.emit(result)
            self.changed.emit();return
        self.result=result;self.error='';self.phase='ready';self._dirty=False;self._record_dirty=True
        if pending['kind']=='translate':
            self.original=result;self.revisions=[]
            if not self.document.matches(self.source_text,result):self.document.pair(self.source_text,result)
        else:
            entry=dict(before=pending['before'],after=result,instruction=pending['instruction'])
            if pending['regenerate'] and self.revisions:self.revisions[-1]=entry
            else:self.revisions.append(entry)
            self.revisions=self.revisions[-30:]
            self.document.pair(self.source_text,result)
        self.history_timer.start();self.changed.emit();self.result_ready.emit()

    def edit_result(self,text):
        if self.closed or text==self.result:return
        if text and not self.source_text:self._clear_undo=None
        self._invalidate();self.result=text;self.phase='ready' if text else 'idle';self.error='';self._record_dirty=True
        self.document.pair(self.source_text,text)
        if len(text)>24000:self.error='译文最多支持 24,000 个字符。'
        else:self.history_timer.start()
        self.changed.emit()

    def undo(self,scope=None):
        if (scope or self.polish_scope)=='source':
            if not self.source_revisions:return
            self._invalidate();self.source_text=self.source_revisions.pop()['before']
            self._source_revision_restored();return
        if not self.revisions:return
        self._invalidate();self.result=self.revisions.pop()['before'];self.error='';self.phase='ready';self._record_dirty=True
        self.document.pair(self.source_text,self.result)
        self.history_timer.start();self.changed.emit()

    def regenerate(self,scope=None):
        scope=scope or self.polish_scope
        history=self.source_revisions if scope=='source' else self.revisions
        return self.polish(history[-1]['instruction'],regenerate=True,scope=scope) if history else False

    def restore_original(self,scope=None):
        if (scope or self.polish_scope)=='source':
            if not self.source_original:return
            self._invalidate();self.source_text=self.source_original;self.source_revisions=[]
            self._source_revision_restored();return
        baseline=self.translation_baseline
        if not baseline:return
        self._invalidate();self.result=baseline;self.revisions=[];self.phase='ready';self.error='';self._record_dirty=True
        self.document.pair(self.source_text,self.result)
        self.history_timer.start();self.changed.emit()

    def _source_revision_restored(self):
        if self._draft_origin is not None and self.source_text==self._draft_origin[0]:
            _,self._dirty=self._draft_origin
            self._draft_origin=None
        self.error='';self.phase='ready' if self.result else 'idle'
        self.detected=UNKNOWN_SOURCE;self.detected_code=None
        if self.source=='auto':self.detection_timer.start()
        self.source_replaced.emit(self.source_text);self.changed.emit()

    def replace_original(self,scope=None):
        if (scope or self.polish_scope)=='source':
            if not self.source_text:return
            if not self.source_text.strip() or len(self.source_text)>MAX_TEXT_CHARS or '\0' in self.source_text:
                self.error='请确认原文非空且不超过 16,000 个字符。';self.changed.emit();return
            self._invalidate()
            pending=self.source_unconfirmed
            self._draft_origin=None;self.source_original=self.source_text;self.source_revisions=[]
            if pending:
                self.original='';self.revisions=[]
                self._dirty=True
                if self.translate():return
            self.changed.emit();return
        if not self.result:return
        self._invalidate();self.original=self.result;self.revisions=[];self.phase='ready';self.error='';self._record_dirty=True
        self.history_timer.start();self.changed.emit()

    def remember(self):
        if self.source_unconfirmed:return
        if not self.document.matches(self.source_text,self.result):return
        if not self._record_dirty or not self.source_text.strip() or not self.result.strip() or len(self.source_text)>16000 or len(self.result)>24000:return
        self.history_id=self.store.remember(dict(source_text=self.source_text,text=self.result,original=self.original,
            source=self.source,target=self.target,favorite=self.favorite,created_at=datetime.now(timezone.utc).isoformat()))
        self._record_dirty=False

    def toggle_favorite(self):
        if not self.result:return
        self.favorite=not self.favorite;self._record_dirty=True;self.remember();self.changed.emit()

    def restore_record(self,record):
        self._invalidate();self._draft_origin=None;self._draft_edit_pending=False
        self._clear_undo=None
        self.source_text=record['source_text'];self.result=record['text'];self.original=record['original'] or record['text']
        self._manual_target=True;self._rule_target=False
        self.source=record['source'];self.target=record['target'];self.favorite=record['favorite'];self.history_id=record['id']
        self.revisions=[];self.source_revisions=[];self.source_original=record['source_text'];self.phase='ready';self.error='';self._dirty=False;self._record_dirty=False;self.changed.emit()
        self.document.pair(self.source_text,self.result)

    def forget_record(self,identifier=None):
        if identifier is None or identifier==self.history_id:
            self.history_id=None;self._record_dirty=False
        if self._clear_undo is not None and (identifier is None or identifier==self._clear_undo['history_id']):
            self._clear_undo['history_id']=None;self._clear_undo['_record_dirty']=False

    def suspend(self,value=True):
        self.suspended=bool(value)
        if self.suspended:self._invalidate()
        elif self._dirty:self._schedule()
        self.changed.emit()

    def cancel(self):
        self._draft_edit_pending=False
        self._invalidate();self.phase='ready' if self.result else 'idle';self.changed.emit()

    def close(self):
        if self.closed:return
        self.remember();self.closed=True;self._invalidate()
        for job in self.jobs:job.cancel.set()
        self.pool.clear();self.pool.waitForDone(1500)
        self.store.close()
