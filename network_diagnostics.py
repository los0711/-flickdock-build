"""Small private diagnostics: stable phases/codes only, never payload or names."""
import json,os,re,sys,threading,time
from pathlib import Path
from uuid import uuid4
from app_version import APP_VERSION,APP_BUILD

_lock=threading.Lock()
_last={}


def record_event(base,component,phase,code,status=0,details=None):
    if base is None or component not in ('cloud','update','remote'):return
    if not all(isinstance(value,str) and re.fullmatch(r'[A-Z0-9_]{1,80}',value.upper()) for value in (phase,code)):return
    trace=details.get('trace','') if isinstance(details,dict) else ''
    if not isinstance(trace,str) or not re.fullmatch('[a-f0-9]{12}',trace):trace=''
    key=(str(base),component,phase,code,status,trace);now=time.monotonic()
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
                             status=int(status),platform=sys.platform,version=APP_VERSION,build=APP_BUILD))
            if isinstance(details,dict):
                safe={}
                for name in ('received_size','expected_size','network_error','errno','local_candidates','remote_candidates','early_candidates',
                    'generation','rtt_ms','wire_sent_bytes_per_second','wire_received_bytes_per_second','delivered_bytes_per_second','buffered_bytes','unacked_bytes',
                    'peer_requests','binding_responses','check_success','check_failed','role_conflict','credentials_failed',
                    'peer_reflexive','send_failed','server_failed','local_family','remote_family'):
                    value=details.get(name)
                    if type(value) is int and 0<=value<=2**53:safe[name]=value
                for name in ('hash_matches','reply_finished','header_digest_valid','clipboard_priority'):
                    if type(details.get(name)) is bool:safe[name]=details[name]
                for name in ('phase','cause','location','response_length','pc_state','ice_state','gathering_state'):
                    value=details.get(name)
                    if isinstance(value,str) and re.fullmatch(r'[A-Za-z0-9_.:-]{0,90}',value):safe[name]=value
                if trace:safe['trace']=trace
                for name in ('transport','local_type','remote_type','relay_transport','library_version'):
                    value=details.get(name)
                    if isinstance(value,str) and re.fullmatch(r'[A-Za-z0-9_.-]{1,24}',value):safe[name]=value
                if safe:rows[-1]['details']=safe
            temporary=root/('.network-diagnostics-'+uuid4().hex+'.tmp')
            descriptor=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
            with os.fdopen(descriptor,'w',encoding='utf8') as stream:json.dump(rows,stream)
            os.replace(temporary,path)
        except (OSError,TypeError,ValueError):pass
        finally:
            if temporary is not None:
                try:temporary.unlink(missing_ok=True)
                except OSError:pass
