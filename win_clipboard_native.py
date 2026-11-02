"""Isolated Win32 clipboard reads. No Qt, COM, application startup or logging.

GetClipboardData may wait forever for a foreign delayed renderer. This module
is run only in an owned, killable helper process, never the GUI/thread pool.
"""
from __future__ import annotations
import codecs,ctypes,hashlib,itertools,json,mmap,os,re,stat,struct,sys,threading
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass,field
from pathlib import Path

MAX_BYTES=100*1024*1024
MAX_HEADER=16384
MAX_WIRE=MAX_BYTES+MAX_HEADER+2048
NATIVE_COPY_CHUNK_BYTES=262144
MAGIC=b'FDCR1\0'
SYNC_MARKER='application/x-flickdock-cloud-clipboard'
IMAGE_FORMAT_PREFERENCE=(
    # A native alias can be rendered from decoded pixels even when an exact
    # encoded original is also offered. Keep explicit MIME bytes first, in
    # the same order as the original Qt clipboard snapshot path.
    ('image/png','image/png'),('image/jpeg','image/jpeg'),('image/webp','image/webp'),
    ('image/bmp','image/bmp'),('image/tiff','image/tiff'),
    ('PNG','image/png'),('JFIF','image/jpeg'),('WebP','image/webp'),('TIFF','image/tiff'),
)
class TooLarge(Exception):pass
class Unsupported(Exception):pass

_EMPTY_ISSUER=object()
_EMPTY_SCOPE=ContextVar('flickdock_native_empty_scope',default=None)
_METADATA_LOCK=threading.Lock()
_METADATA_SERIAL=itertools.count(1)
_LATEST_METADATA_SERIAL=0
_LATEST_EMPTY_PROOF=None

@dataclass(frozen=True)
class EmptyProof:
    """One in-process metadata request, never a native value/wire sample."""
    sequence:int
    serial:int
    _context:object=field(repr=False,compare=False)
    _issuer:object=field(repr=False,compare=False)

@contextmanager
def empty_proof_scope(context):
    """Expire request authority on return; old objects cannot cross requests."""
    global _LATEST_EMPTY_PROOF
    token=_EMPTY_SCOPE.set(context)
    try:yield
    finally:
        with _METADATA_LOCK:
            if _LATEST_EMPTY_PROOF is not None and _LATEST_EMPTY_PROOF._context is context:_LATEST_EMPTY_PROOF=None
        _EMPTY_SCOPE.reset(token)

def consume_empty_proof(proof,context,sequence):
    global _LATEST_EMPTY_PROOF
    with _METADATA_LOCK:
        valid=(type(proof)is EmptyProof and proof is _LATEST_EMPTY_PROOF and proof._issuer is _EMPTY_ISSUER
               and proof._context is context and _EMPTY_SCOPE.get() is context
               and proof.serial==_LATEST_METADATA_SERIAL and proof.sequence==0
               and type(sequence)is int and 0<=sequence<=0xffffffff)
        if valid:_LATEST_EMPTY_PROOF=None
        return valid

def safe_failure(phase,error):
    """Fixed-schema diagnostics: no exception message, filename or payload."""
    phases={'init','metadata','copy','transform','sequence','close','spawn','job','protect','resume','wait','pipe','decode','cleanup','cache'}
    classes={'OSError','PermissionError','FileNotFoundError','TimeoutError','BrokenPipeError','ValueError','UnicodeDecodeError','RuntimeError','TooLarge','Unsupported','TimeoutExpired','MemoryError','InterruptedError'}
    result={'phase':phase if phase in phases else 'init','exception':type(error).__name__ if type(error).__name__ in classes else 'Exception'}
    for name in ('errno','winerror'):
        value=getattr(error,name,None)
        if type(value)is int and -0x80000000<=value<=0xffffffff:result[name]=value
    return result

def native_error(phase):
    # Snapshot immediately at the failing API, before cleanup changes it.
    error=OSError('Native helper operation failed');error.phase=phase
    getter=getattr(ctypes,'get_last_error',None)
    if getter is not None:error.winerror=int(getter())
    return error

class NativeClipboard:
    def __init__(self):
        if sys.platform!='win32':raise OSError('Unsupported platform')
        from ctypes import wintypes as w
        self.user=ctypes.WinDLL('user32',use_last_error=True);self.kernel=ctypes.WinDLL('kernel32',use_last_error=True);self.shell=ctypes.WinDLL('shell32',use_last_error=True);self.snapshots=[]
        signatures=[(self.user,'OpenClipboard',[w.HWND],w.BOOL),(self.user,'CloseClipboard',[],w.BOOL),
          (self.user,'CountClipboardFormats',[],ctypes.c_int),(self.user,'GetClipboardSequenceNumber',[],w.DWORD),
          (self.user,'IsClipboardFormatAvailable',[w.UINT],w.BOOL),(self.user,'RegisterClipboardFormatW',[w.LPCWSTR],w.UINT),
          (self.user,'GetClipboardData',[w.UINT],w.HANDLE),(self.kernel,'GlobalSize',[w.HGLOBAL],ctypes.c_size_t),
          (self.kernel,'GlobalLock',[w.HGLOBAL],ctypes.c_void_p),(self.kernel,'GlobalUnlock',[w.HGLOBAL],w.BOOL),
          (self.shell,'DragQueryFileW',[w.HANDLE,w.UINT,w.LPWSTR,w.UINT],w.UINT)]
        for library,name,args,result in signatures:
            fn=getattr(library,name);fn.argtypes=args;fn.restype=result
    def open(self):return bool(self.user.OpenClipboard(None))
    def close(self):
        if not self.user.CloseClipboard():raise native_error('close')
    def sequence(self):return int(self.user.GetClipboardSequenceNumber())
    def count(self):
        ctypes.set_last_error(0);n=self.user.CountClipboardFormats()
        if n==0 and ctypes.get_last_error():raise native_error('metadata')
        return int(n)
    def format(self,value):return value if isinstance(value,int) else int(self.user.RegisterClipboardFormatW(value))
    def available(self,value):return bool(self.user.IsClipboardFormatAvailable(self.format(value)))
    def read(self,value,limit):
        handle=self.user.GetClipboardData(self.format(value))
        if not handle:raise native_error('copy')
        size=int(self.kernel.GlobalSize(handle))
        if not size:raise native_error('copy')
        if size>limit:raise TooLarge()
        address=self.kernel.GlobalLock(handle)
        if not address:raise native_error('copy')
        try:return ctypes.string_at(address,size)
        finally:self.kernel.GlobalUnlock(handle)
    def read_snapshot(self,value):
        """Copy only native memory under the clipboard lock; never disk I/O."""
        handle=self.user.GetClipboardData(self.format(value))
        if not handle:raise native_error('copy')
        size=int(self.kernel.GlobalSize(handle))
        if not size:raise native_error('copy')
        address=self.kernel.GlobalLock(handle)
        if not address:raise native_error('copy')
        try:
            if size<=MAX_BYTES:return ctypes.string_at(address,size)
            snapshot=mmap.mmap(-1,size,access=mmap.ACCESS_WRITE)
            try:ctypes.memmove(ctypes.addressof(ctypes.c_char.from_buffer(snapshot)),address,size)
            except Exception:snapshot.close();raise
            self.snapshots.append(snapshot);return snapshot
        finally:self.kernel.GlobalUnlock(handle)
    def file(self):
        handle=self.user.GetClipboardData(15) # CF_HDROP, not a pointer to free.
        if not handle:raise native_error('copy')
        if self.shell.DragQueryFileW(handle,0xffffffff,None,0)!=1:raise Unsupported()
        size=int(self.shell.DragQueryFileW(handle,0,None,0))
        if not 0<size<=32767:raise Unsupported()
        value=ctypes.create_unicode_buffer(size+1)
        if self.shell.DragQueryFileW(handle,0,value,size+1)!=size:raise native_error('copy')
        return value.value

def _base(status='unknown',before=None,after=None,*,empty=False,code=''):
    return dict(status=status,sequence_before=before,sequence_after=after,sequence=after,
                known_empty=bool(empty),valid=status=='ready' and type(before) is int and before>0 and before==after,
                marker=b'',value=None,code=code)

def native_metadata(provider=None):
    """One immediate Open attempt; no GetClipboardData/OLE or busy retry."""
    global _LATEST_METADATA_SERIAL,_LATEST_EMPTY_PROOF
    serial=next(_METADATA_SERIAL)
    with _METADATA_LOCK:_LATEST_METADATA_SERIAL=serial;_LATEST_EMPTY_PROOF=None
    opened=False;before=after=None;count=None;result=None
    try:
        p=provider or NativeClipboard();before=p.sequence()
        if type(before)is not int or not 0<=before<=0xffffffff:return _base(before=before,code='SEQUENCE_UNAVAILABLE')
        if not p.open():return _base(before=before,code='BUSY')
        opened=True;count=p.count();after=p.sequence()
        if type(count)is not int or count<0:raise ValueError('Invalid clipboard format count')
        if type(after)is not int or not 0<=after<=0xffffffff:raise ValueError('Invalid clipboard counter')
        result=_base('ready' if before==after and before>0 else 'changed' if before!=after else 'unknown',before,after,empty=count==0 and before==after and before>0,code='SEQUENCE_UNAVAILABLE' if before==after==0 else '')
        result['format_count']=count
    except Exception as error:return dict(_base(before=before,after=after,code='METADATA_UNAVAILABLE'),diagnostic=safe_failure('metadata',error))
    finally:
        if opened:
            try:p.close()
            except OSError as error:return dict(_base(before=before,after=after,code='CLOSE_UNAVAILABLE'),diagnostic=safe_failure('close',error))
    context=_EMPTY_SCOPE.get()
    if context is not None and count==0 and before==after==0:
        with _METADATA_LOCK:
            if serial==_LATEST_METADATA_SERIAL:
                proof=EmptyProof(0,serial,context,_EMPTY_ISSUER);_LATEST_EMPTY_PROOF=proof;result['empty_proof']=proof
    return result

def unicode_text(raw):
    if len(raw)%2:raise Unsupported()
    at=raw.find(b'\0\0')
    while at>=0 and at%2:at=raw.find(b'\0\0',at+1)
    if at<0:raise Unsupported()
    return raw[:at].decode('utf-16-le','strict')

def html_text(raw):
    fields={k.decode():int(v) for k,v in re.findall(rb'(StartHTML|EndHTML|StartFragment|EndFragment):\s*(-?\d+)',raw[:4096])}
    start,end=fields.get('StartHTML',-1),fields.get('EndHTML',-1)
    if not 0<=start<=end<=len(raw):start,end=fields.get('StartFragment',-1),fields.get('EndFragment',-1)
    if not 0<=start<=end<=len(raw):raise Unsupported()
    return raw[start:end].decode('utf8','strict')

def dib_layout(raw):
    """Return complete lossless image slices without copying the pixel body."""
    if len(raw)<12:raise Unsupported()
    header=struct.unpack_from('<I',raw)[0]
    if header==12:
        width,height,planes,bpp=struct.unpack_from('<HHHH',raw,4);compression=0;image_size=0
        palette=(1<<bpp)*3 if bpp<=8 else 0;offset=header+palette
    elif header in (40,52,56,108,124) and len(raw)>=header:
        width,height,planes,bpp,compression,image_size=struct.unpack_from('<iiHHII',raw,4)
        used=struct.unpack_from('<I',raw,32)[0]
        if compression not in (0,1,2,3,4,5,6):raise Unsupported()
        masks=(16 if compression==6 else 12) if header==40 and compression in (3,6) else 0
        if bpp<=8 and used>(1<<bpp):raise Unsupported()
        offset=header+masks+((used or (1<<bpp))*4 if bpp<=8 else used*4)
    else:raise Unsupported()
    if width<=0 or height==0 or planes!=1 or bpp not in (1,4,8,16,24,32) and compression not in (4,5):raise Unsupported()
    if compression in (0,3,6):length=((width*bpp+31)//32)*4*abs(height)
    else:
        if image_size<=0:raise Unsupported()
        length=image_size
    end=offset+length
    if header==124:
        profile_start,profile_size=struct.unpack_from('<II',raw,112)
        if profile_size:end=max(end,profile_start+profile_size)
    if offset>len(raw) or end>len(raw):raise Unsupported()
    if compression in (4,5):return b'',offset,offset+length,('image/jpeg' if compression==4 else 'image/png')
    return struct.pack('<2sIHHI',b'BM',end+14,0,0,offset+14),0,end,'image/bmp'

def dib_image(raw,limit=MAX_BYTES):
    """Preserve original pixels/bitstream; add BMP framing, never JPEG encode."""
    prefix,start,end,mime=dib_layout(raw)
    if len(prefix)+end-start>limit:raise TooLarge()
    return prefix+raw[start:end],mime

def _chunks(raw,start=0,end=None):
    end=len(raw) if end is None else end
    for offset in range(start,end,NATIVE_COPY_CHUNK_BYTES):yield raw[offset:min(end,offset+NATIVE_COPY_CHUNK_BYTES)]

def _text_parts(raw,html,limit,*,cancel=None,encoding='utf-16-le'):
    if encoding=='utf-16-le':
        if len(raw)%2:raise Unsupported()
        end=raw.find(b'\0\0')
        while end>=0 and end%2:end=raw.find(b'\0\0',end+1)
        if end<0:raise Unsupported()
    elif encoding=='utf-8':end=len(raw)
    else:raise Unsupported()
    # Every UTF-16 code unit needs at least one UTF-8 byte (a surrogate pair
    # needs four). This lower bound can prove ineligibility before decoding;
    # still validate and hash every original chunk, without retaining a full
    # decoded copy which will be discarded just above the cloud threshold.
    too_large=end//2>limit if encoding=='utf-16-le' else end>limit
    decoder=codecs.getincrementaldecoder(encoding)('strict');text=[];size=0;digest=hashlib.sha256()
    for chunk in _chunks(raw,0,end):
        if cancel is not None and cancel.is_set():raise InterruptedError('Original analysis cancelled')
        part=decoder.decode(chunk);encoded=part.encode('utf8');size+=len(encoded);digest.update(encoded)
        if not too_large:
            if size<=limit:text.append(part)
            else:text.clear();too_large=True
    final=decoder.decode(b'',final=True);size+=len(final.encode('utf8'));digest.update(final.encode('utf8'))
    if not too_large:
        if size<=limit:text.append(final)
        else:text.clear();too_large=True
    html_value='';html_size=0;html_digest=hashlib.sha256()
    if html is not None:
        fields={k.decode():int(v) for k,v in re.findall(rb'(StartHTML|EndHTML|StartFragment|EndFragment):\s*(-?\d+)',html[:4096])}
        a,b=fields.get('StartHTML',-1),fields.get('EndHTML',-1)
        if not 0<=a<=b<=len(html):a,b=fields.get('StartFragment',-1),fields.get('EndFragment',-1)
        if not 0<=a<=b<=len(html):raise Unsupported()
        html_decoder=codecs.getincrementaldecoder('utf8')('strict');pieces=[]
        for chunk in _chunks(html,a,b):
            if cancel is not None and cancel.is_set():raise InterruptedError('Original analysis cancelled')
            part=html_decoder.decode(chunk);html_size+=len(chunk);html_digest.update(chunk)
            if not too_large and size+html_size<=limit:pieces.append(part)
            else:pieces.clear();too_large=True
        part=html_decoder.decode(b'',final=True)
        if not too_large:pieces.append(part);html_value=''.join(pieces)
    fingerprint=hashlib.sha256(struct.pack('<QQ',size,html_size)+digest.digest()+html_digest.digest()).hexdigest()
    return (None if too_large else dict(text=''.join(text),html=html_value)),size+html_size,fingerprint,end+(2 if encoding=='utf-16-le' else 0)

def capture(provider=None,limit=MAX_BYTES,*,cache=None):
    opened=False;before=after=None;phase='init'
    try:
        p=provider or NativeClipboard();before=p.sequence()
        if not before:return _base(before=before,code='SEQUENCE_UNAVAILABLE')
        if not p.open():return _base(before=before,code='BUSY')
        opened=True
        read=(lambda name,maximum:p.read_snapshot(name)) if cache is not None and hasattr(p,'read_snapshot') else p.read
        # Keep the station-wide lock only for native data copying. UNC/cloud
        # file stat, HTML/text decoding and image conversion must not own it.
        try:
            phase='copy';count=p.count();copied=None;marker=b''
            if count:
                marker=p.read(SYNC_MARKER,1024).rstrip(b'\0') if p.available(SYNC_MARKER) else b''
                if p.available(15):copied=('files',p.file())
                else:
                    for name,mime in IMAGE_FORMAT_PREFERENCE:
                        if p.available(name):copied=('image',(read(name,limit),mime));break
                    if copied is None and (p.available(17) or p.available(8)):copied=('dib',read(17 if p.available(17) else 8,limit))
                    if copied is None and p.available(13):copied=('text',(read(13,2*limit+2),read('HTML Format',limit+65536) if p.available('HTML Format') else None))
            after=p.sequence()
        finally:
            opened=False
            try:p.close()
            except Exception:phase='close';raise
        phase='transform'
        if before!=after:return _base('changed',before,after)
        if count==0:return _base('ready',before,after,empty=True)
        value=None;file_signature=None;source_stat=None;local=None;eligible=True
        if copied is not None and copied[0]=='files':
            path=copied[1]
            if not isinstance(path,str) or not path or '\0' in path or len(path)>32767:raise Unsupported()
            source=Path(path)
            if not source.is_absolute() or source.is_symlink() or source.is_junction():raise Unsupported()
            info=source.stat()
            if not stat.S_ISREG(info.st_mode):raise Unsupported()
            eligible=info.st_size<=limit
            value=('files',[path])
            file_signature=('files',((os.path.normcase(os.path.abspath(path)),info.st_size,info.st_mtime_ns),))
            source_stat=[info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns]
        elif copied is not None and copied[0]=='image':
            raw,mime=copied[1]
            if not raw:raise Unsupported()
            if len(raw)>limit:
                if cache is None:raise TooLarge()
                phase='cache'
                part=cache.write('image',_chunks(raw),mime=mime);local=cache.descriptor('image',len(raw),part['sha256']);eligible=False
            else:value=('image',dict(raw=bytes(raw),mime=mime))
        elif copied is not None and copied[0]=='dib':
            raw=copied[1];prefix,start,end,mime=dib_layout(raw)
            if len(prefix)+end-start>limit:
                if cache is None:raise TooLarge()
                phase='cache'
                def image_chunks():yield prefix;yield from _chunks(raw,start,end)
                part=cache.write('image',image_chunks(),mime=mime);cache.write('native_image',_chunks(raw,0,end))
                local=cache.descriptor('image',part['size'],part['sha256']);eligible=False
            else:
                image,mime=dib_image(raw,limit);value=('image',dict(raw=image,mime=mime))
        elif copied is not None and copied[0]=='text':
            raw,html=copied[1]
            if cache is None:
                text=unicode_text(raw);formatted=html_text(html) if html is not None else ''
                if len(text.encode('utf8'))+len(formatted.encode('utf8'))>limit:raise TooLarge()
                if text:value=('text',dict(text=text,html=formatted))
            else:
                text,size,fingerprint,end=_text_parts(raw,html,limit)
                if text is None:
                    phase='cache'
                    cache.write('text',_chunks(raw,0,end),encoding='utf-16-le')
                    if html is not None:cache.write('html',_chunks(html),encoding='utf-8')
                    local=cache.descriptor('text',size,fingerprint);eligible=False
                elif text['text']:value=('text',text)
        phase='sequence';after=p.sequence()
        if before!=after:return _base('changed',before,after)
        if value is None and local is None:return _base(before=before,after=after,code='UNSUPPORTED')
        result=_base('ready',before,after);result.update(value=value,marker=marker,sync_eligible=eligible)
        if local is not None:result.update(local)
        if file_signature is not None:result.update(signature=file_signature,source_stat=source_stat)
        return result
    except TooLarge:
        return _base('overlimit',before,after if after is not None else before,code='SIZE_POLICY')
    except Exception as error:
        resource=isinstance(error,MemoryError) or getattr(error,'errno',None) in (12,23,24,28) or getattr(error,'winerror',None) in (8,14,112,1450)
        return dict(_base(before=before,after=after,code='RESOURCE_UNAVAILABLE' if resource else 'READ_UNAVAILABLE'),diagnostic=safe_failure(phase,error))
    finally:
        if opened:p.close()
        for snapshot in getattr(locals().get('p',None),'snapshots',()):snapshot.close()

def encode_frame(result):
    parts=[];data=[];value=result.get('value') if result.get('valid') else None
    def add(name,raw):parts.append(dict(name=name,size=len(raw)));data.append(raw)
    if result.get('valid') and result.get('local_kind') in ('text','image'):
        kind='local';add('marker',result.get('marker',b''))
    elif value is not None:
        add('marker',result.get('marker',b''));kind,body=value
        if kind=='text':add('text',body['text'].encode('utf8'));add('html',body.get('html','').encode('utf8'))
        elif kind=='image':add('image',body['raw'])
        elif kind=='files':
            add('path',body[0].encode('utf8'));add('canonical',result['signature'][1][0][0].encode('utf8'))
        else:raise ValueError('Unsupported result')
    else:kind=None
    header={k:result.get(k) for k in ('status','sequence_before','sequence_after','known_empty','code')}
    if 'diagnostic' in result:header['diagnostic']=result['diagnostic']
    header.update(version=1,kind=kind,parts=parts,mime=value[1]['mime'] if kind=='image' else None)
    header['sync_eligible']=result.get('sync_eligible',True)
    if kind=='local':header.update({key:result[key] for key in ('local_kind','local_parts','logical_size','fingerprint','cache_token')})
    if kind=='files':header.update(file_stat=list(result['signature'][1][0][1:]),source_stat=result['source_stat'])
    encoded=json.dumps(header,separators=(',',':')).encode('ascii')
    if len(encoded)>MAX_HEADER or sum(map(len,data))>MAX_BYTES+1024:raise TooLarge()
    return [MAGIC+struct.pack('<I',len(encoded))+encoded,*data]

def write_stdout(chunks,kernel=None):
    """Windowed frozen Python has no sys.stdout; use the inherited pipe handle."""
    from ctypes import wintypes as w
    kernel=kernel or ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.GetStdHandle.argtypes=[w.DWORD];kernel.GetStdHandle.restype=w.HANDLE
    kernel.WriteFile.argtypes=[w.HANDLE,ctypes.c_void_p,w.DWORD,ctypes.POINTER(w.DWORD),ctypes.c_void_p];kernel.WriteFile.restype=w.BOOL
    handle=kernel.GetStdHandle(w.DWORD(-11))
    if not handle or handle==ctypes.c_void_p(-1).value:raise OSError('Output pipe unavailable')
    for chunk in chunks:
        for offset in range(0,len(chunk),262144):
            remaining=chunk[offset:offset+262144]
            while remaining:
                sent=w.DWORD();buffer=ctypes.create_string_buffer(remaining)
                if not kernel.WriteFile(handle,buffer,len(remaining),ctypes.byref(sent),None) or not 0<sent.value<=len(remaining):raise OSError('Output pipe closed')
                remaining=remaining[sent.value:]

def parse_cache_arguments(args):
    """Pure exact private-helper grammar, validated before any native API."""
    args=list(args)
    if not args:return None
    keys={'--native-cache-root','--native-cache-token','--native-cache-instance'}
    if len(args)!=6 or set(args[::2])!=keys:raise ValueError('Invalid helper arguments')
    values=dict(zip(args[::2],args[1::2]))
    if len(values)!=3 or any(not isinstance(value,str) or not value for value in values.values()):raise ValueError('Invalid helper arguments')
    token=values['--native-cache-token'];instance=values['--native-cache-instance'];root=Path(values['--native-cache-root'])
    from win_clipboard_cache import cache_base
    if not re.fullmatch('[0-9a-f]{32}',token) or not re.fullmatch('[0-9a-f]{32}',instance) or not root.is_absolute() or root.name!=token or root.parent!=cache_base():raise ValueError('Invalid helper capability')
    return dict(root=str(root),token=token,instance=instance)

def main(args=None):
    cache=None;code=0
    try:
        args=list(sys.argv[1:] if args is None else args)
        if args[:1]==['--clipboard-reader']:args=args[1:]
        capability=parse_cache_arguments(args)
        if capability is not None:
            from win_clipboard_cache import CacheSink
            cache=CacheSink(**capability)
        write_stdout(encode_frame(capture(cache=cache)))
    except Exception:code=2 # No payload, file paths or exception text in logs.
    finally:
        if cache is not None:
            try:cache.close()
            except Exception:code=2 # Parent rejects even an already-written frame.
    return code

if __name__=='__main__':raise SystemExit(main())
