"""Pinned application-publisher signatures, in addition to TLS and body SHA256."""
import base64,json

TRUSTED_PUBLISHERS={'flickdock-ed25519-v1':'feb8e7decdc925fec51d1c9fa395678e80f3a88026709454fe1fb1b7906d741a'}


def signing_bytes(manifest):
    value={key:item for key,item in manifest.items() if key!='publisher_signature'}
    return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode('utf8')


def sign_manifest(manifest,signer,key_id='flickdock-ed25519-v1'):
    result=dict(manifest)
    result['publisher_signature']=dict(algorithm='Ed25519',key_id=key_id,value=base64.b64encode(signer.sign(signing_bytes(result))).decode('ascii'))
    verify_manifest_signature(result,required=True)
    return result


def verify_manifest_signature(manifest,*,required=False):
    signature=manifest.get('publisher_signature')
    if signature is None:
        if required:raise ValueError('更新缺少受信任的发布者签名。')
        return False
    try:
        if not isinstance(signature,dict) or set(signature)!={'algorithm','key_id','value'} or signature['algorithm']!='Ed25519':raise ValueError()
        public=TRUSTED_PUBLISHERS[signature['key_id']]
        if not isinstance(signature['value'],str) or len(signature['value'])!=88:raise ValueError()
        raw=base64.b64decode(signature['value'],validate=True)
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        Ed25519PublicKey.from_public_bytes(bytes.fromhex(public)).verify(raw,signing_bytes(manifest))
    except Exception as error:
        raise ValueError('更新发布者签名无效，已拒绝此安装包。') from error
    return True
