"""Bounded sentence reuse and explicit bilingual spans; no proportional matching.

Only successful translations enter the in-memory cache. A plan is committed as a
whole so a late/cancelled batch cannot mix document generations. No copied text is
logged or persisted by this module.
"""
from collections import Counter, OrderedDict
from dataclasses import dataclass, replace
from difflib import SequenceMatcher
import re


def utf16_position(text,position):return len(text[:position].encode('utf-16-le'))//2
def python_position(text,position):return len(text.encode('utf-16-le')[:max(0,position)*2].decode('utf-16-le','ignore'))


def editor_to_text_position(text,position):
    """Map a Qt cursor into an original string that may contain CRLF."""
    offset=python_position(text.replace('\r\n','\n'),position)
    pair=text.find('\r\n')
    while 0<=pair<offset:
        offset+=1;pair=text.find('\r\n',pair+2)
    return offset


def text_to_editor_position(text,position):
    return utf16_position(text,position)-text.count('\r\n',0,position)


@dataclass
class Unit:
    text:str
    start:int
    end:int
    gap:str=''
    translation:str|None=None


def split_units(text):
    # Do not split decimals, domains or common dotted abbreviations.
    boundaries=[]
    for match in re.finditer(r'[。！？!?；;]+[”’"\')\]]*|\.(?=\s|$)|\n+',text):
        if match.group()=='.':
            prefix=text[:match.start()]
            word=re.search(r'([A-Za-z.]+)$',prefix)
            if word and (word.group().lower() in ('mr','mrs','ms','dr','prof','e.g','i.e','vs') or len(word.group())==1):continue
        boundaries.append(match.end())
    if not boundaries or boundaries[-1]!=len(text):boundaries.append(len(text))
    result=[];start=0
    for end in boundaries:
        raw=text[start:end];content=raw.strip()
        if content:
            begin=start+len(raw)-len(raw.lstrip());finish=end-(len(raw)-len(raw.rstrip()))
            if result:result[-1].gap=text[result[-1].end:begin]
            result.append(Unit(content,begin,finish))
        start=end
    if result:result[-1].gap=text[result[-1].end:]
    return result


class TranslationDocument:
    def __init__(self):
        self.units=[];self.source='';self.target='';self.options=None;self.spans=[]
        self.cache=OrderedDict();self.cache_chars=0
        self._projected_source=None;self._projected_spans=()
    def plan(self,text,options):
        units=split_units(text)
        old=self.units if self.options==options else []
        for block in SequenceMatcher(None,[u.text for u in old],[u.text for u in units],autojunk=False).get_matching_blocks():
            for offset in range(block.size):units[block.b+offset].translation=old[block.a+offset].translation
        for unit in units:
            if unit.translation is None:
                value=self.cache.get((options,unit.text))
                if value is not None:unit.translation=value
        return units
    def commit(self,source,units,options):
        self._projected_source=None;self._projected_spans=()
        output=[];self.spans=[];position=0
        for index,unit in enumerate(units):
            assert isinstance(unit.translation,str) and unit.translation.strip()
            value=unit.translation.strip();output.append(value)
            self.spans.append((unit.start,unit.end,position,position+len(value)))
            position+=len(value)
            gap=unit.gap
            if not gap and index+1<len(units) and value[-1:].isascii() and options[1] not in ('zh','zh-Hant','ja','ko'):gap=' '
            output.append(gap);position+=len(gap)
            key=(options,unit.text);previous=self.cache.pop(key,None)
            if previous is not None:self.cache_chars-=len(unit.text)+len(previous)
            self.cache[key]=value;self.cache_chars+=len(unit.text)+len(value)
        while len(self.cache)>512 or self.cache_chars>256000:
            (_,text),value=self.cache.popitem(last=False);self.cache_chars-=len(text)+len(value)
        self.source=source;self.target=''.join(output);self.options=options;self.units=[replace(unit) for unit in units]
        return self.target
    def pair(self,source,target):
        # Global rewrites/manual output edits have no proven sentence alignment.
        # Keep one honest document pair instead of guessing by sentence number.
        self.source=source;self.target=target;self.options=None
        self._projected_source=None;self._projected_spans=()
        self.cache.clear();self.cache_chars=0
        self.units=[Unit(source,0,len(source),'',target)] if source and target else []
        self.spans=[(0,len(source),0,len(target))] if self.units else []
    def matches(self,source,target):return self.source==source and self.target==target
    def selection_spans(self,source,target):
        if self.matches(source,target):return self.spans,self.options is not None
        if self.options is None or not target or target!=self.target:return (),False
        if self._projected_source!=source:
            # While the old translation is visible, reuse only unchanged,
            # uniquely identifiable sentences. Never guess a changed span.
            current=split_units(source)
            old_count=Counter(unit.text for unit in self.units)
            current_count=Counter(unit.text for unit in current)
            previous={unit.text:span[2:] for unit,span in zip(self.units,self.spans) if old_count[unit.text]==1}
            self._projected_spans=tuple((unit.start,unit.end,*previous[unit.text]) for unit in current
                                        if current_count[unit.text]==1 and unit.text in previous)
            self._projected_source=source
        return self._projected_spans,True

    def corresponding(self,side,begin,end,*,spans=None):
        offset=0 if side=='source' else 2;other=2-offset
        return [(span[other],span[other+1],span) for span in (self.spans if spans is None else spans)
                if begin<span[offset+1] and end>span[offset]]
    def clear(self):
        self.units=[];self.spans=[];self.source=self.target='';self.options=None
        self._projected_source=None;self._projected_spans=()


def unique_quote(text,quote):
    if not isinstance(quote,str) or not quote or len(quote)>len(text):return None
    begin=text.find(quote)
    return (begin,begin+len(quote)) if begin>=0 and text.find(quote,begin+1)<0 else None
