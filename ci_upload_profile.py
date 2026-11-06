"""Separate network transmission time from cloud confirmation on native Qt."""
import hashlib,json,os,sys,tempfile,time
from pathlib import Path
from uuid import uuid4

def main():
    if os.environ.get('GITHUB_ACTIONS')!='true' or os.environ.get('GITHUB_REPOSITORY')!='los0711/-flickdock-build':raise RuntimeError('Disposable authorized CI only')
    os.environ['QT_QPA_PLATFORM']='offscreen'
    from PySide6.QtWidgets import QApplication
    from account_service import AccountService
    import cloud_api
    app=QApplication([]);secret=json.loads(os.environ.pop('FLICKDOCK_QA_SYNC_JSON'))
    from ci_vpn import verify_qt_proxy
    proxy=verify_qt_proxy()
    run=os.environ['GITHUB_RUN_ID'];attempt=os.environ['GITHUB_RUN_ATTEMPT'];platform=os.environ['RUNNER_OS']
    name='cross-qa-'+hashlib.sha256((secret['username']+run+attempt+platform+'upload-profile').encode()).hexdigest()[:16]
    report=dict(ok=False,platform=platform,source_commit=os.environ['GITHUB_SHA'],cases=[],request_failures=[],native_ui=False,proxy=proxy)
    output=Path(os.environ['RUNNER_TEMP'])/'flickdock-upload-profile.json'
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
    with tempfile.TemporaryDirectory(prefix='flickdock-upload-',dir=os.environ['RUNNER_TEMP']) as directory:
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
        active_upload=None;active_item=None;committed=False
        try:
            account.register(name,secret['password']);until(lambda:account.signed_in or not account.busy,60)
            if not account.signed_in:raise RuntimeError('Profile authentication failed')
            for mode in ('native_qfile_high','upload_slice_high','upload_slice_normal'):
                body=hashlib.shake_256((run+platform+mode).encode()).digest(8*1024*1024)
                path=root/'snapshot.bin';path.write_bytes(body);digest=hashlib.sha256(body).hexdigest()
                active_item=str(uuid4());active_upload=str(uuid4());committed=False
                initialized=call('POST','/v1/uploads',data=dict(upload_id=active_upload,item_id=active_item,base_version=0,kind='file',
                    name='Synthetic-upload-profiler.bin',mime_type='application/octet-stream',size=len(body),sha256=digest,fast_upload=True,fast_multipart=True))
                assert initialized['part_count']==1 and initialized['chunk_size']==len(body)
                progress=[];started=time.perf_counter()
                def sent(count,total):progress.append(dict(bytes=count,seconds=time.perf_counter()-started))
                options=dict(source=path,headers={'X-Content-SHA256':digest},progress=sent,priority='normal' if mode.endswith('normal') else 'high')
                if mode!='native_qfile_high':options.update(source_offset=0,source_size=len(body))
                call('PUT','/v1/uploads/'+active_upload+'/parts/0',**options)
                elapsed=time.perf_counter()-started
                first=next((x['seconds'] for x in progress if x['bytes']>0),None)
                complete=next((x['seconds'] for x in progress if x['bytes']==len(body)),None)
                result=call('POST','/v1/uploads/'+active_upload+'/commit',data={});committed=True
                assert result['record']['sha256']==digest
                report['cases'].append(dict(mode=mode,bytes=len(body),request_seconds=round(elapsed,4),first_progress_seconds=first,
                    full_upload_progress_seconds=complete,confirmation_after_progress_seconds=None if complete is None else round(elapsed-complete,4),
                    mib_per_second=round(len(body)/1024/1024/max(.001,elapsed),3)))
                call('DELETE','/v1/items/'+active_item,data={'base_version':result['record']['version']});active_upload=None;active_item=None
                print(json.dumps(dict(platform=platform,case=mode,request_seconds=round(elapsed,3),full_upload_progress_seconds=complete)),flush=True)
            report['ok']=True;call('POST','/v1/auth/logout',data={});return 0
        except Exception as error:report['error_type']=type(error).__name__;return 1
        finally:
            if active_upload and not committed:
                try:call('DELETE','/v1/uploads/'+active_upload)
                except Exception:report['cleanup_incomplete']=True
            if active_item and committed:
                try:call('DELETE','/v1/items/'+active_item,data={'base_version':1})
                except Exception:report['cleanup_incomplete']=True
            account.close();output.write_text(json.dumps(report,indent=2),encoding='utf8')

if __name__=='__main__':raise SystemExit(main())
