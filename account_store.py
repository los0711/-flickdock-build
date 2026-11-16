"""Device session in the OS credential store; passwords never reach disk."""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import sys
import threading
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
        self._metadata_lock=threading.Lock()
        self._generation=0

    def generation(self):
        with self._metadata_lock:return self._generation

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

    def save(self, session, *, generation=None):
        if not self.enabled: return
        token = session.get('token')
        if not isinstance(token, str) or not token: raise ValueError('登录凭据无效。')
        with self._metadata_lock:
            if generation is None:generation=self._generation
            if generation!=self._generation:return False
        backend = self.backend()
        prior = backend.get_password(self.service, self.key)
        try:
            with self._metadata_lock:
                if generation!=self._generation:return False
            backend.set_password(self.service, self.key, token)
            with self._metadata_lock:
                if generation!=self._generation:return False
                atomic_json(self.path, {k: session[k] for k in ('account','device_id','expires_at') if k in session})
        except Exception:
            if prior: backend.set_password(self.service, self.key, prior)
            else:
                try: backend.delete_password(self.service, self.key)
                except Exception: pass
            raise
        return True

    def clear(self):
        if not self.enabled: return
        self.invalidate()
        self.clear_credentials()

    def clear_credentials(self):
        if not self.enabled:return
        try: self.backend().delete_password(self.service, self.key)
        except Exception: pass

    def invalidate(self):
        """Make a session unrestorable without waiting for a Keychain prompt."""
        if not self.enabled:return
        with self._metadata_lock:
            self._generation+=1
            self.path.unlink(missing_ok=True)
