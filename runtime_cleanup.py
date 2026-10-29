"""Conservative startup cleanup of completed, older updater downloads only.

Unknown files, helper stages, links, current/future versions and all shelf or
clipboard data are retained. An interrupted update can always resume.
"""
import os,re,stat,time
from pathlib import Path
from PySide6.QtCore import QRunnable,QThreadPool

_VERSION=re.compile(r'(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)\Z')
_KNOWN={'setup.exe','update.zip','update.dmg','install.log','update-failure.json'}

def _plain(path,directory=False):
    info=path.lstat()
    return (not stat.S_ISLNK(info.st_mode) and not getattr(info,'st_file_attributes',0)&0x400
            and (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)))

def cleanup_updates(base,current_version,*,now=None):
    removed=0;now=time.time() if now is None else now
    base=Path(base).absolute();root=base/'updates'
    try:
        if base.resolve()!=base or not _plain(base,True) or not _plain(root,True):return 0
        current=tuple(map(int,_VERSION.fullmatch(current_version).groups()))
        for directory in root.iterdir():
            match=_VERSION.fullmatch(directory.name)
            if not match or tuple(map(int,match.groups()))>=current or not _plain(directory,True):continue
            # No recursive removal: a helper stage or unknown file makes the
            # whole version directory ineligible, even if it appears old.
            files=list(directory.iterdir())
            if (not files or any(path.name not in _KNOWN or not _plain(path)
                    or now-path.stat().st_mtime<86400 for path in files)):continue
            for path in files:
                if path.resolve().parent!=directory.resolve() or not _plain(path):continue
                size=path.stat().st_size;path.unlink();removed+=size
            try:directory.rmdir()
            except OSError:pass
    except (OSError,ValueError,AttributeError):pass
    return removed

class _Cleanup(QRunnable):
    def __init__(self,base,version):super().__init__();self.base=base;self.version=version
    def run(self):cleanup_updates(self.base,self.version)

def schedule_cleanup(base,version):
    QThreadPool.globalInstance().start(_Cleanup(base,version),-1)
