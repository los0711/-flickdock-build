"""Actual Qt transport and client against disposable local Workers/D1/R2.

No desktop actions, real credential store, external emails or cloud writes.
Requires `npm ci` in cloud/ and Node.js. A macos device here is a protocol peer,
not a claim to have tested an application on a physical Mac.
"""
from __future__ import annotations
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from uuid import uuid4

os.environ['QT_QPA_PLATFORM']='offscreen'
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QTimer
from account_service import AccountService
from cloud_api import CloudApi
from shelf_sync import ShelfSync

app=QApplication.instance() or QApplication([])
root=Path(__file__).resolve().parent;cloud=root/'cloud'
node=shutil.which('node')
assert node and (cloud/'node_modules/wrangler/bin/wrangler.js').is_file(),'Run npm ci in cloud/ first'
wrangler=cloud/'node_modules/wrangler/bin/wrangler.js'


def until(condition,timeout=30):
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        app.processEvents()
        if condition():return
        time.sleep(.003)
    raise AssertionError('Local cloud client timed out')


class MemoryStore:
    def __init__(self):self.value=None
    def load(self):return copy.deepcopy(self.value)
    def save(self,value):self.value=copy.deepcopy(value)
    def clear(self):self.value=None


class Adapter:
    def __init__(self):self.view={'permanent':[],'texts':{},'metadata':{}}
    def snapshot(self):return copy.deepcopy(self.view)
    def restore(self,view):self.view=copy.deepcopy(view)
    def title(self,ref):return self.view['metadata'].get(ref,{}).get('title') or (self.view['texts'][ref][:60] if ref in self.view['texts'] else Path(ref).name)


with tempfile.TemporaryDirectory(prefix='flickdock-real-cloud-client-') as temporary:
    temp=Path(temporary);config=json.loads((cloud/'wrangler.jsonc').read_text(encoding='utf8'))
    config['main']=str(cloud/'src/index.ts');config['d1_databases'][0]['migrations_dir']=str(cloud/'migrations')
    config['vars']['AUTH_PEPPER']='isolated-local-test-pepper-never-for-production'
    config['vars']['EMAIL_PROVIDER']='disabled';config['triggers']={}
    config_path=temp/'wrangler-test.json';config_path.write_text(json.dumps(config),encoding='utf8')
    environment=dict(os.environ,WRANGLER_SEND_METRICS='false',CLOUDFLARE_API_TOKEN='',CLOUDFLARE_API_KEY='',CLOUDFLARE_EMAIL='',CLOUDFLARE_ACCOUNT_ID='')
    command=[node,str(wrangler),'--config',str(config_path)]
    migration=subprocess.run(command+['d1','migrations','apply','DB','--local','--persist-to',str(temp/'state')],cwd=temp,env=environment,capture_output=True,text=True,encoding='utf8',timeout=60)
    assert migration.returncode==0,migration.stdout+migration.stderr
    with socket.socket() as listener:listener.bind(('127.0.0.1',0));port=listener.getsockname()[1]
    origin=f'http://127.0.0.1:{port}'
    log=(temp/'worker.log').open('wb')
    worker=subprocess.Popen(command+['dev','--local','--ip','127.0.0.1','--port',str(port),'--persist-to',str(temp/'state')],cwd=temp,env=environment,stdout=log,stderr=log,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    job=None
    if os.name=='nt':
        import ctypes
        kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        kernel.CreateJobObjectW.restype=ctypes.c_void_p
        kernel.AssignProcessToJobObject.argtypes=[ctypes.c_void_p,ctypes.c_void_p]
        kernel.TerminateJobObject.argtypes=[ctypes.c_void_p,ctypes.c_uint]
        kernel.CloseHandle.argtypes=[ctypes.c_void_p]
        job=kernel.CreateJobObjectW(None,None)
        if not job or not kernel.AssignProcessToJobObject(job,int(worker._handle)):
            raise ctypes.WinError(ctypes.get_last_error())
    services=[];engines=[]
    try:
        ready=False
        for attempt in range(120):
            try:
                with urllib.request.urlopen(origin+'/v1/health',timeout=1) as response:ready=response.status==200
            except Exception:time.sleep(.25)
            if ready:break
            if worker.poll() is not None:break
        assert ready,(temp/'worker.log').read_text(encoding='utf8',errors='replace')[-6000:]
        for index in range(2):
            api=CloudApi(origin,allow_test_http=True)
            services.append(AccountService(temp/str(index),origin=origin,api=api,store=MemoryStore()))
        statuses=[[],[]]
        for i,service in enumerate(services):service.status_changed.connect(statuses[i].append)
        username='client-'+uuid4().hex;password='isolated-'+uuid4().hex
        services[0].register(username,password);until(lambda:services[0].signed_in or not services[0].busy)
        assert services[0].signed_in,statuses[0]
        services[1].platform='macos';services[1].device_name='Isolated macOS protocol peer'
        services[1].login(username,password);until(lambda:services[1].signed_in or not services[1].busy)
        assert services[1].signed_in,statuses[1]
        devices=[];services[0].devices_changed.connect(lambda value:devices.append(value));services[0].list_devices()
        until(lambda:devices and len(devices[-1])==2)
        assert {d['platform'] for d in devices[-1]}=={'windows','macos'}
        print('PASS Qt challenge/background KDF/register/login/two real device sessions')
        adapters=[Adapter(),Adapter()]
        original=temp/'multiple-chunks.mp4';payload=(bytes(range(256))*11000);original.write_bytes(payload)
        empty=temp/'empty.txt';empty.write_bytes(b'')
        adapters[0].view={'permanent':[str(original),'flickdock:text:local',str(empty)],
            'texts':{'flickdock:text:local':'跨 Windows / Mac 文字，不转 txt'},'metadata':{}}
        sync_status=[[],[]]
        for i in range(2):
            engine=ShelfSync(temp/str(i),services[i],adapters[i],poll_ms=600000);engines.append(engine)
            engine.status_changed.connect(sync_status[i].append)
            engine.select_account(services[i].account)
        # Both event loops run real transfers; manually poll when the second
        # peer's first pull occurred before the first peer finished uploading.
        try:until(lambda:not engines[0].running and len(engines[0].state['entries'])==3 and all(not e.get('dirty') for e in engines[0].state['entries'].values()))
        except AssertionError:raise AssertionError(sync_status)
        engines[1].tick();until(lambda:not engines[1].running and len(adapters[1].view['permanent'])==3)
        remote=next(Path(ref) for ref in adapters[1].view['permanent'] if ref.endswith('.mp4'))
        assert remote.read_bytes()==payload and next(Path(ref) for ref in adapters[1].view['permanent'] if ref.endswith('.txt')).stat().st_size==0
        assert list(adapters[1].view['texts'].values())==['跨 Windows / Mac 文字，不转 txt']
        original.rename(original.with_suffix('.released'));original.with_suffix('.released').unlink()
        print('PASS real multipart upload/download/full SHA256, native text and empty file')
        # Cross-account URL/content access must not succeed even when IDs leak.
        third=AccountService(temp/'third',origin=origin,api=CloudApi(origin,allow_test_http=True),store=MemoryStore());services.append(third)
        third.register('other-'+uuid4().hex,password);until(lambda:third.signed_in or not third.busy);assert third.signed_in
        item=next(iter(engines[0].state['entries']));result=[]
        third.api.request('GET','/v1/items/'+item+'/content',lambda value,error:result.append((value,error)))
        until(lambda:result);assert result[0][1] and result[0][1].status==404
        print('PASS private ownership enforced through actual Qt HTTP')
        # Session revocation reaches the active replica, clears its account,
        # cancels requests and restores its separate guest profile.
        services[0].revoke_device(services[1].device_id)
        until(lambda:any(d['id']==services[1].device_id and d.get('revoked_at') for d in devices[-1]))
        services[1].refresh();until(lambda:not services[1].signed_in)
        assert engines[1].account_id is None
        print('PASS device revocation and account-scoped replica shutdown')
        print('CLOUD_CLIENT_V180_OK')
    finally:
        for engine in engines:engine.close()
        for service in services:service.close()
        if job:
            kernel.TerminateJobObject(job,0);kernel.CloseHandle(job)
        elif os.name=='nt':
            subprocess.run(['taskkill','/PID',str(worker.pid),'/T','/F'],capture_output=True,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        else:worker.terminate()
        try:worker.wait(8)
        except subprocess.TimeoutExpired:worker.kill();worker.wait(4)
        time.sleep(.3)
        log.close();app.processEvents()
