"""Publisher pinning and tamper rejection, using ephemeral keys only."""
import copy,unittest
from unittest.mock import patch
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization
from update_signatures import TRUSTED_PUBLISHERS,sign_manifest,verify_manifest_signature
from update_client import validate_manifest


class PublisherSignatureTests(unittest.TestCase):
    def setUp(self):
        self.key=Ed25519PrivateKey.generate()
        public=self.key.public_key().public_bytes(serialization.Encoding.Raw,serialization.PublicFormat.Raw).hex()
        self.pin=patch.dict(TRUSTED_PUBLISHERS,{'fixture':public});self.pin.start();self.addCleanup(self.pin.stop)
        self.manifest=dict(schema_version=1,version='1.8.21',published_at='2026-10-30T00:00:00Z',notes='new release',
            installer=dict(url='https://example.org/update.exe',filename='update.exe',sha256='a'*64,size=123))

    def test_signed_manifest_roundtrip_and_all_published_fields_are_protected(self):
        signed=sign_manifest(self.manifest,self.key,'fixture')
        self.assertEqual(validate_manifest(signed,{'example.org'},platform='win32'),signed)
        self.assertTrue(verify_manifest_signature(signed,required=True))
        for field in ('notes','version','published_at'):
            changed=copy.deepcopy(signed);changed[field]+='altered'
            with self.assertRaises(ValueError):verify_manifest_signature(changed,required=True)
        for field,value in (('size',124),('sha256','b'*64),('url','https://example.org/other.exe')):
            changed=copy.deepcopy(signed);changed['installer'][field]=value
            with self.assertRaises(ValueError):verify_manifest_signature(changed,required=True)
        changed=copy.deepcopy(signed);changed['releases']=[dict(version='1.8.21',notes='altered')]
        with self.assertRaises(ValueError):verify_manifest_signature(changed,required=True)

    def test_missing_unknown_and_corrupt_signatures_never_verify(self):
        self.assertFalse(verify_manifest_signature(self.manifest))
        with self.assertRaises(ValueError):verify_manifest_signature(self.manifest,required=True)
        signed=sign_manifest(self.manifest,self.key,'fixture')
        for key,value in (('key_id','untrusted'),('value','A'*88),('algorithm','none')):
            changed=copy.deepcopy(signed);changed['publisher_signature'][key]=value
            with self.assertRaises(ValueError):validate_manifest(changed,{'example.org'},platform='win32')


if __name__=='__main__':unittest.main()
