"""Exercise the production shelf drop route from two isolated native runners."""
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time

from PySide6.QtWidgets import QApplication

from account_service import AccountService
from cloud_events import CloudEvents
from shelf_direct import ShelfDirectDelivery
from shelf_sync import ShelfSync
from sync_scheduler import SyncScheduler
from wan_transfer import WanTransferService


class MemorySessions:
    def load(self):return None
    def save(self,value):return None
    def clear(self):return None
    def invalidate(self):return None


class ShelfState:
    def __init__(self):
        self.view={'permanent':[],'texts':{},'metadata':{}}

    def snapshot(self):return copy.deepcopy(self.view)
    def restore(self,value):self.view=value
    def title(self,ref):return Path(ref).name


def run(root,side,credentials):
    app=QApplication([])
    app.setQuitOnLastWindowClosed(False)
    account=AccountService(root/'account',store=MemorySessions())
    account.device_name='FlickDock-shelf-QA-'+side
    adapter=ShelfState()
    scheduler=SyncScheduler(app,connected=True)
    shelf=ShelfSync(root/'shelf',account,adapter,scheduler=scheduler)
    events=CloudEvents(account,app)
    events.content_changed.connect(shelf.request_refresh)
    wan=WanTransferService(root/'remote',device_name=account.device_name)
    direct=ShelfDirectDelivery(shelf,wan)
    shelf.direct_delivery=direct
    wan.attach_account(account)
    wan.offer_received.connect(direct.offer)
    wan.start()
    downloads=[];offers=[];completed=[];errors=[]
    def reported(offer):
        if 'shelf_intake' in offer:offers.append(offer['shelf_intake']['id'])
    wan.offer_received.connect(reported)
    wan.completed.connect(completed.append)
    sys.excepthook=lambda kind,value,trace:errors.append(kind.__name__)
    original_download=shelf._download_record
    def download(record,*args,**kwargs):
        downloads.append(record['name'])
        return original_download(record,*args,**kwargs)
    shelf._download_record=download
    other='B' if side=='A' else 'A'
    mine=f'FlickDock-direct-{side}.bin';theirs=f'FlickDock-direct-{other}.bin'
    report=dict(ok=False,side=side,platform=sys.platform,phase='account')

    def wait_for(condition,limit=65):
        deadline=time.monotonic()+limit
        while time.monotonic()<deadline:
            app.processEvents()
            if condition():return
            time.sleep(.008)
        raise TimeoutError(report['phase'])

    def call(method,path,**kwargs):
        result=[]
        account.api.request(method,path,lambda reply,error:result.append((reply,error)),**kwargs)
        wait_for(lambda:bool(result),12)
        reply,error=result[0]
        if error:raise RuntimeError(getattr(error,'code','api'))
        return reply

    def fixture(which):
        seed=(os.environ['GITHUB_RUN_ID']+'-shelf-'+which).encode()
        return hashlib.shake_256(seed).digest(4*1024*1024)

    def entry(name):
        return next((row for row in shelf.state['entries'].values()
                     if row['name']==name and not row.get('deleted')),None) if shelf.state else None

    def name_device(status):
        call('PATCH','/v1/devices/'+account.device_id,data={'name':'FlickDock-shelf-QA-'+side+'-'+status})

    def wait_peer_status(peer_id,status):
        next_check=0.
        def matching():
            nonlocal next_check
            if time.monotonic()<next_check:return False
            next_check=time.monotonic()+.75
            return any(item['id']==peer_id and item['name']=='FlickDock-shelf-QA-'+other+'-'+status
                       for item in call('GET','/v1/devices')['devices'])
        wait_for(matching,40)

    def drop(name,which):
        source=root/name;source.write_bytes(fixture(which))
        direct.mark_drop([str(source)])
        adapter.view['permanent'].append(str(source))
        shelf.local_changed()
        shelf.request_refresh()

    def matches(row,which):
        return hashlib.sha256(Path(row['ref']).read_bytes()).digest()==hashlib.sha256(fixture(which)).digest()

    try:
        account.login(credentials['username'],credentials['password'])
        wait_for(lambda:account.signed_in or not account.busy,35)
        assert account.signed_in
        report['phase']='peer_ready'
        def peer():
            return next((item for item in wan.peers() if item.get('account_device') is True
                         and item.get('online') and 'shelf-drop-v1' in item.get('features',())
                         and item.get('name')=='FlickDock-shelf-QA-'+other),None)
        wait_for(lambda:wan._shelf_drop_supported and wan._bound_identity==wan._account_identity()
                 and peer() is not None and not shelf.catching_up)
        peer_id=peer()['account_device_id']
        report['phase']='drop'
        drop(mine,side)
        report['phase']='transfer'
        def saved():
            local,remote=entry(mine),entry(theirs)
            return (local and remote and local['version']>=1 and not local['dirty']
                    and remote['version']>=1 and not remote.get('direct_pending'))
        wait_for(saved,130)
        local,remote=entry(mine),entry(theirs)
        report.update(verified_bytes=Path(remote['ref']).stat().st_size,
                      automatic_offer=remote['id'] in offers,
                      authenticated_transfer_completed=any(
                          item.get('direction')=='receive' and item.get('paths') for item in completed),
                      cloud_backup=local['version']>=1,
                      redundant_cloud_downloads=downloads.count(theirs),
                      matching_sha256=matches(remote,other))
        report['phase']='offline_cloud_fallback'
        offline_name='FlickDock-direct-offline.bin'
        if side=='A':
            wait_peer_status(peer_id,'offline')
            wait_for(lambda:not any(item.get('account_device_id')==peer_id and item.get('online')
                                    for item in wan.peers()),25)
            drop(offline_name,'offline')
            wait_for(lambda:entry(offline_name) and entry(offline_name)['version']==1,65)
            name_device('backed-up')
            wait_peer_status(peer_id,'restored')
            report['offline_backup']=True
            # Remove just the known synthetic fixture objects from the QA account.
            for row in (local,remote,entry(offline_name)):shelf.delete_cloud_copy(row['ref'])
            wait_for(lambda:not shelf.cloud_copy_operations,15)
        else:
            shelf.set_directions(False,False)
            wan.set_signaling_allowed(False)
            name_device('offline')
            wait_peer_status(peer_id,'backed-up')
            assert entry(offline_name) is None
            shelf.set_directions(True,True)
            wan.set_signaling_allowed(True);wan.start()
            shelf.request_refresh()
            wait_for(lambda:entry(offline_name),55)
            restored=entry(offline_name)
            report['offline_fallback']=matches(restored,'offline') and downloads.count(offline_name)==1
            assert report['offline_fallback']
            name_device('restored')
            # Keep the cloud session valid while the sender reads the
            # completion and retires the three known test objects.
            until=time.monotonic()+6
            wait_for(lambda:time.monotonic()>=until,8)
        report['ok']=all(report[key] for key in ('automatic_offer','authenticated_transfer_completed',
                        'cloud_backup','matching_sha256')) and not report['redundant_cloud_downloads'] and not errors
        report['phase']='complete'
        return 0 if report['ok'] else 1
    except (AssertionError,RuntimeError,TimeoutError,KeyError,OSError) as error:
        report['error_type']=type(error).__name__
        report['received_offer']=bool(offers)
        report['shelf_drop_v1']=wan._shelf_drop_supported
        return 1
    finally:
        if account.signed_in:
            account.logout()
            try:wait_for(lambda:not account.busy,12)
            except TimeoutError:report['logout_unconfirmed']=True
        direct.close();shelf.close();wan.close();events.close();scheduler.close();account.close()
        report['callback_errors']=errors
        (root/'shelf-direct-report.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
        print(json.dumps(report),flush=True)


if __name__=='__main__':
    assert os.environ.get('GITHUB_ACTIONS')=='true' and os.environ.get('RUNNER_ENVIRONMENT')=='github-hosted'
    assert os.environ.get('GITHUB_REPOSITORY')=='los0711/-flickdock-build'
    parser=argparse.ArgumentParser()
    parser.add_argument('side',choices=('A','B'))
    parser.add_argument('root',type=Path)
    args=parser.parse_args()
    root=args.root.resolve()
    assert root.is_relative_to(Path(os.environ['RUNNER_TEMP']).resolve())
    root.mkdir(parents=True,exist_ok=True)
    credentials=json.loads(os.environ.pop('FLICKDOCK_SHELF_QA'))
    assert re.fullmatch('shelf-qa-[a-f0-9]{16}',credentials['username'])
    raise SystemExit(run(root,args.side,credentials))
