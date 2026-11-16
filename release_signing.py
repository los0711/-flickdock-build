"""Release operator only. Private material comes from a vault or CI secret."""
import base64,os,sys


def publisher_signer():
    secret=os.environ.get('FLICKDOCK_RELEASE_SIGNING_KEY')
    if secret is not None:
        if os.environ.get('GITHUB_ACTIONS')!='true':
            raise ValueError('环境发布密钥只能由 GitHub Actions 使用。')
        saved=secret
    elif sys.platform=='win32':
        from keyring.backends.Windows import WinVaultKeyring
        vault=WinVaultKeyring()
        saved=vault.get_password('FlickDock.ReleaseSigning','ed25519-v1')
    elif sys.platform=='darwin':
        from keyring.backends.macOS import Keyring
        vault=Keyring()
        saved=vault.get_password('FlickDock.ReleaseSigning','ed25519-v1')
    else:raise ValueError('此发布机未配置签名凭据存储。')
    if not saved:raise ValueError('发布签名凭据不可用。')
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives import serialization
    from update_signatures import TRUSTED_PUBLISHERS
    key=Ed25519PrivateKey.from_private_bytes(base64.b64decode(saved,validate=True))
    if key.public_key().public_bytes(serialization.Encoding.Raw,serialization.PublicFormat.Raw).hex()!=TRUSTED_PUBLISHERS['flickdock-ed25519-v1']:
        raise ValueError('发布签名凭据与客户端信任密钥不一致。')
    return key
