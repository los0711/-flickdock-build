"""Release operator only. Private material never leaves the OS credential vault."""
import base64,sys


def publisher_signer():
    if sys.platform=='win32':
        from keyring.backends.Windows import WinVaultKeyring
        vault=WinVaultKeyring()
    elif sys.platform=='darwin':
        from keyring.backends.macOS import Keyring
        vault=Keyring()
    else:raise ValueError('此发布机未配置签名凭据存储。')
    saved=vault.get_password('FlickDock.ReleaseSigning','ed25519-v1')
    if not saved:raise ValueError('发布签名凭据不可用。')
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives import serialization
    from update_signatures import TRUSTED_PUBLISHERS
    key=Ed25519PrivateKey.from_private_bytes(base64.b64decode(saved,validate=True))
    if key.public_key().public_bytes(serialization.Encoding.Raw,serialization.PublicFormat.Raw).hex()!=TRUSTED_PUBLISHERS['flickdock-ed25519-v1']:
        raise ValueError('发布签名凭据与客户端信任密钥不一致。')
    return key
