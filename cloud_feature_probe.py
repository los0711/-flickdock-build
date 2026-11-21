"""Explicit opt-in cloud account/transfer smoke probe; memory-only credentials.

Creates a disposable account, then removes its content and active sessions.
No native UI, keychain, installed app, or user's shelf is accessed.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import sys
import time
from uuid import uuid4

os.environ['QT_QPA_PLATFORM']='offscreen'
from PySide6.QtWidgets import QApplication
from account_service import AUTH_KDF,derive_auth_key,validate_auth_challenge
from cloud_api import CloudApi
from cloud_config import DEFAULT_CLOUD_URL


def run(endpoint):
    app=QApplication.instance() or QApplication([])
    apis=[];checks=[];pending=set();records={};primary=None
    name='qa-features-'+uuid4().hex[:20]
    password,new_password=secrets.token_urlsafe(32),secrets.token_urlsafe(32)
    report={'mode':'actual TLS cloud with disposable account; no desktop/keychain/user data',
            'ok':False,'checks':checks,'cleanup':{}}
    def check(name,value=True):
        if not value:raise RuntimeError(name)
        checks.append({'name':name,'ok':True})
    def api():
        value=CloudApi(endpoint);apis.append(value);return value
    def call(client,method,path,data=None,raw=None,expected=200,authenticated=True,headers=None):
        result=[]
        client.request(method,path,lambda value,error:result.append((value,error)),data=data,raw=raw,
                       authenticated=authenticated,headers=headers,timeout_ms=60000)
        deadline=time.monotonic()+70
        while not result and time.monotonic()<deadline:app.processEvents();time.sleep(.002)
        if not result:raise RuntimeError('Cloud probe request timed out')
        value,error=result[0]
        if expected>=400:
            if error is None or error.status!=expected:raise RuntimeError(f'Expected HTTP {expected}; got {getattr(error,"status",200)} {getattr(error,"code","")}')
            return error
        if error is not None:raise RuntimeError(f'Cloud feature probe HTTP {error.status} {error.code}')
        return value
    def key(secret,salt):
        password_bytes=bytearray(secret.encode('utf8'));proof=None
        try:
            proof=derive_auth_key(password_bytes,bytes.fromhex(salt));return proof.hex()
        finally:
            password_bytes[:]=b'\0'*len(password_bytes)
            if proof is not None:proof[:]=b'\0'*len(proof)
    def sign_in(client,action,secret,device):
        challenge=call(client,'POST','/v1/auth/challenge',{'username':name,'purpose':action},authenticated=False)
        validate_auth_challenge(challenge)
        result=call(client,'POST','/v1/auth/'+action,{'username':name,'challenge_id':challenge['challenge_id'],
            'auth_key':key(secret,challenge['salt']),'device_name':device,'platform':'windows'},expected=201 if action=='register' else 200,authenticated=False)
        client.token=result['token'];return result
    def reserve(client,item_id,data,upload_id=None):
        upload_id=upload_id or str(uuid4());pending.add(upload_id)
        spec={'upload_id':upload_id,'item_id':item_id,'base_version':0,'kind':'file','name':'original-probe.bin',
              'mime_type':'application/octet-stream','size':len(data),'sha256':hashlib.sha256(data).hexdigest()}
        return call(client,'POST','/v1/uploads',spec,expected=201),spec
    def part(client,reserved,index,data):
        return call(client,'PUT',f'/v1/uploads/{reserved["upload_id"]}/parts/{index}',raw=data,
                    headers={'X-Content-SHA256':hashlib.sha256(data).hexdigest()})
    def password_body(client,old,new):
        challenge=call(client,'POST','/v1/auth/password/challenge',{})
        validate_auth_challenge(challenge)
        if challenge.get('kdf')!=AUTH_KDF or not isinstance(challenge.get('new_salt'),str) or len(challenge['new_salt'])!=32:raise RuntimeError('Invalid password challenge')
        return {'challenge_id':challenge['challenge_id'],'old_auth_key':key(old,challenge['salt']),
                'new_auth_key':key(new,challenge['new_salt'])}
    try:
        primary,peer=api(),api()
        first=sign_in(primary,'register',password,'Probe primary');second=sign_in(peer,'login',password,'Probe peer')
        check('two independent device sessions',first['account']['id']==second['account']['id'] and first['device_id']!=second['device_id'])
        call(primary,'PATCH','/v1/devices/'+second['device_id'],{'name':'Renamed probe peer'})
        check('device rename visible on peer',next(d for d in call(peer,'GET','/v1/devices')['devices'] if d['current'])['name']=='Renamed probe peer')
        data=os.urandom(131073);item=str(uuid4());reserved,spec=reserve(primary,item,data)
        transfer=next(t for t in call(peer,'GET','/v1/transfers')['transfers'] if t['upload_id']==reserved['upload_id'])
        check('pending transfer visible before content is committed',transfer['received_bytes']==0 and transfer['device_id']==first['device_id'])
        chunk=reserved['chunk_size'];part(primary,reserved,0,data[:chunk])
        transfer=next(t for t in call(peer,'GET','/v1/sync/changes')['transfers'] if t['upload_id']==reserved['upload_id'])
        check('peer sees verified partial upload progress',transfer['received_bytes']==chunk)
        call(peer,'DELETE','/v1/uploads/'+reserved['upload_id'])
        for method,path,payload in [('GET','/v1/uploads/'+reserved['upload_id'],None),('POST','/v1/uploads',spec),('POST','/v1/uploads/'+reserved['upload_id']+'/commit',{})]:
            problem=call(primary,method,path,payload,expected=410);check('cancelled UUID remains terminal for '+method+' '+path.split('/')[-1],problem.code=='UPLOAD_CANCELLED')
        check('cancel reclaims reserved R2 space',call(primary,'GET','/v1/me')['storage']['used_bytes']==0)
        late_id=str(uuid4());pending.add(late_id);call(primary,'DELETE','/v1/uploads/'+late_id)
        late_spec=dict(spec,upload_id=late_id,item_id=str(uuid4()))
        check('cancellation before late init cannot allocate storage',call(primary,'POST','/v1/uploads',late_spec,expected=410).code=='UPLOAD_CANCELLED')
        content=b'FlickDock original bytes\0\xff\x80'+os.urandom(257);committed_id=str(uuid4());finished,finished_spec=reserve(primary,committed_id,content)
        part(primary,finished,0,content);committed=call(primary,'POST','/v1/uploads/'+finished['upload_id']+'/commit',{})['record']
        records[committed_id]=committed['version'];pending.discard(finished['upload_id'])
        check('small original content commits with full SHA',committed['sha256']==hashlib.sha256(content).hexdigest())
        wrong=password_body(primary,'wrong '+password,new_password)
        check('incorrect old password rejected without logout',call(primary,'POST','/v1/auth/password',wrong,expected=403).code=='INVALID_OLD_PASSWORD')
        call(primary,'GET','/v1/me')
        correct=password_body(primary,password,new_password);changed=call(primary,'POST','/v1/auth/password',correct)
        check('password change revokes other sessions',changed.get('other_devices_revoked') is True)
        check('current session survives password change',call(primary,'GET','/v1/me')['device_id']==first['device_id'])
        check('other session must sign in again',call(peer,'GET','/v1/me',expected=401).code=='UNAUTHORIZED')
        replacement=api();third=sign_in(replacement,'login',new_password,'Probe new password')
        check('new password signs in successfully',third['account']['id']==first['account']['id'])
        call(primary,'DELETE','/v1/items/'+committed_id,{'base_version':committed['version']});records.pop(committed_id)
        check('committed item deletion reclaims all R2 quota',call(primary,'GET','/v1/me')['storage']['used_bytes']==0)
        report['ok']=True
    except Exception as error:
        report['error']=str(error)[:240]
    finally:
        clean=True
        if primary and primary.token:
            for upload_id in pending:
                try:call(primary,'DELETE','/v1/uploads/'+upload_id)
                except Exception:clean=False
            for item_id,version in records.items():
                try:call(primary,'DELETE','/v1/items/'+item_id,{'base_version':version})
                except Exception:clean=False
            try:report['cleanup']['used_bytes']=call(primary,'GET','/v1/me')['storage']['used_bytes']
            except Exception:clean=False
        for client in reversed(apis):
            if client.token:
                try:
                    result=[];client.request('POST','/v1/auth/logout',lambda value,error:result.append(error),data={})
                    deadline=time.monotonic()+65
                    while not result and time.monotonic()<deadline:app.processEvents();time.sleep(.002)
                    if not result or result[0] is not None and result[0].status!=401:clean=False
                except Exception:clean=False
            client.token='';client.close()
        report['cleanup'].update(content_and_sessions_removed=clean,disposable_empty_account_retained=True)
        report['ok']=bool(report['ok'] and clean and report['cleanup'].get('used_bytes')==0)
        password=new_password=''
    return report


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--endpoint',default=DEFAULT_CLOUD_URL);parser.add_argument('--output',required=True)
    args=parser.parse_args(argv);report=run(args.endpoint)
    destination=Path(args.output);destination.parent.mkdir(parents=True,exist_ok=True)
    destination.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8')
    print(json.dumps({'ok':report['ok'],'checks':len(report['checks']),'cleanup':report['cleanup'],'error':report.get('error')},ensure_ascii=False))
    return 0 if report['ok'] else 1


if __name__=='__main__':raise SystemExit(main())
