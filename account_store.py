"""Device session in the OS credential store; passwords never reach disk."""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import sys
from uuid import uuid4


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name('.' + path.name + '-' + uuid4().hex + '.tmp')
    try:
        with temporary.open('x', encoding='utf8') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class SessionStore:
    def __init__(self, base, origin, *, backend=None, enabled=True):
        self.path = Path(base) / 'account-session.json'
        self.service = 'FlickDock.CloudSession'
        self.key = hashlib.sha256((str(Path(base).resolve()) + '\n' + origin).encode()).hexdigest()
        self.enabled, self._backend = enabled, backend

    def backend(self):
        if self._backend is None:
            if sys.platform == 'win32':
                from keyring.backends.Windows import WinVaultKeyring
                self._backend = WinVaultKeyring()
            elif sys.platform == 'darwin':
                from keyring.backends.macOS import Keyring
                self._backend = Keyring()
            else:
                raise RuntimeError('此系统未配置安全凭据存储。')
        return self._backend

    def load(self):
        if not self.enabled: return None
        try:
            if self.path.stat().st_size > 16384: return None
            state = json.loads(self.path.read_text(encoding='utf8'))
            token = self.backend().get_password(self.service, self.key)
            if isinstance(state, dict) and isinstance(state.get('account'), dict) and token:
                return dict(state, token=token)
        except Exception:
            return None

    def save(self, session):
        if not self.enabled: return
        token = session.get('token')
        if not isinstance(token, str) or not token: raise ValueError('登录凭据无效。')
        backend = self.backend()
        prior = backend.get_password(self.service, self.key)
        try:
            backend.set_password(self.service, self.key, token)
            atomic_json(self.path, {k: session[k] for k in ('account','device_id','expires_at') if k in session})
        except Exception:
            if prior: backend.set_password(self.service, self.key, prior)
            else:
                try: backend.delete_password(self.service, self.key)
                except Exception: pass
            raise

    def clear(self):
        if not self.enabled: return
        try: self.backend().delete_password(self.service, self.key)
        except Exception: pass
        self.path.unlink(missing_ok=True)
