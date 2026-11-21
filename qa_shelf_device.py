"""Real file transfer between separate OS instances and a disposable Worker."""
import argparse
from copy import deepcopy
import json
import os
from pathlib import Path
import platform
import sys
from time import monotonic,sleep

parser=argparse.ArgumentParser()
parser.add_argument('--source',type=Path,required=True)
parser.add_argument('--root',type=Path,required=True)
parser.add_argument('--origin',required=True)
parser.add_argument('--scenario',required=True)
parser.add_argument('--role',choices=('sender','receiver'),required=True)
args=parser.parse_args()
sys.path.insert(0,str(args.source.resolve()))
os.environ['QT_QPA_PLATFORM']='offscreen'
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication
from account_service import AccountService
from cloud_api import CloudApi
from shelf_direct import ShelfDirectDelivery
from shelf_sync import ShelfSync
from wan_transfer import WanTransferService

class Store:
    def load(self):return None
    def save(self,value):pass
    def clear(self):pass

class Adapter:
    def __init__(self):self.view={'permanent':[],'texts':{},'metadata':{}}
    def snapshot(self):return deepcopy(self.view)
    def restore(self,data):self.view=data
    def title(self,ref):return Path(ref).name

app=QApplication([])
root=args.root.resolve()
root.mkdir(parents=True,exist_ok=True)
sender=args.role=='sender'
view=Adapter()
account=AccountService(root,origin=args.origin,store=Store(),api=CloudApi(args.origin,allow_test_http=True))
account.device_name=args.role
wan=WanTransferService(root,device_name=args.role,origin=args.origin,allow_test_origin=True)
wan.attach_account(account)
shelf=ShelfSync(root,account,view)
direct=ShelfDirectDelivery(shelf,wan)
shelf.direct_delivery=direct
offers=[];progress=[];completed=[];status=[];requests=[];devices={}
wan.offer_received.connect(lambda offer:(offers.append(bool(offer.get('shelf_intake'))),direct.offer(offer)))
wan.progress.connect(lambda row:progress.append(dict(state=row['state'],route=row.get('route'),received=row.get('received'),message=row.get('message'))))
wan.completed.connect(lambda row:completed.append(row['direction']))
wan.status_changed.connect(lambda text:status.append(text) if text else None)
account.status_changed.connect(lambda text:status.append(text) if text else None)
api=account.api.request

def request(method,path,complete,**kwargs):
    if '/content' in path or '/parts/' in path:
        requests.append((method,path))
    if sender and method=='POST' and path.endswith('/commit') and not any(name.startswith('receiver-direct') for name in devices.values()):
        def later():
            if any(name.startswith('receiver-direct') for name in devices.values()):api(method,path,complete,**kwargs)
            else:QTimer.singleShot(120,later)
        QTimer.singleShot(120,later);return None
    return api(method,path,complete,**kwargs)
account.api.request=request

def poll_devices():
    if account.signed_in:
        def done(value,error):
            if not error:devices.update({device['id']:device['name'] for device in value['devices']})
        api('GET','/v1/devices',done,priority='high')

def until(phase,ready,seconds=90,refresh=False):
    deadline=monotonic()+seconds
    next_poll=0
    while monotonic()<deadline:
        app.processEvents()
        if ready():return
        if monotonic()>=next_poll:
            poll_devices()
            if refresh:shelf.request_refresh()
            next_poll=monotonic()+.8
        sleep(.008)
    raise RuntimeError('Timed out at '+phase)

def announce(state):
    result=[]
    api('PATCH','/v1/devices/'+account.device_id,lambda data,error:result.append(error),
        data={'name':args.role+'-'+state},priority='high')
    until('publish '+state,lambda:result)
    if result[0]:raise RuntimeError('Failed to publish fixture stage '+state)

def seen(state):
    return state in devices.values()

def peer_online(wanted):
    return any(peer.get('account_device') is True and peer.get('online') is wanted
               and 'shelf-drop-v1' in peer.get('features',()) for peer in wan.peers())

def data(name):
    return (b'FlickDock real two-device test data '+name.encode()+b'//')*44179

def get(name):
    return next((Path(ref) for ref in view.view['permanent'] if Path(ref).name==name),None)

def drop(name):
    path=root/name;path.write_bytes(data(name));ref=str(path)
    direct.mark_drop([ref]);view.view['permanent'].append(ref);shelf.local_changed()
    return ref

report=dict(ok=False,role=args.role,os=sys.platform,architecture=platform.machine())
try:
    username='shelf-device-qa-'+args.scenario
    password='FlickDock disposable QA '+args.scenario
    password=password.replace(' ','-')
    (account.register if sender else account.login)(username,password)
    until('test account sign in',lambda:account.signed_in)
    if sender:
        (root/'ready').touch()
    wan.start()
    until('both online in the same test account',lambda:wan._bound_identity is not None and peer_online(True),360)
    if sender:
        first=drop('01-online.bin')
        until('online file committed to cloud',lambda:shelf.sync_status_for_ref(first)=='synced',130)
        announce('online-committed')
        until('other device offline',lambda:seen('receiver-offline') and any(
            p.get('account_device') is True and p.get('online') is False for p in wan.peers()),105)
        # The online phase has already passed, so the next upload can commit.
        devices['fixture-online-done']='receiver-direct-complete'
        second=drop('02-offline.bin')
        until('offline cloud commit',lambda:shelf.sync_status_for_ref(second)=='synced')
        announce('offline-committed')
        until('the offline device has synced',lambda:seen('receiver-done'),105)
    else:
        until('direct payload appears on shelf',lambda:get('01-online.bin') is not None,105,refresh=True)
        assert get('01-online.bin').read_bytes()==data('01-online.bin')
        assert completed==['receive'],completed
        assert offers==[True],offers
        assert not any(method=='GET' for method,_ in requests),requests
        assert any(entry.get('direct_pending') for entry in shelf.state['entries'].values())
        announce('direct-delivered')
        until('ordinary cloud upload commits',lambda:seen('sender-online-committed'),refresh=True)
        until('same shelf identity confirmed',lambda:not any(e.get('direct_pending') for e in shelf.state['entries'].values()),refresh=True)
        assert len(view.view['permanent'])==1
        shelf.set_directions(False,False)
        wan.close()
        announce('offline')
        until('file added while offline',lambda:seen('sender-offline-committed'))
        assert not get('02-offline.bin')
        shelf.set_directions(True,True)
        until('cloud file after reconnection',lambda:get('02-offline.bin') is not None,refresh=True)
        assert get('02-offline.bin').read_bytes()==data('02-offline.bin')
        assert len(view.view['permanent'])==2
        report.update(online_payload_bytes=len(data('01-online.bin')),online_cloud_payload_requests=0,offline_cloud_payload_requests=sum(
            method=='GET' for method,_ in requests))
        assert report['offline_cloud_payload_requests']>0
        announce('done')
    report['ok']=True
except Exception as error:
    report['error']=repr(error)
finally:
    report.update(completed=completed,offers=offers,progress=progress[-30:],status=status[-6:])
    direct.close()
    if not wan._closing:wan.close()
    shelf.close()
    account.close()
    (root/'evidence.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False,indent=2))
if not report['ok']:raise SystemExit(1)
