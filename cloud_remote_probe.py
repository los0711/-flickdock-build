"""Explicit operator probe; never prints credentials/content or sends email."""
import argparse
import hashlib
import json
from pathlib import Path
import secrets
import urllib.error
import urllib.request
from uuid import uuid4


def run(origin,output):
    checks=[];sessions=[];identity=str(uuid4())
    def request(method,path,data=None,token=None,raw=None,headers=None):
        body=raw if raw is not None else json.dumps(data).encode() if data is not None else None
        h={'Accept':'application/json','User-Agent':'FlickDock/1.8.0 (CloudSync)',**(headers or {})}
        if body is not None:h['Content-Type']='application/octet-stream' if raw is not None else 'application/json'
        if token:h['Authorization']='Bearer '+token
        req=urllib.request.Request(origin+path,data=body,headers=h,method=method)
        try:
            with urllib.request.urlopen(req,timeout=60) as response:
                value=response.read()
                return (value,response.headers) if raw is False else json.loads(value)
        except urllib.error.HTTPError as problem:
            try:code=json.loads(problem.read()).get('error',{}).get('code','')
            except Exception:code='non_json_response'
            raise RuntimeError(f'Cloud probe HTTP {problem.code}: {code}') from None
    def auth(action,username,password,platform):
        challenge=request('POST','/v1/auth/challenge',{'username':username,'purpose':action})
        assert challenge['kdf']==dict(name='scrypt',N=16384,r=8,p=5,dkLen=32)
        derived=hashlib.scrypt(password.encode(),salt=bytes.fromhex(challenge['salt']),n=16384,r=8,p=5,dklen=32,maxmem=32*1024*1024)
        result=request('POST','/v1/auth/'+action,dict(username=username,challenge_id=challenge['challenge_id'],auth_key=derived.hex(),device_name='FlickDock deployment verification',platform=platform))
        sessions.append(result);return result
    username='deploy-check-'+uuid4().hex;password=secrets.token_urlsafe(32)
    payload=bytes(range(256))*(8*1024*1024//256);digest=hashlib.sha256(payload).hexdigest()
    try:
        assert request('GET','/v1/health')['schema_version']==1;checks.append('deployed HTTPS health')
        windows=auth('register',username,password,'windows');mac=auth('login',username,password,'macos')
        token=windows['token'];checks.append('registration without email and independent Windows/macOS protocol sessions')
        upload=request('POST','/v1/uploads',dict(item_id=identity,base_version=0,kind='file',name='deployment-check.bin',mime_type='application/octet-stream',size=len(payload),sha256=digest),token)
        for index in range(upload['part_count']):
            part=payload[index*upload['chunk_size']:(index+1)*upload['chunk_size']]
            request('PUT',f'/v1/uploads/{upload["upload_id"]}/parts/{index}',token=token,raw=part,headers={'X-Content-SHA256':hashlib.sha256(part).hexdigest()})
        record=request('POST',f'/v1/uploads/{upload["upload_id"]}/commit',{},token)['record']
        received=hashlib.sha256();total=0
        for index in range(upload['part_count']):
            # Raw read has no request body; an explicit helper avoids treating
            # server bytes as JSON and verifies each authenticated part.
            req=urllib.request.Request(origin+f'/v1/items/{identity}/parts/{index}?version={record["version"]}',headers={'Authorization':'Bearer '+mac['token'],'User-Agent':'FlickDock/1.8.0 (CloudSync)'})
            with urllib.request.urlopen(req,timeout=60) as response:
                part=response.read();assert hashlib.sha256(part).hexdigest()==response.headers['X-Content-SHA256']
            received.update(part);total+=len(part)
        assert total==len(payload) and received.hexdigest()==digest
        checks.append(f'{total} bytes real private R2 multipart upload/download with full SHA256')
        assert any(c['record']['id']==identity for c in request('GET','/v1/sync/changes?cursor=0',token=mac['token'])['changes'])
        request('DELETE','/v1/devices/'+mac['device_id'],token=token)
        try:request('GET','/v1/me',token=mac['token'])
        except RuntimeError as problem:assert '401' in str(problem)
        else:raise AssertionError('Revoked device retained access')
        checks.append('revocation enforced by deployed service')
        request('DELETE','/v1/items/'+identity,{'base_version':record['version']},token)
        request('POST','/v1/auth/logout',{},token)
        report=dict(ok=True,origin=origin,checks=checks,probe_account_id=windows['account']['id'],probe_item_id=identity,probe_item_deleted=True,sessions_revoked=True,email_sent=False)
        Path(output).write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8')
        print(json.dumps({k:v for k,v in report.items() if not k.startswith('probe_')},ensure_ascii=False))
    except Exception:
        for session in sessions:
            try:request('POST','/v1/auth/logout',{},session['token'])
            except Exception:pass
        raise


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--url',required=True);parser.add_argument('--report',required=True)
    args=parser.parse_args();run(args.url.rstrip('/'),args.report)
