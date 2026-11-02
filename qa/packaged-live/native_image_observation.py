"""Failure-only PNG diagnostics for an owned ephemeral Windows producer.

No application hook, clipboard mutation, arbitrary format enumeration or normal
acceptance. The format probe runs in a protected owned process, never the GUI.
"""
from __future__ import annotations
import hashlib,json,os,struct,sys
from pathlib import Path
import native_reader_observation as base

FORMATS=('PNG','image/png')
MAX_SYNTHETIC_BYTES=4*1024*1024 # Known 640x360 RGBA QA fixture, not a user policy.
FAILURE_LABEL='06-native-original-image-copy'

def _format_clipboard():
    import ctypes
    from win_clipboard_native import NativeClipboard,native_error
    class CheckedFormats(NativeClipboard):
        def available(self,name):
            identifier=self.format(name)
            if not identifier:raise native_error('metadata')
            ctypes.set_last_error(0)
            value=bool(self.user.IsClipboardFormatAvailable(identifier))
            if not value and ctypes.get_last_error():raise native_error('metadata')
            return value
    return CheckedFormats()

def _original(observer,path,expected):
    target=Path(path).absolute();base._no_links(target)
    wanted=observer.producer_profile.parent/'files/original-image.png'
    if (target.resolve(strict=True)!=wanted.resolve(strict=True) or not target.is_file()
            or not isinstance(expected,str) or not base._SHA.fullmatch(expected)
            or not 0<target.stat().st_size<=MAX_SYNTHETIC_BYTES):raise RuntimeError('IMAGE_OBSERVER_ORIGINAL_REQUIRED')
    raw=target.read_bytes()
    if (hashlib.sha256(raw).hexdigest()!=expected or len(raw)<33 or raw[:8]!=b'\x89PNG\r\n\x1a\n'
            or raw[12:16]!=b'IHDR' or struct.unpack('!II',raw[16:24])!=(640,360)):
        raise RuntimeError('IMAGE_OBSERVER_ORIGINAL_MISMATCH')
    return raw

def _format_capture(api,clipboard,owner,sequence,original):
    """Only native memory copies under lock; hash/fixture I/O is outside it."""
    if api.owner()!=owner or api.sequence()!=sequence:raise RuntimeError('IMAGE_OBSERVER_OWNER_CHANGED')
    if not clipboard.open():return {'unavailable':'CLIPBOARD_BUSY'}
    copied={};after=None
    try:
        if api.owner()!=owner or api.sequence()!=sequence:raise RuntimeError('IMAGE_OBSERVER_OWNER_CHANGED')
        for name in FORMATS:
            if clipboard.available(name):copied[name]=clipboard.read(name,MAX_SYNTHETIC_BYTES)
        after=api.sequence()
    finally:
        try:clipboard.close()
        except OSError as error:error.phase='close';raise
    if after!=sequence or api.sequence()!=sequence or api.owner()!=owner:raise RuntimeError('IMAGE_OBSERVER_OWNER_CHANGED')
    rows=[]
    for name in FORMATS:
        raw=copied.get(name)
        if raw is None:rows.append({'format':name,'advertised':False});continue
        prefix=len(raw)>=len(original) and raw[:len(original)]==original
        extra=raw[len(original):] if prefix else None
        rows.append({'format':name,'advertised':True,'size':len(raw),'sha256':hashlib.sha256(raw).hexdigest(),
            'matches_original':raw==original,'original_prefix_equal':prefix,
            'extra_bytes':len(extra) if extra is not None else None,
            'extra_all_zero':not any(extra) if extra is not None else None})
    return {'formats':rows,'sequence':sequence}

def _checked_formats(value,expected_sequence,expected_sha256,expected_size):
    if not isinstance(value,dict):raise ValueError('IMAGE_OBSERVER_INVALID_FORMAT_REPORT')
    if value=={'unavailable':'CLIPBOARD_BUSY'}:return value
    if set(value)!={'formats','sequence'} or type(value['sequence'])is not int or value['sequence']!=expected_sequence:raise ValueError('IMAGE_OBSERVER_INVALID_FORMAT_REPORT')
    rows=value['formats']
    if not isinstance(rows,list) or len(rows)!=2:raise ValueError('IMAGE_OBSERVER_INVALID_FORMAT_REPORT')
    for row,name in zip(rows,FORMATS):
        if not isinstance(row,dict) or row.get('format')!=name or type(row.get('advertised'))is not bool:raise ValueError('IMAGE_OBSERVER_INVALID_FORMAT_REPORT')
        if not row['advertised']:
            if set(row)!={'format','advertised'}:raise ValueError('IMAGE_OBSERVER_INVALID_FORMAT_REPORT')
            continue
        if (set(row)!={'format','advertised','size','sha256','matches_original','original_prefix_equal','extra_bytes','extra_all_zero'}
                or type(row['size'])is not int or not 0<row['size']<=MAX_SYNTHETIC_BYTES
                or not isinstance(row['sha256'],str) or not base._SHA.fullmatch(row['sha256'])
                or any(type(row[key])is not bool for key in ('matches_original','original_prefix_equal'))):raise ValueError('IMAGE_OBSERVER_INVALID_FORMAT_REPORT')
        extra=row['extra_bytes'];zero=row['extra_all_zero']
        if row['matches_original']!=(row['size']==expected_size and row['sha256']==expected_sha256):raise ValueError('IMAGE_OBSERVER_INVALID_FORMAT_REPORT')
        if row['original_prefix_equal']:
            if (type(extra)is not int or extra!=row['size']-expected_size or extra<0 or type(zero)is not bool
                    or extra==0 and (not row['matches_original'] or not zero)):raise ValueError('IMAGE_OBSERVER_INVALID_FORMAT_REPORT')
        elif row['matches_original']:raise ValueError('IMAGE_OBSERVER_INVALID_FORMAT_REPORT')
        elif extra is not None or zero is not None:raise ValueError('IMAGE_OBSERVER_INVALID_FORMAT_REPORT')
    return value

def _run(observer,command):
    operation=observer._operation_factory(command);observer._owned_operation=operation
    operation.start()
    if not operation.done.wait(base.OBSERVATION_WAIT_SECONDS):
        operation.cancel('CANCELLED');operation.done.wait(base.CLOSE_WAIT_SECONDS)
    process=operation.process
    summary={'helper_done':operation.done.is_set(),'helper_reaped':operation.reaped,
        'helper_exit_code':process.returncode if process is not None and type(process.returncode)is int else None}
    if operation.done.is_set():summary['result']=base._result_summary(operation.result if isinstance(operation.result,dict) else {})
    return operation,summary

def after_native_image_failure(observer,exe,exe_sha256,producer,original_png,expected_sha256,*,failure_label=FAILURE_LABEL):
    """Call only from the original PNG assertion failure, before producer cleanup."""
    if not isinstance(observer,base.HostedNativeReaderObservation):raise RuntimeError('IMAGE_OBSERVER_SCOPE_REQUIRED')
    observer._guard()
    if not observer._initial or observer._failure or failure_label!=FAILURE_LABEL:raise RuntimeError('IMAGE_OBSERVER_FAILURE_SCOPE_REQUIRED')
    observer._failure=True;observer._producer(producer)
    path=Path(exe).absolute();base._no_links(path)
    if (path.resolve(strict=True)!=(base.REPO/'dist/FlickDock/FlickDock.exe').resolve(strict=True)
            or not isinstance(exe_sha256,str) or not base._SHA.fullmatch(exe_sha256)):
        raise RuntimeError('OBSERVER_IMMUTABLE_EXE_REQUIRED')
    if base._digest(path)!=exe_sha256:raise RuntimeError('OBSERVER_EXE_HASH_MISMATCH')
    original=_original(observer,original_png,expected_sha256)
    api=observer._native();owner=api.owner();sequence=api.sequence()
    row={'observation_only':True,'normal_acceptance':False,'original_failure_preserved':True,
        'expected_original_size':len(original),'expected_original_sha256':expected_sha256,
        'owner_verified':base._owned_owner(owner,producer.pid),'sequence_before_launch':sequence}
    if not row['owner_verified'] or type(sequence)is not int or sequence<=0:
        row['unavailable']='CLIPBOARD_OWNER_OR_SEQUENCE_UNVERIFIED';return row
    observer._producer(producer)
    operation,summary=_run(observer,[str(path),'--clipboard-reader']);row['frozen_helper']=summary
    if not operation.done.is_set() or not operation.reaped:row['unavailable']='HELPER_UNREAPED';return row
    if base._digest(path)!=exe_sha256:row['unavailable']='EXE_CHANGED_DURING_OBSERVATION';return row
    if api.owner()!=owner or api.sequence()!=sequence or producer.poll() is not None:
        row['unavailable']='CLIPBOARD_OWNER_CHANGED';return row
    result=operation.result if isinstance(operation.result,dict) else {};value=result.get('value')
    if (result.get('valid') is True and result.get('sequence_before')==result.get('sequence_after')==sequence
            and isinstance(value,tuple) and len(value)==2 and value[0]=='image' and isinstance(value[1],dict)
            and isinstance(value[1].get('raw'),bytes) and value[1].get('mime') in ('image/png','image/jpeg','image/webp','image/bmp','image/tiff')):
        raw=value[1]['raw'];row['frozen_content']={'kind':'image','mime':value[1]['mime'],'size':len(raw),'sha256':hashlib.sha256(raw).hexdigest(),'matches_original':raw==original}
    observer._producer(producer)
    command=[sys.executable,str(Path(__file__).resolve()),'--format-child',str(observer.root),observer.nonce,
        str(observer.producer_profile),str(producer.pid),str(owner[0]),str(sequence),expected_sha256]
    formats,summary=_run(observer,command);row['format_helper']=summary
    if not formats.done.is_set() or not formats.reaped:row['unavailable']='FORMAT_HELPER_UNREAPED';return row
    if base._digest(path)!=exe_sha256 or api.owner()!=owner or api.sequence()!=sequence or producer.poll() is not None:
        row.pop('frozen_content',None);row['unavailable']='IMAGE_OBSERVER_SCOPE_CHANGED';return row
    result=formats.result if isinstance(formats.result,dict) else {};value=result.get('value')
    if (result.get('valid') is True and result.get('sequence_before')==result.get('sequence_after')==sequence
            and isinstance(value,tuple) and len(value)==2 and value[0]=='text' and isinstance(value[1],dict)
            and isinstance(value[1].get('text'),str) and len(value[1]['text'])<=base.MARKER_MAX_BYTES*8):
        try:row['native_formats']=_checked_formats(json.loads(value[1]['text']),sequence,expected_sha256,len(original))
        except (ValueError,TypeError,KeyError):row['unavailable']='INVALID_FORMAT_REPORT'
    else:row['unavailable']='FORMAT_READ_UNAVAILABLE'
    return row

def _child_arguments(args):
    if len(args)!=14 or args[0]!='--format-child':raise ValueError('IMAGE_OBSERVER_CHILD_ARGUMENTS')
    root,nonce,profile,pid,hwnd,sequence,expected=args[1:8]
    if (any(not value.isascii() or not value.isdigit() for value in (pid,hwnd,sequence))
            or not 0<int(pid)<=0xffffffff or not 0<int(hwnd)<2**64 or not 0<int(sequence)<=0xffffffff
            or not base._SHA.fullmatch(expected)):raise ValueError('IMAGE_OBSERVER_CHILD_ARGUMENTS')
    from win_clipboard_native import parse_cache_arguments
    if parse_cache_arguments(args[8:]) is None:raise ValueError('IMAGE_OBSERVER_CHILD_CAPABILITY')
    return root,nonce,profile,int(pid),int(hwnd),int(sequence),expected

def child_main(args):
    try:
        base._hosted()
        # Python starts this standalone QA script with only its own directory
        # on sys.path. Import production primitives from the guarded checkout.
        sys.path.insert(0,str(base.REPO))
        root,nonce,profile,pid,hwnd,sequence,expected=_child_arguments(list(args))
        observer=base.HostedNativeReaderObservation(root,nonce,producer_profile=profile,create=False)
        observer._guard();original=_original(observer,observer.producer_profile.parent/'files/original-image.png',expected)
        from win_clipboard_native import TooLarge,encode_frame,write_stdout,safe_failure
        try:value=_format_capture(observer._native(),_format_clipboard(),(hwnd,pid),sequence,original)
        except (OSError,RuntimeError,ValueError,MemoryError,TooLarge) as error:
            # An unsuccessful Close must exit immediately, not hash/write a
            # diagnostic while the process may still own the global lock.
            if getattr(error,'phase',None)=='close':return 3
            write_stdout(encode_frame(dict(status='unknown',valid=False,known_empty=False,sequence_before=sequence,
                sequence_after=sequence,sequence=sequence,value=None,marker=b'',code='READ_UNAVAILABLE',
                diagnostic=safe_failure(getattr(error,'phase','copy'),error))))
            return 0
        # Reuse the owned binary transport, with only fixed-schema SHA facts.
        text=json.dumps(value,separators=(',',':'))
        write_stdout(encode_frame(dict(status='ready',valid=True,known_empty=False,sequence_before=sequence,
            sequence_after=sequence,sequence=sequence,value=('text',dict(text=text,html='')),marker=b'',code='')))
        return 0
    except Exception:return 2 # No raw exception, path, bytes or commandline output.

if __name__=='__main__':raise SystemExit(child_main(sys.argv[1:]))
