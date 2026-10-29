"""Small private diagnostics: stable phases/codes only, never payload or names."""
import json,os,re,sys,threading,time
from pathlib import Path
from uuid import uuid4
from app_version import APP_VERSION

_lock=threading.Lock()
_last={}


def record_event(base,component,phase,code,status=0):
    if base is None or component not in ('cloud','update','remote'):return
    if not all(isinstance(value,str) and re.fullmatch(r'[A-Z0-9_]{1,80}',value.upper()) for value in (phase,code)):return
    key=(str(base),component,phase,code,status);now=time.monotonic()
    with _lock:
        if now-_last.get(key,-1000)<30:return
        _last[key]=now
        if len(_last)>100:_last.pop(next(iter(_last)))
        root=Path(base);path=root/'network-diagnostics.json';temporary=None
        try:
            if root.is_symlink() or path.is_symlink():return
            root.mkdir(parents=True,exist_ok=True);rows=[]
            if path.is_file() and path.stat().st_size<=65536:
                try:
                    existing=json.loads(path.read_text(encoding='utf8'))
                    if isinstance(existing,list):rows=existing[-63:]
                except (OSError,ValueError):pass
            rows.append(dict(at=round(time.time()),component=component,phase=phase.upper(),code=code.upper(),
                             status=int(status),platform=sys.platform,version=APP_VERSION))
            temporary=root/('.network-diagnostics-'+uuid4().hex+'.tmp')
            descriptor=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
            with os.fdopen(descriptor,'w',encoding='utf8') as stream:json.dump(rows,stream)
            os.replace(temporary,path)
        except (OSError,TypeError,ValueError):pass
        finally:
            if temporary is not None:
                try:temporary.unlink(missing_ok=True)
                except OSError:pass
