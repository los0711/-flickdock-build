"""Separate network transmission time from cloud confirmation on native Qt."""
import base64,hashlib,json,os,sys,tempfile,time
from pathlib import Path
from uuid import uuid4

def main():
    if os.environ.get('GITHUB_ACTIONS')!='true' or os.environ.get('GITHUB_REPOSITORY')!='los0711/-flickdock-build':raise RuntimeError('Disposable authorized CI only')
    secret=json.loads(os.environ.pop('FLICKDOCK_QA_SYNC_JSON'))
    return profile(secret,run=os.environ['GITHUB_RUN_ID'],attempt=os.environ['GITHUB_RUN_ATTEMPT'],
        platform=os.environ['RUNNER_OS'],commit=os.environ['GITHUB_SHA'],root_base=Path(os.environ['RUNNER_TEMP']),
        output=Path(os.environ['RUNNER_TEMP'])/'flickdock-upload-profile.json')


def profile(secret,*,run,attempt,platform,commit,root_base,output,proxy=None):
    """Synthetic account and R2 objects only; never reads the system clipboard."""
    os.environ['QT_QPA_PLATFORM']='offscreen'
    from PySide6.QtWidgets import QApplication
    from account_service import AccountService
    import cloud_api
    app=QApplication([])
    from ci_vpn import verify_qt_proxy
    if proxy is None:proxy=verify_qt_proxy()
    name='cross-qa-'+hashlib.sha256((secret['username']+run+attempt+platform+'upload-profile').encode()).hexdigest()[:16]
    report=dict(ok=False,platform=platform,source_commit=commit,cases=[],downloads=[],request_failures=[],native_ui=False,proxy=proxy)
    def until(predicate,seconds=180):
        deadline=time.monotonic()+seconds
        while time.monotonic()<deadline:
            app.processEvents()
            if predicate():return
            time.sleep(.002)
        raise TimeoutError('Upload profile timed out')
    class Store:
        def load(self):return None
        def save(self,value):pass
        def clear(self):pass
    with tempfile.TemporaryDirectory(prefix='flickdock-upload-',dir=root_base) as directory:
        root=Path(directory);account=AccountService(root,store=Store());account.device_name='Disposable upload profiler'
        transport=account.api.request
        def traced(method,path,callback,**options):
            def done(value,error):
                if error:report['request_failures'].append(dict(method=method,route=path.split('/')[1:3],status=getattr(error,'status',0),code=getattr(error,'code','')))
                callback(value,error)
            return transport(method,path,done,**options)
        account.api.request=traced
        def call(method,path,**options):
            result=[];account.api.request(method,path,lambda value,error:result.append((value,error)),**options)
            until(lambda:bool(result))
            value,error=result[0]
            if error:
                report['request_failures'].append(dict(method=method,route=path.split('/')[1:3],status=getattr(error,'status',0),code=getattr(error,'code','')))
                raise RuntimeError('Profile cloud operation failed')
            return value
        active_upload=None;active_item=None;committed=False;clipboard_used=False
        def clear_clipboard():
            from clipboard_sync import package
            folder=root/'cleanup';folder.mkdir(exist_ok=True)
            item=package(('text',{'text':'Disposable network benchmark finished'}),folder)
            blob=Path(item['path']).read_bytes()
            value=call('POST','/v1/clipboard',data=dict(id=str(uuid4()),protocol=2,sequence=2,copied_at=round(time.time()*1000),
                inline=dict(kind='text',body=base64.b64encode(blob).decode(),size=len(blob),sha256=item['sha256'])))
            assert value['clipboard']['ready']
        try:
            account.register(name,secret['password']);until(lambda:account.signed_in or not account.busy,60)
            if not account.signed_in:raise RuntimeError('Profile authentication failed')
            for mode in ('native_qfile_high','upload_slice_high','upload_slice_normal'):
                body=hashlib.shake_256((run+platform+mode).encode()).digest(8*1024*1024)
                path=root/'snapshot.bin';path.write_bytes(body);digest=hashlib.sha256(body).hexdigest()
                active_item=str(uuid4());active_upload=str(uuid4());committed=False
                reserved_at=time.perf_counter()
                initialized=call('POST','/v1/uploads',data=dict(upload_id=active_upload,item_id=active_item,base_version=0,kind='file',
                    name='Synthetic-upload-profiler.bin',mime_type='application/octet-stream',size=len(body),sha256=digest,fast_upload=True,fast_multipart=True))
                reserve_seconds=time.perf_counter()-reserved_at
                assert initialized['part_count']==1 and initialized['chunk_size']==len(body)
                progress=[];started=time.perf_counter()
                def sent(count,total):progress.append(dict(bytes=count,seconds=time.perf_counter()-started))
                options=dict(source=path,headers={'X-Content-SHA256':digest},progress=sent,priority='normal' if mode.endswith('normal') else 'high')
                if mode!='native_qfile_high':options.update(source_offset=0,source_size=len(body))
                call('PUT','/v1/uploads/'+active_upload+'/parts/0',**options)
                elapsed=time.perf_counter()-started
                first=next((x['seconds'] for x in progress if x['bytes']>0),None)
                complete=next((x['seconds'] for x in progress if x['bytes']==len(body)),None)
                committed_at=time.perf_counter()
                result=call('POST','/v1/uploads/'+active_upload+'/commit',data={});committed=True
                commit_seconds=time.perf_counter()-committed_at
                assert result['record']['sha256']==digest
                report['cases'].append(dict(mode=mode,bytes=len(body),request_seconds=round(elapsed,4),first_progress_seconds=first,
                    reserve_seconds=round(reserve_seconds,4),commit_seconds=round(commit_seconds,4),pipeline_seconds=round(reserve_seconds+elapsed+commit_seconds,4),
                    cloud_ready_after_progress_seconds=None if complete is None else round(elapsed-complete+commit_seconds,4),
                    full_upload_progress_seconds=complete,confirmation_after_progress_seconds=None if complete is None else round(elapsed-complete,4),
                    mib_per_second=round(len(body)/1024/1024/max(.001,elapsed),3)))
                if mode=='upload_slice_high':
                    download_started=time.perf_counter()
                    call('GET',f'/v1/items/{active_item}/parts/0?version={result["record"]["version"]}',headers={'X-FlickDock-Fast-Content':'1'},
                        destination=root/'shelf-download.bin',expected={'size':len(body),'sha256':digest},priority='high')
                    report['downloads'].append(dict(mode='shelf_fast_content',bytes=len(body),seconds=round(time.perf_counter()-download_started,4),sha256_verified=True))
                call('DELETE','/v1/items/'+active_item,data={'base_version':result['record']['version']});active_upload=None;active_item=None
                print(json.dumps(dict(platform=platform,case=mode,request_seconds=round(elapsed,3),full_upload_progress_seconds=complete)),flush=True)
            from clipboard_sync import package
            folder=root/'clipboard-package';folder.mkdir();item=package(('files',[str(path)]),folder);raw=item['raw_file']
            clipboard_id=str(uuid4())
            call('POST','/v1/clipboard',data=dict(id=clipboard_id,protocol=2,sequence=1,copied_at=round(time.time()*1000)));clipboard_used=True
            headers={'X-Clipboard-Kind':'files','X-Content-SHA256':item['sha256'],
                'X-Clipboard-Raw-File':json.dumps({key:raw[key] for key in ('name','size','sha256','prefix','suffix')},separators=(',',':'))}
            progress=[];started=time.perf_counter()
            def clipboard_sent(count,total):progress.append(dict(bytes=count,seconds=time.perf_counter()-started))
            uploaded=call('PUT',f'/v1/clipboard/{clipboard_id}/content',source=Path(item['path']),source_offset=raw['offset'],source_size=raw['size'],
                headers=headers,priority='high',progress=clipboard_sent,timeout_ms=300000)
            elapsed=time.perf_counter()-started;assert uploaded['clipboard']['ready'] and uploaded['clipboard']['sha256']==item['sha256']
            complete=next((value['seconds'] for value in progress if value['bytes']==raw['size']),None)
            report['cases'].append(dict(mode='clipboard_raw_file_high',bytes=raw['size'],request_seconds=round(elapsed,4),
                full_upload_progress_seconds=complete,confirmation_after_progress_seconds=None if complete is None else round(elapsed-complete,4),
                mib_per_second=round(raw['size']/1024/1024/max(.001,elapsed),3)))
            download_started=time.perf_counter()
            call('GET',f'/v1/clipboard/{clipboard_id}/content',destination=root/'clipboard-download.bin',expected={'size':item['size'],'sha256':item['sha256']},priority='high')
            report['downloads'].append(dict(mode='clipboard_content',bytes=item['size'],seconds=round(time.perf_counter()-download_started,4),sha256_verified=True))
            print(json.dumps(dict(platform=platform,case='clipboard_raw_file_high',request_seconds=round(elapsed,3),full_upload_progress_seconds=complete)),flush=True)
            clear_clipboard();clipboard_used=False
            report['ok']=True;call('POST','/v1/auth/logout',data={});return 0
        except Exception as error:report['error_type']=type(error).__name__;return 1
        finally:
            if clipboard_used:
                try:clear_clipboard()
                except Exception:report['cleanup_incomplete']=True
            if active_upload and not committed:
                try:call('DELETE','/v1/uploads/'+active_upload)
                except Exception:report['cleanup_incomplete']=True
            if active_item and committed:
                try:call('DELETE','/v1/items/'+active_item,data={'base_version':1})
                except Exception:report['cleanup_incomplete']=True
            account.close();output.write_text(json.dumps(report,indent=2),encoding='utf8')

if __name__=='__main__':raise SystemExit(main())
