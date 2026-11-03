"""Private, request-bound local references; independent of cloud body policy.

These receipts authorize one explicit Paste only. They cannot establish a cloud
clipboard baseline or grant permission to upload a pre-existing system value.
"""
from __future__ import annotations
from dataclasses import dataclass
import json
from pathlib import Path
import re
import struct
import sys

MAGIC=b'FDREF1\0'
MAX_REFERENCE_BYTES=16*1024*1024
MAX_FRAME_BYTES=MAX_REFERENCE_BYTES*3+4096
MAX_PATH_UNITS=32767
STATUS_CODES={
    'paths':frozenset(('',)),
    'not_files':frozenset(('',)),
    'unavailable':frozenset(('BUSY','CHANGED','REFERENCE_LIMIT','READ_UNAVAILABLE',
        'CLOSE_UNAVAILABLE','INVALID_REFERENCES','UNSUPPORTED_PLATFORM')),
}


def checked_request(request,sequence):
    if not isinstance(request,str) or re.fullmatch('[0-9a-f]{32}',request) is None:
        raise ValueError('Invalid reference request')
    if type(sequence) is not int or not 0<sequence<=0xffffffff:
        raise ValueError('Invalid reference sequence')


def checked_paths(paths):
    if not isinstance(paths,(list,tuple)) or not paths:
        raise ValueError('Invalid local references')
    size=0
    for path in paths:
        if not isinstance(path,str) or not path or '\0' in path:
            raise ValueError('Invalid local reference')
        encoded=path.encode('utf-16-le','strict')
        if len(encoded)>MAX_PATH_UNITS*2:raise ValueError('Local reference too long')
        size+=len(encoded)+2
        if size>MAX_REFERENCE_BYTES:raise ValueError('Local reference limit')
    return tuple(paths)


def receipt(request,sequence,status,*,paths=(),code=''):
    checked_request(request,sequence)
    if status not in STATUS_CODES or code not in STATUS_CODES[status]:
        raise ValueError('Invalid reference result')
    if not isinstance(paths,(list,tuple)):raise ValueError('Invalid reference list')
    if status=='paths':paths=checked_paths(paths)
    elif paths:raise ValueError('Non-file result has paths')
    return dict(purpose='explicit-local-references',request=request,sequence=sequence,
                status=status,paths=tuple(paths),code=code)


def encode_frame(value):
    checked=receipt(value['request'],value['sequence'],value['status'],paths=value['paths'],code=value['code'])
    raw=json.dumps(checked,ensure_ascii=True,separators=(',',':')).encode('ascii')
    if len(raw)>MAX_FRAME_BYTES:raise ValueError('Reference frame limit')
    return MAGIC+struct.pack('<I',len(raw))+raw


def decode_frame(raw,*,request,sequence):
    start=len(MAGIC)+4
    if not isinstance(raw,bytes) or len(raw)<start or not raw.startswith(MAGIC):
        raise ValueError('Invalid reference frame')
    size=struct.unpack_from('<I',raw,len(MAGIC))[0]
    if not 0<size<=MAX_FRAME_BYTES or len(raw)!=start+size:
        raise ValueError('Invalid reference frame size')
    def unique_pairs(pairs):
        value={}
        for key,item in pairs:
            if key in value:raise ValueError('Duplicate reference field')
            value[key]=item
        return value
    value=json.loads(raw[start:].decode('ascii'),object_pairs_hook=unique_pairs)
    keys={'purpose','request','sequence','status','paths','code'}
    if not isinstance(value,dict) or set(value)!=keys or value['purpose']!='explicit-local-references':
        raise ValueError('Invalid reference schema')
    if value['request']!=request or type(value['sequence']) is not int or value['sequence']!=sequence:
        raise ValueError('Stale reference request')
    return receipt(request,sequence,value['status'],paths=value['paths'],code=value['code'])


@dataclass(frozen=True)
class ReferenceReadPurpose:
    request:str
    sequence:int
    canonicalize:bool=False

    def __post_init__(self):
        checked_request(self.request,self.sequence)
        if type(self.canonicalize)is not bool:raise ValueError('Invalid local reference purpose')
    @property
    def max_wire(self):return MAX_FRAME_BYTES+len(MAGIC)+4
    @staticmethod
    def cache(cancel):return None
    def arguments(self,cache):
        if cache is not None:raise RuntimeError('Reference reader cannot own a body cache')
        arguments=['--request',self.request,'--sequence',str(self.sequence)]
        return arguments+['--canonicalize','1'] if self.canonicalize else arguments
    def decode(self,raw,cache):
        if cache is not None:raise RuntimeError('Reference reader cannot adopt body data')
        return decode_frame(raw,request=self.request,sequence=self.sequence)
    def failure(self,code='READ_UNAVAILABLE',diagnostic=None):
        # Process/lifecycle codes are typed separately from native capture codes.
        return dict(purpose='explicit-local-references',request=self.request,sequence=self.sequence,
                    status='failed',paths=(),code=code,diagnostic=diagnostic)
    @staticmethod
    def command():
        if getattr(sys,'frozen',False):return [sys.executable,'--clipboard-references']
        return [sys.executable,str(Path(__file__).with_name('win_clipboard_references.py'))]
