"""Live disposable two-device notification probe; no local shelf or UI."""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import secrets
import time
from uuid import uuid4

os.environ['QT_QPA_PLATFORM']='offscreen'
from PySide6.QtCore import QObject,Signal
from PySide6.QtWidgets import QApplication
from account_service import derive_auth_key
from cloud_api import CloudApi
from cloud_config import DEFAULT_CLOUD_URL
from cloud_events import CloudEvents

APP=QApplication.instance() or QApplication([])

def until(predicate,seconds=10):
    end=time.monotonic()+seconds
    while time.monotonic()<end:
        APP.processEvents()
        if predicate():return True
        time.sleep(.004)
    return False

class Account(QObject):
    account_changed=Signal(object)
    def __init__(self,api):
        super().__init__();self.api=api;self.account=None
    @property
    def signed_in(self):return bool(self.account and self.api.token)
    def start(self,account):self.account=account;self.account_changed.emit(account)

def call(api,method,path,data=None,raw=None,authenticated=True,headers=None):
    done=[]
    api.request(method,path,lambda value,error:done.append((value,error)),data=data,raw=raw,
        authenticated=authenticated,headers=headers,timeout_ms=60000)
    if not until(lambda:done,65):raise RuntimeError('请求超时: '+path)
    value,error=done[0]
    if error:raise RuntimeError(f'{path}: HTTP {error.status} {error.code}')
    return value

def run():
    name='qa-push-'+uuid4().hex[:20];secret=secrets.token_urlsafe(24)
    apis=[CloudApi(DEFAULT_CLOUD_URL),CloudApi(DEFAULT_CLOUD_URL)]
    events=[];tokens=[];item=None;version=None
    result={'ok':False,'endpoint':DEFAULT_CLOUD_URL}
    try:
        for index,action in enumerate(('register','login')):
            api=apis[index]
            challenge=call(api,'POST','/v1/auth/challenge',{'username':name,'purpose':action},authenticated=False)
            proof=derive_auth_key(bytearray(secret.encode()),bytes.fromhex(challenge['salt']))
            try:
                signed=call(api,'POST','/v1/auth/'+action,{'username':name,'challenge_id':challenge['challenge_id'],
                    'auth_key':proof.hex(),'device_name':'Push test '+str(index+1),'platform':'windows'},authenticated=False)
            finally:proof[:]=b'\0'*len(proof)
            api.token=signed['token'];tokens.append(signed['token'])
            account=Account(api);stream=CloudEvents(account,enabled=True)
            received={'content':[],'devices':[]}
            stream.content_changed.connect(lambda r=received:r['content'].append(time.perf_counter()))
            stream.devices_changed.connect(lambda r=received:r['devices'].append(time.perf_counter()))
            events.append((stream,received,account))
            account.start(signed['account'])
            if not until(lambda:len(received['content'])>0 and len(received['devices'])>0,12):
                raise RuntimeError('WebSocket did not connect and dispatch initial refresh')
        content=os.urandom(100000)
        item=str(uuid4());upload=str(uuid4())
        count=len(events[1][1]['content']);started=time.perf_counter()
        allocation=call(apis[0],'POST','/v1/uploads',{'upload_id':upload,'item_id':item,'base_version':0,
            'kind':'file','name':'qa-push.bin','mime_type':'application/octet-stream',
            'size':len(content),'sha256':hashlib.sha256(content).hexdigest()})
        if not until(lambda:len(events[1][1]['content'])>count,5):raise RuntimeError('Peer did not see upload start')
        pending_ms=round((events[1][1]['content'][-1]-started)*1000,1)
        call(apis[0],'PUT',f'/v1/uploads/{upload}/parts/0',raw=content,
            headers={'X-Content-SHA256':hashlib.sha256(content).hexdigest()})
        count=len(events[1][1]['content']);started=time.perf_counter()
        committed=call(apis[0],'POST',f'/v1/uploads/{upload}/commit',{})
        version=committed['record']['version']
        if not until(lambda:len(events[1][1]['content'])>count,5):raise RuntimeError('Peer did not see completed upload')
        commit_ms=round((events[1][1]['content'][-1]-started)*1000,1)
        count=len(events[0][1]['devices']);started=time.perf_counter()
        call(apis[1],'PATCH','/v1/devices/'+signed['device_id'],{'name':'Renamed push test'})
        if not until(lambda:len(events[0][1]['devices'])>count,5):raise RuntimeError('Peer did not see device rename')
        devices_ms=round((events[0][1]['devices'][-1]-started)*1000,1)
        result.update(ok=True,pending_hint_ms=pending_ms,commit_hint_ms=commit_ms,
            device_hint_ms=devices_ms,content_bytes=len(content))
    except Exception as error:result['error']=str(error)[:200]
    finally:
        for stream,_,_ in events:stream.close()
        if item and version:
            try:call(apis[0],'DELETE','/v1/items/'+item,{'base_version':version})
            except Exception as error:result['cleanup_error']=str(error)[:120]
        for api in apis:
            if api.token:
                try:call(api,'POST','/v1/auth/logout',{})
                except Exception:pass
            api.close()
    return result

if __name__=='__main__':
    import sys
    value=run()
    target=Path('outputs/FlickDock-1.8.3-cloud-events-live.json')
    target.parent.mkdir(parents=True,exist_ok=True)
    target.write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(value,ensure_ascii=False))
    sys.exit(0 if value['ok'] else 1)
