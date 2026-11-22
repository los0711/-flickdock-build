"""Precomputed word alignment with an optional legacy phrase fallback."""
from collections import OrderedDict
import re
from PySide6.QtCore import QObject,QTimer,Qt,Signal
from PySide6.QtWidgets import QApplication
from translation_document import unique_quote
import aligned_words


class SelectionAlignment(QObject):
    highlight=Signal(str,object)
    presentation=Signal(str,object,object,str)
    def __init__(self,document,api=None,parent=None,*,document_api=None,fallback=True):
        super().__init__(parent);self.document=document;self.api=api;self.epoch=0;self.pending=None;self.cache=OrderedDict()
        self.queue=[];self.ranges=[];self.other='target'
        self.context={};self.status='';self._pointer_ended=False
        self.timer=QTimer(self);self.timer.setSingleShot(True);self.timer.timeout.connect(self._request)
        if api is not None:api.aligned.connect(self._done)
        self.document_api=document_api
        self.fallback=fallback
        self._pair=None;self._pair_epoch=0;self._pair_failed=False
        self._pair_bounds=[];self._pair_version=None;self._batch_version=None
        self._forward=[];self._reverse=[];self._word_map=None
        self._pair_cache=OrderedDict();self._selection=None
        if document_api is not None:document_api.document_aligned.connect(self._word_map_arrived)

    def prepare(self,source,target,source_lang='auto',target_lang='auto'):
        if self.document_api is None:return
        pair=(source,target,source_lang,target_lang) if (source.strip() and target.strip()
            and source!=target and self.document.matches(source,target)) else None
        if pair==self._pair:return
        self._pair_epoch+=1
        self.document_api.cancel()
        self.cancel()
        self._pair=pair;self._pair_failed=False;self._word_map=None
        self._pair_bounds=[];self._forward=[];self._reverse=[];self._batch_version=None
        if pair is None:return
        key=pair,self._pair_version
        if key in self._pair_cache:
            self._pair_cache.move_to_end(key)
            self._word_map=self._pair_cache[key]
            return
        self._pair_bounds=aligned_words.batches(source,target,self.document.spans)
        if self._pair_bounds:self._request_word_map()
        else:self._pair_failed=True

    def _request_word_map(self):
        source,target,source_lang,target_lang=self._pair
        s0,s1,t0,t1=self._pair_bounds[0]
        self.document_api.align_document(self._pair_epoch,source[s0:s1],target[t0:t1],source_lang,target_lang)

    def _word_map_arrived(self,epoch,value,error):
        if epoch!=self._pair_epoch or not self._pair_bounds:return
        if not error:
            try:
                version,forward,reverse=aligned_words.parse(value,self._pair_bounds[0])
                if self._batch_version not in (None,version):raise ValueError('Alignment model changed mid-document')
            except ValueError:
                error='对齐结果无效'
        if error:
            self._pair_failed=True
            self._pair_bounds.clear();self._forward.clear();self._reverse.clear()
        else:
            self._batch_version=version
            self._forward.extend(forward);self._reverse.extend(reverse)
            self._pair_bounds.pop(0)
            if self._pair_bounds:
                self._request_word_map()
                return
            if version!=self._pair_version:self._pair_cache.clear()
            self._pair_version=version
            self._word_map=(tuple(self._forward),tuple(self._reverse))
            self._pair_cache[self._pair,version]=self._word_map
            while len(self._pair_cache)>6:self._pair_cache.popitem(last=False)
        if self._selection is not None:self.select(*self._selection)
    def cancel(self):
        self.epoch+=1;self.pending=None;self.timer.stop()
        self._selection=None
        self.queue.clear();self.ranges.clear()
        self.context.clear();self.status='';self._pointer_ended=False
        if self.api is not None:self.api.cancel()

    def _publish(self):
        exact=sorted(set(self.ranges));sentences=sorted(set(self.context.values()))
        status=('正在匹配划词' if self.pending is not None or self.queue else
                '已定位到对应句子' if sentences else self.status)
        self.highlight.emit(self.other,exact)
        self.presentation.emit(self.other,exact,sentences,status)

    def select(self,side,begin,end,source,target):
        self.cancel();self.other='target' if side=='source' else 'source'
        if begin==end or not source.strip() or not target.strip():self._publish();return
        self._selection=side,begin,end,source,target
        pair_active=self._pair is not None and self._pair[:2]==(source,target)
        if pair_active and self._word_map is not None:
            self.ranges=aligned_words.corresponding(self._word_map[side=='target'],begin,end)
            self._publish()
            return
        allow_fallback=self.fallback and (self.document_api is None or pair_active and self._pair_failed)
        spans,sentence_mapped=self.document.selection_spans(source,target)
        selected=self.document.corresponding(side,begin,end,spans=spans)
        if not selected and not self.document.matches(source,target):self.status='等待当前文本翻译后对齐'
        for offset,other_end,(s0,s1,t0,t1) in selected:
            s,t=source[s0:s1],target[t0:t1]
            own0,own1=(s0,s1) if side=='source' else (t0,t1)
            own,other=(s,t) if side=='source' else (t,s)
            start,stop=max(0,begin-own0),min(own1-own0,end-own0)
            raw=own[start:stop];selection=raw.strip()
            if not selection:continue
            start+=len(raw)-len(raw.lstrip());stop=start+len(selection)
            if start==0 and stop==len(own):
                self.ranges.append((offset,other_end));continue
            if own==other:
                self.ranges.append((offset+start,offset+stop));continue
            literal=self._literal(other,selection)
            if literal is not None:
                self.ranges.append((offset+literal[0],offset+literal[1]));continue
            key=(side,s,t,start,stop)
            if key in self.cache:
                self.cache.move_to_end(key)
                self.ranges.extend((offset+a,offset+b) for a,b in self.cache[key])
            else:
                if sentence_mapped:self.context[key]=(offset,other_end)
                if allow_fallback and self.api is not None and len(selection)<=1000:self.queue.append((key,offset,selection))
                elif not sentence_mapped:self.status='未找到对应词句'
        self._publish()
        if self.queue:self.timer.start(110)

    def pointer_released(self):
        self._pointer_ended=True
        if self.queue and self.pending is None:self.timer.start(0)

    @staticmethod
    def _literal(text,selected):
        # Shared numbers, product codes and URLs have an exact literal match.
        # Common natural-language words still need semantic alignment.
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:/-]*',selected):return None
        if not (any(c.isdigit() for c in selected) or '://' in selected):return None
        matches=list(re.finditer(r'(?<![A-Za-z0-9_])'+re.escape(selected)+r'(?![A-Za-z0-9_])',text))
        return matches[0].span() if len(matches)==1 else None

    def _request(self):
        if self.pending is not None or not self.queue:return
        if not self._pointer_ended and QApplication.mouseButtons() & Qt.LeftButton:self.timer.start(65);return
        key,offset,selection=self.queue.pop(0)
        self.pending=(key,offset)
        side,source,target,begin,_=key
        self.api.align(self.epoch,source,target,selection,side,begin)

    def _done(self,epoch,value,error):
        if self.pending is None or epoch!=self.epoch:return
        (key,offset),self.pending=self.pending,None
        side,source,target,begin,end=key;other=target if side=='source' else source
        spans=self._response_spans(other,value) if not error and isinstance(value,dict) else []
        if spans:
            self.cache[key]=spans
            if len(spans)==1:
                other_side='target' if side=='source' else 'source'
                start,stop=spans[0]
                reverse=(other_side,source,target,start,stop)
                self.cache.setdefault(reverse,[(begin,end)])
            while len(self.cache)>128 or sum(len(k[1])+len(k[2]) for k in self.cache)>128000:self.cache.popitem(last=False)
            self.context.pop(key,None)
            self.ranges.extend((offset+a,offset+b) for a,b in spans)
        elif key not in self.context:
            self.status='划词匹配暂不可用' if error else '未找到对应词句'
        self._publish()
        if self.queue:self.timer.start(0)

    @staticmethod
    def _response_spans(text,value):
        if value.get('certain') is not True:return []
        matches=value.get('matches')
        if isinstance(matches,list) and 0<len(matches)<=12:
            spans=[]
            for entry in matches:
                if not isinstance(entry,dict):return []
                a,b,quote=entry.get('start'),entry.get('end'),entry.get('quote')
                if type(a) is not int or type(b) is not int or not 0<=a<b<=len(text) or text[a:b]!=quote:
                    return []
                spans.append((a,b))
            return sorted(set(spans))
        legacy=unique_quote(text,value.get('quote'))
        return [legacy] if legacy else []
