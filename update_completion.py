"""Persist a completed-update notice until the user reads it in overview."""
import json
import logging
from pathlib import Path
from account_store import atomic_json
from update_client import compare_versions, version_tuple
from release_history import update_notes

MAX_NOTICE_BYTES = 128 * 1024


def record_update(base, current_version, manifest):
    version_tuple(current_version)
    version = manifest['version']
    if compare_versions(version, current_version) <= 0:
        raise ValueError('更新版本必须高于当前版本。')
    atomic_json(Path(base) / 'updates/completed-notice.json',
                dict(version=version, previous=current_version,
                     notes=update_notes(current_version,version,manifest.get('notes',''),
                                        manifest.get('releases'))[:32768], acknowledged=False))


class UpdateCompletion:
    def __init__(self, base, version, *, updated=False, notes=''):
        version_tuple(version)
        self.path = Path(base) / 'updates/completed-notice.json'
        self.value = None
        try:
            if self.path.exists():
                if self.path.is_symlink() or self.path.stat().st_size > MAX_NOTICE_BYTES:
                    raise ValueError('更新完成记录无效。')
                value = json.loads(self.path.read_text(encoding='utf8'))
                if (not isinstance(value, dict) or not isinstance(value.get('notes'), str)
                        or type(value.get('acknowledged')) is not bool):
                    raise ValueError('更新完成记录格式无效。')
                version_tuple(value.get('version'))
                if value['version'] == version:
                    self.value = value
        except (OSError,ValueError,TypeError) as error:
            logging.getLogger(__name__).warning('Cannot read completed-update notice: %s',type(error).__name__)
        if updated and self.value is None:
            self.value = dict(version=version, previous='', notes=notes[:32768], acknowledged=False)
            atomic_json(self.path, self.value)

    @property
    def pending(self):
        return self.value is not None and not self.value['acknowledged']

    def acknowledge(self):
        if self.value is None:
            return
        value = dict(self.value, acknowledged=True)
        atomic_json(self.path, value)
        self.value = value
