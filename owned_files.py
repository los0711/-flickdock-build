"""Deletion is limited to copies whose exact imports owner is recorded."""
from pathlib import Path
import os
import re
import shutil


def owned_copy_spec(ref, metadata, capture_dir):
    if not isinstance(metadata,dict) or metadata.get('capture_mode') != 'copy': return None
    try:
        target = Path(ref).absolute()
        root = Path(metadata.get('owned_root') or capture_dir).absolute()
        batch = target.parent
        if not re.fullmatch(r'capture-[A-Za-z0-9_-]+',batch.name): return None
        if os.path.normcase(str(batch.parent)) != os.path.normcase(str(root/'imports')): return None
        # Never follow a replacement link/junction into another directory.
        for path in (target,batch,batch.parent,root):
            if path.is_symlink() or (hasattr(path,'is_junction') and path.is_junction()): return None
        if target.resolve().parent != batch.resolve() or batch.resolve().parent != (root/'imports').resolve(): return None
        return dict(ref=str(target),metadata=dict(metadata),capture_dir=str(root))
    except (OSError,ValueError,TypeError): return None


def delete_owned_copy(spec,cancel):
    checked = owned_copy_spec(spec['ref'],spec['metadata'],spec['capture_dir'])
    if checked is None or cancel.is_set(): return False
    target = Path(checked['ref'])
    if target.is_dir(): shutil.rmtree(target)
    else: target.unlink(missing_ok=True)
    try: target.parent.rmdir()
    except OSError: pass  # Another item from this batch still owns its bytes.
    return True
