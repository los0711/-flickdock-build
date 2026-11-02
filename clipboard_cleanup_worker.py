"""Private, bounded clipboard-cache filesystem capability. No Qt or clipboard."""
from __future__ import annotations
import errno,hashlib,heapq,json,math,os,re,signal,stat,sys,tempfile,time
from contextlib import contextmanager
from pathlib import Path

VERSION=1
RETENTION_SECONDS=86400
BATCH_SIZE=32
MAX_MESSAGE_BYTES=65536
DEADLINE_MS=5000
RETRY_SECONDS=30
FATAL_RECHECK_SECONDS=300
TOKEN=re.compile(r'[0-9a-f]{32}\Z')
NAME=re.compile(r'clip-[A-Za-z0-9_-]{1,64}\Z')
SIDECAR_SUFFIX='.cleanup.json'
PARTIAL_SUFFIX='.cleanup-partial'


class CleanupBoundary(ValueError):pass
class CleanupStale(RuntimeError):pass
class CleanupUnsupported(RuntimeError):pass


def diagnostic(error):
    result={'code':'UNSAFE' if isinstance(error,ValueError) else 'STALE' if isinstance(error,CleanupStale) else 'UNSUPPORTED' if isinstance(error,CleanupUnsupported) else 'ACCESS_DENIED' if isinstance(error,PermissionError) else 'IO',
            'exception':type(error).__name__ if type(error).__name__ in ('OSError','PermissionError','FileNotFoundError') else 'Boundary'}
    for name in ('errno','winerror'):
        value=getattr(error,name,None)
        if type(value)is int:result[name]=value
    return result


def number(value):return type(value)in (float,int) and math.isfinite(value) and value>0


def no_links(path):
    for item in (path,*path.parents):
        info=item.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info,'st_file_attributes',0)&0x400:raise CleanupBoundary('Reparse boundary')


def read_json(path):
    no_links(path)
    with path.open('rb') as source:raw=source.read(MAX_MESSAGE_BYTES+1)
    if len(raw)>MAX_MESSAGE_BYTES:raise CleanupBoundary('Metadata limit')
    return json.loads(raw,object_pairs_hook=unique_object),raw


def unique_object(pairs):
    result={}
    for key,value in pairs:
        if key in result:raise CleanupBoundary('Duplicate metadata field')
        result[key]=value
    return result


def lease(path):
    value,raw=read_json(path)
    if (not isinstance(value,dict) or value.get('flickdock_clipboard')!=1
            or type(value.get('flickdock_clipboard'))is not int or not number(value.get('created_at'))):raise CleanupBoundary('Invalid lease')
    return value['created_at'],hashlib.sha256(raw).hexdigest()


def file_identity(info):
    if type(info.st_dev)is not int or info.st_dev<0 or type(info.st_ino)is not int or info.st_ino<=0:raise CleanupBoundary('Filesystem identity unavailable')
    return [info.st_dev,info.st_ino]


def stamp(path):
    no_links(path);info=path.stat()
    if not stat.S_ISDIR(info.st_mode):raise CleanupBoundary('Directory required')
    return file_identity(info)


@contextmanager
def held_directory(path):
    """Keep an inspected directory from being replaced during traversal."""
    before=stamp(path)
    if os.name=='nt':
        import ctypes
        from ctypes import wintypes as w
        kernel=windows_directory_api()
        kernel.CreateFileW.argtypes=[w.LPCWSTR,w.DWORD,w.DWORD,w.LPVOID,w.DWORD,w.DWORD,w.HANDLE];kernel.CreateFileW.restype=w.HANDLE
        kernel.CloseHandle.argtypes=[w.HANDLE];kernel.CloseHandle.restype=w.BOOL
        handle=kernel.CreateFileW(str(path),0x80,3,None,3,0x02200000,None)
        if handle==ctypes.c_void_p(-1).value:raise ctypes.WinError(ctypes.get_last_error())
        try:
            # Omitting FILE_SHARE_DELETE pins this directory and its name;
            # OPEN_REPARSE_POINT ensures the final component is not followed.
            if stamp(path)!=before:raise CleanupBoundary('Directory changed')
            yield None
        finally:
            if not kernel.CloseHandle(handle):raise ctypes.WinError(ctypes.get_last_error())
    else:
        fd=os.open(path,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
        try:
            info=os.fstat(fd)
            if file_identity(info)!=before:raise CleanupBoundary('Directory changed')
            yield fd
        finally:os.close(fd)


def windows_directory_api():
    import ctypes
    return ctypes.WinDLL('kernel32',use_last_error=True)


def sidecar(cache,name):return cache/(name+SIDECAR_SUFFIX)


def owned(cache,name):
    if not isinstance(name,str) or not NAME.fullmatch(name):raise CleanupBoundary('Invalid target name')
    path=cache/name
    if path.parent!=cache:raise CleanupBoundary('Invalid target parent')
    return path


def side_owner(cache,name):
    value,_=read_json(sidecar(cache,name))
    return checked_owner(value,name)


def checked_owner(value,name):
    if (not isinstance(value,dict) or set(value)!={'version','name','created','stamp','lease_sha256'}
            or type(value['version'])is not int or value['version']!=VERSION or value['name']!=name
            or not number(value['created']) or not isinstance(value['stamp'],list) or len(value['stamp'])!=2
            or any(type(x)is not int or x<0 for x in value['stamp']) or value['stamp'][1]<=0
            or not isinstance(value['lease_sha256'],str) or not re.fullmatch('[0-9a-f]{64}',value['lease_sha256'])):raise CleanupBoundary('Invalid cleanup owner')
    return value


def inspect(cache,name):
    path=owned(cache,name)
    try:identity=stamp(path)
    except FileNotFoundError:
        record=side_owner(cache,name)
        return dict(record,missing=True)
    try:created,digest=lease(path/'.lease.json')
    except FileNotFoundError:
        record=side_owner(cache,name)
        if record['stamp']!=identity:raise CleanupBoundary('Cleanup owner changed')
        return dict(record,missing=False)
    record=dict(version=VERSION,name=name,created=created,stamp=identity,lease_sha256=digest)
    if sidecar(cache,name).exists() and side_owner(cache,name)!=record:raise CleanupBoundary('Conflicting cleanup owner')
    return dict(record,missing=False)


def save_owner(cache,record):
    if os.name!='nt':
        with held_directory(cache) as fd:return save_owner_at(fd,record)
    path=sidecar(cache,record['name']);value={key:record[key] for key in ('version','name','created','stamp','lease_sha256')}
    if path.exists():
        if side_owner(cache,record['name'])!=value:raise CleanupBoundary('Conflicting cleanup owner')
        partial=cache/(record['name']+PARTIAL_SUFFIX)
        if partial.exists():
            pending,_=read_json(partial)
            if checked_owner(pending,record['name'])!=value:raise CleanupBoundary('Unknown owner partial')
            partial.unlink()
        return
    # Publish a complete durable owner before any payload can be removed;
    # a crash while writing a partial file must not poison the final sidecar.
    temporary=cache/(record['name']+PARTIAL_SUFFIX)
    if temporary.exists():
        pending,_=read_json(temporary)
        if checked_owner(pending,record['name'])!=value:raise CleanupBoundary('Unknown owner partial')
    else:
        with temporary.open('x',encoding='ascii') as target:
            json.dump(value,target,separators=(',',':'));target.flush();os.fsync(target.fileno())
    # Windows rename is no-replace. POSIX deletion uses link-at below, not an
    # overwriting rename. Unknown partial bytes are never erased by filename.
    try:temporary.rename(path)
    except FileExistsError:
        if side_owner(cache,record['name'])!=value:raise CleanupBoundary('Conflicting cleanup owner')
        temporary.unlink()


def validate_request(value,token):
    keys={'version','token','mode','cache','targets','references','protected','inventory','cursor','cache_stamp'}
    if (not isinstance(value,dict) or set(value)!=keys or type(value['version'])is not int or value['version']!=VERSION
            or value['token']!=token or value['mode']not in ('plan','delete') or type(value['inventory'])is not bool
            or not isinstance(value['cache'],str) or not Path(value['cache']).is_absolute()
            or Path(value['cache']).name!='clipboard-cache' or not isinstance(value['cursor'],str)
            or len(value['cursor'])>128):raise CleanupBoundary('Invalid cleanup request')
    for key in ('targets','references','protected'):
        if not isinstance(value[key],list) or len(value[key])>BATCH_SIZE:raise CleanupBoundary('Cleanup batch limit')
    if any(not isinstance(path,str) or not path or '\0'in path or not Path(path).is_absolute() for path in value['references']+value['protected']):raise CleanupBoundary('Invalid reference')
    for item in value['targets']:
        if not isinstance(item,dict) or not NAME.fullmatch(str(item.get('name',''))) or not number(item.get('created')):raise CleanupBoundary('Invalid target')
        if value['mode']=='plan':
            if set(item)!={'name','created'}:raise CleanupBoundary('Invalid plan target')
        elif (set(item)!={'version','name','created','stamp','lease_sha256','missing'} or type(item['version'])is not int or item['version']!=VERSION
                or not isinstance(item['stamp'],list) or len(item['stamp'])!=2 or any(type(x)is not int or x<0 for x in item['stamp']) or item['stamp'][1]<=0
                or not isinstance(item['lease_sha256'],str) or not re.fullmatch('[0-9a-f]{64}',item['lease_sha256']) or type(item['missing'])is not bool):raise CleanupBoundary('Invalid delete target')
    if value['mode']=='plan' and value['cache_stamp']is not None:raise CleanupBoundary('Invalid plan stamp')
    if value['mode']=='delete' and (not isinstance(value['cache_stamp'],list) or len(value['cache_stamp'])!=2 or any(type(x)is not int or x<0 for x in value['cache_stamp']) or value['cache_stamp'][1]<=0):raise CleanupBoundary('Invalid cache stamp')
    return value


def execute(request,*,check=lambda:None):
    if request['mode']=='delete' and os.name!='nt':return delete_posix(request,check)
    cache=Path(request['cache']);no_links(cache);base_stamp=stamp(cache)
    if request['mode']=='delete' and request['cache_stamp']!=base_stamp:raise CleanupBoundary('Cache identity changed')
    check();protected=[Path(path).resolve() for path in request['protected']]
    references=[Path(path).resolve() for path in request['references']]
    names={item['name'] for item in request['targets']};more=False;cursor=''
    if request['inventory']:
        # Bounded retained memory and result size. Scanning is killable, and
        # lexical pagination also finds old sidecars without trusting a path.
        with os.scandir(cache) as entries:
            batch=heapq.nsmallest(BATCH_SIZE+1,(entry.name for entry in entries
                if (NAME.fullmatch(entry.name) or entry.name.endswith(SIDECAR_SUFFIX) or entry.name.endswith(PARTIAL_SUFFIX)) and entry.name>request['cursor']))
        more=len(batch)>BATCH_SIZE;batch=batch[:BATCH_SIZE];cursor=batch[-1] if more else ''
        for name in batch:
            for suffix in (SIDECAR_SUFFIX,PARTIAL_SUFFIX):
                if name.endswith(suffix):name=name[:-len(suffix)];break
            if NAME.fullmatch(name):names.add(name)
    result=dict(mode=request['mode'],records=[],candidates=[],errors=[],deleted=[],more=more,cursor=cursor,cache_stamp=base_stamp)
    expected={item['name']:item for item in request['targets']}
    for name in sorted(names)[:BATCH_SIZE]:
        check()
        try:
            record=inspect(cache,name);path=owned(cache,name)
            result['records'].append({key:record[key] for key in ('name','created','stamp','lease_sha256')})
            if path in protected or any(path==ref or path in ref.parents for ref in references):continue
            if time.time()-record['created']<RETENTION_SECONDS:continue
            if name in expected and expected[name]['created']!=record['created']:raise CleanupBoundary('Lease epoch changed')
            if request['mode']=='plan':result['candidates'].append(record);continue
            if name not in expected or any(expected[name].get(key)!=record.get(key) for key in ('stamp','lease_sha256')):raise CleanupBoundary('Stale delete identity')
            with held_directory(cache):
                if stamp(cache)!=base_stamp:raise CleanupBoundary('Cache identity changed')
                save_owner(cache,record);check()
                if not record['missing']:remove_tree(path,record,check)
                check();no_links(cache)
                if path.exists():raise OSError('Cleanup root still exists')
                if side_owner(cache,name)!={key:record[key] for key in ('version','name','created','stamp','lease_sha256')}:raise CleanupBoundary('Owner changed')
                sidecar(cache,name).unlink();result['deleted'].append(name)
        except FileNotFoundError as error:
            # Unmarked user directories are not cleanup owners. A known
            # target confirmed absent can be forgotten without deleting it.
            path=owned(cache,name)
            if not path.exists() and not sidecar(cache,name).exists() and name in expected:result['deleted'].append(name)
            elif path.exists() and not (path/'.lease.json').exists() and not sidecar(cache,name).exists():continue
            else:result['errors'].append(dict(name=name,**diagnostic(error)))
        except (OSError,ValueError,RuntimeError) as error:result['errors'].append(dict(name=name,**diagnostic(error)))
    return result


def remove_tree(path,record,check):
    if stamp(path)!=record['stamp']:raise CleanupBoundary('Delete identity changed')
    def remove(folder,root=False):
        with held_directory(folder) as fd:
            entries=os.scandir(fd if fd is not None else folder)
            with entries:
                for entry in entries:
                    if root and entry.name=='.lease.json':continue
                    check();child=folder/entry.name;no_links(child);info=entry.stat(follow_symlinks=False)
                    if stat.S_ISDIR(info.st_mode):
                        remove(child);check()
                        if fd is None:child.rmdir()
                        else:os.rmdir(entry.name,dir_fd=fd)
                    elif stat.S_ISREG(info.st_mode):
                        check()
                        if fd is None:child.unlink()
                        else:os.unlink(entry.name,dir_fd=fd)
                    else:raise CleanupBoundary('Unsupported cleanup child')
            if root:
                check();marker=folder/'.lease.json'
                if marker.exists():
                    created,digest=lease(marker)
                    if created!=record['created'] or digest!=record['lease_sha256']:raise CleanupBoundary('Delete lease changed')
                    if fd is None:marker.unlink()
                    else:os.unlink('.lease.json',dir_fd=fd)
    with held_directory(path.parent):remove(path,root=True);check();path.rmdir()


def info_at(fd,name):
    info=os.stat(name,dir_fd=fd,follow_symlinks=False)
    if stat.S_ISLNK(info.st_mode) or getattr(info,'st_file_attributes',0)&0x400:raise CleanupBoundary('Reparse boundary')
    return info


def directory_at(fd,name):
    before=info_at(fd,name)
    if not stat.S_ISDIR(before.st_mode):raise CleanupBoundary('Directory required')
    previous=file_identity(before);child=os.open(name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd)
    try:
        current=os.fstat(child)
        if previous!=file_identity(current):raise CleanupBoundary('Directory changed')
        return child
    except Exception:os.close(child);raise


def json_at(fd,name):
    info=info_at(fd,name)
    if not stat.S_ISREG(info.st_mode):raise CleanupBoundary('Regular metadata required')
    source=os.open(name,os.O_RDONLY|os.O_NOFOLLOW,dir_fd=fd)
    with os.fdopen(source,'rb') as stream:
        opened=os.fstat(stream.fileno())
        if (opened.st_dev,opened.st_ino)!=(info.st_dev,info.st_ino):raise CleanupBoundary('Metadata changed')
        raw=stream.read(MAX_MESSAGE_BYTES+1)
    if len(raw)>MAX_MESSAGE_BYTES:raise CleanupBoundary('Metadata limit')
    return json.loads(raw,object_pairs_hook=unique_object),raw


def owner_at(fd,name):return checked_owner(json_at(fd,name+SIDECAR_SUFFIX)[0],name)


def lease_at(fd):
    value,raw=json_at(fd,'.lease.json')
    if (not isinstance(value,dict) or type(value.get('flickdock_clipboard'))is not int
            or value['flickdock_clipboard']!=1 or not number(value.get('created_at'))):raise CleanupBoundary('Invalid lease')
    return value['created_at'],hashlib.sha256(raw).hexdigest()


def exists_at(fd,name):
    try:info_at(fd,name);return True
    except FileNotFoundError:return False


def save_owner_at(fd,record):
    name=record['name'];final=name+SIDECAR_SUFFIX;partial=name+PARTIAL_SUFFIX
    value={key:record[key] for key in ('version','name','created','stamp','lease_sha256')}
    if exists_at(fd,final):
        if owner_at(fd,name)!=value:raise CleanupBoundary('Conflicting cleanup owner')
        if exists_at(fd,partial):
            if checked_owner(json_at(fd,partial)[0],name)!=value:raise CleanupBoundary('Unknown owner partial')
            os.unlink(partial,dir_fd=fd)
        return
    if exists_at(fd,partial):
        if checked_owner(json_at(fd,partial)[0],name)!=value:raise CleanupBoundary('Unknown owner partial')
    else:
        handle=os.open(partial,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=fd)
        with os.fdopen(handle,'w',encoding='ascii') as target:
            json.dump(value,target,separators=(',',':'));target.flush();os.fsync(target.fileno())
    try:os.link(partial,final,src_dir_fd=fd,dst_dir_fd=fd,follow_symlinks=False)
    except FileExistsError:
        if owner_at(fd,name)!=value:raise CleanupBoundary('Conflicting cleanup owner')
    except OSError as error:
        if error.errno in (errno.ENOTSUP,errno.EOPNOTSUPP,errno.ENOSYS):raise CleanupUnsupported('Atomic owner publication unsupported') from None
        raise
    os.fsync(fd);os.unlink(partial,dir_fd=fd)


def remove_at(fd,check,*,root=False):
    with os.scandir(fd) as entries:
        for entry in entries:
            name=entry.name
            if root and name=='.lease.json':continue
            check();info=info_at(fd,name)
            if stat.S_ISDIR(info.st_mode):
                child=directory_at(fd,name)
                try:remove_at(child,check)
                finally:os.close(child)
                check();current=info_at(fd,name)
                if (current.st_dev,current.st_ino)!=(info.st_dev,info.st_ino):raise CleanupBoundary('Child identity changed')
                os.rmdir(name,dir_fd=fd)
            elif stat.S_ISREG(info.st_mode):check();os.unlink(name,dir_fd=fd)
            else:raise CleanupBoundary('Unsupported cleanup child')


def delete_posix(request,check):
    cache=Path(request['cache']);no_links(cache)
    with held_directory(cache) as fd:
        info=os.fstat(fd);identity=file_identity(info)
        if identity!=request['cache_stamp']:raise CleanupBoundary('Cache identity changed')
        refs=[Path(path).resolve() for path in request['references']];protected=[Path(path).resolve() for path in request['protected']]
        result=dict(mode='delete',records=[],candidates=[],errors=[],deleted=[],more=False,cursor='',cache_stamp=identity)
        for expected in request['targets']:
            name=expected['name'];check();child=None
            try:
                path=owned(cache,name)
                if path in protected or any(path==ref or path in ref.parents for ref in refs):continue
                try:child=directory_at(fd,name)
                except FileNotFoundError:record=dict(owner_at(fd,name),missing=True)
                else:
                    current=os.fstat(child);identity=file_identity(current)
                    try:created,digest=lease_at(child)
                    except FileNotFoundError:
                        record=dict(owner_at(fd,name),missing=False)
                        if record['stamp']!=identity:raise CleanupBoundary('Cleanup owner changed')
                    else:
                        record=dict(version=VERSION,name=name,created=created,stamp=identity,lease_sha256=digest,missing=False)
                        if exists_at(fd,name+SIDECAR_SUFFIX) and owner_at(fd,name)!={k:v for k,v in record.items() if k!='missing'}:raise CleanupBoundary('Conflicting cleanup owner')
                result['records'].append({key:record[key] for key in ('name','created','stamp','lease_sha256')})
                if any(record[key]!=expected[key] for key in ('created','stamp','lease_sha256')):raise CleanupBoundary('Delete identity changed')
                if time.time()-record['created']<RETENTION_SECONDS:continue
                save_owner_at(fd,record);check()
                if child is not None:
                    remove_at(child,check,root=True);check()
                    if exists_at(child,'.lease.json'):
                        if lease_at(child)!=(record['created'],record['lease_sha256']):raise CleanupBoundary('Lease changed')
                        os.unlink('.lease.json',dir_fd=child)
                    current=info_at(fd,name)
                    if [current.st_dev,current.st_ino]!=record['stamp']:raise CleanupBoundary('Directory name changed')
                    os.close(child);child=None;check();os.rmdir(name,dir_fd=fd)
                if exists_at(fd,name):raise CleanupBoundary('Cleanup root still exists')
                if owner_at(fd,name)!={k:record[k] for k in ('version','name','created','stamp','lease_sha256')}:raise CleanupBoundary('Owner changed')
                check();os.unlink(name+SIDECAR_SUFFIX,dir_fd=fd);os.fsync(fd);result['deleted'].append(name)
            except (OSError,ValueError,RuntimeError) as error:result['errors'].append(dict(name=name,**diagnostic(error)))
            finally:
                if child is not None:os.close(child)
        return result


def parse_arguments(args):
    keys={'--request','--token','--parent-pid','--deadline-ms'}
    if len(args)!=8 or set(args[::2])!=keys:raise CleanupBoundary('Invalid cleanup arguments')
    values=dict(zip(args[::2],args[1::2]));token=values['--token']
    if not TOKEN.fullmatch(token) or not values['--parent-pid'].isdigit() or not values['--deadline-ms'].isdigit():raise CleanupBoundary('Invalid cleanup arguments')
    parent=int(values['--parent-pid']);deadline=int(values['--deadline-ms']);path=Path(values['--request'])
    if (parent<=0 or not 100<=deadline<=10000 or not path.is_absolute() or path.name!='request.json'
            or path.parent.name!=token or path.parent.parent.name!='FlickDock-cleanup-v1'):raise CleanupBoundary('Invalid cleanup capability')
    return path,token,parent,deadline


def validate_capability(path,token):
    if path!=Path(tempfile.gettempdir()).resolve(strict=True)/'FlickDock-cleanup-v1'/token/'request.json':raise CleanupBoundary('Invalid cleanup capability')


def write_posix(raw):
    offset=0
    while offset<len(raw):
        count=os.write(1,raw[offset:offset+8192])
        if not 0<count<=min(8192,len(raw)-offset):raise OSError('Output pipe closed')
        offset+=count


def write_response(raw):
    if os.name=='nt':
        from win_clipboard_native import write_stdout
        write_stdout([raw])
    else:write_posix(raw)


def main(args=None):
    try:path,token,parent,deadline=parse_arguments(list(sys.argv[1:] if args is None else args))
    except (OSError,ValueError,TypeError):return 2
    # POSIX kernel timer still terminates a blocked filesystem operation if
    # the parent crashes. Parent also owns a real subprocess deadline/reap.
    if os.name!='nt':signal.signal(signal.SIGALRM,signal.SIG_DFL);signal.setitimer(signal.ITIMER_REAL,deadline/1000)
    expires=time.monotonic()+deadline/1000
    permit=None;expected_permit=None
    def check():
        if os.getppid()!=parent or time.monotonic()>=expires:raise CleanupStale('Cleanup authority expired')
        if permit is not None:
            no_links(permit)
            with permit.open('rb') as source:raw=source.read(128)
            if raw!=expected_permit:raise CleanupStale('Cleanup authority revoked')
    try:
        try:
            check();validate_capability(path,token);value,_=read_json(path);request=validate_request(value,token)
            pending=path.parent/'permit'
            while not pending.exists():check();time.sleep(.01)
            permit=pending;expected_permit=(token+':'+request['mode']).encode('ascii');check()
            result=execute(request,check=check)
            result.update(version=VERSION,token=token,status='ready')
        except (OSError,ValueError,RuntimeError) as error:result=dict(version=VERSION,token=token,status='failed',**diagnostic(error))
        raw=json.dumps(result,separators=(',',':')).encode('ascii')
        if len(raw)>MAX_MESSAGE_BYTES:return 3
        try:write_response(raw);return 0
        except OSError:return 3
    finally:
        if os.name!='nt':signal.setitimer(signal.ITIMER_REAL,0)


if __name__=='__main__':raise SystemExit(main())
