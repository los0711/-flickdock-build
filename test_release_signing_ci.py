"""Exercise the CI signer with disposable keys; never read the user's vault."""
import base64
import os
import unittest
from unittest.mock import patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import release_signing
import update_signatures


class ReleaseSigningTests(unittest.TestCase):
    def setUp(self):
        self.key = Ed25519PrivateKey.generate()
        self.secret = base64.b64encode(self.key.private_bytes(
            serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
            serialization.NoEncryption())).decode('ascii')
        self.public = self.key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()

    def test_ci_secret_signs_for_the_existing_publisher_identity(self):
        with patch.dict(os.environ, {'GITHUB_ACTIONS':'true',
                                    'FLICKDOCK_RELEASE_SIGNING_KEY':self.secret}, clear=True):
            with patch.dict(update_signatures.TRUSTED_PUBLISHERS, {'flickdock-ed25519-v1':self.public}):
                signer = release_signing.publisher_signer()
                manifest = update_signatures.sign_manifest({'version':'1.8.38'}, signer)
                self.assertTrue(update_signatures.verify_manifest_signature(manifest, required=True))

    def test_secret_is_rejected_outside_github_actions(self):
        with patch.dict(os.environ, {'FLICKDOCK_RELEASE_SIGNING_KEY':self.secret}, clear=True):
            with self.assertRaisesRegex(ValueError, 'GitHub Actions'):
                release_signing.publisher_signer()

    def test_wrong_key_is_rejected(self):
        with patch.dict(os.environ, {'GITHUB_ACTIONS':'true',
                                    'FLICKDOCK_RELEASE_SIGNING_KEY':self.secret}, clear=True):
            with patch.dict(update_signatures.TRUSTED_PUBLISHERS, {'flickdock-ed25519-v1':'00'*32}):
                with self.assertRaisesRegex(ValueError, '信任密钥不一致'):
                    release_signing.publisher_signer()

    def test_empty_secret_is_rejected(self):
        with patch.dict(os.environ, {'GITHUB_ACTIONS':'true',
                                    'FLICKDOCK_RELEASE_SIGNING_KEY':''}, clear=True):
            with self.assertRaisesRegex(ValueError, '凭据不可用'):
                release_signing.publisher_signer()


if __name__ == '__main__':
    unittest.main()
