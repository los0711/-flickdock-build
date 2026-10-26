"""Keep cloud replication independent of the native shelf/animation host."""
from __future__ import annotations
import copy


class ShelfAdapter:
    def __init__(self,manager): self.manager = manager

    def snapshot(self):
        shelf = self.manager.shelf; refs = list(shelf.files['permanent'])
        return {'permanent':refs,'texts':shelf.content.export_texts(refs),
                'metadata':copy.deepcopy(shelf.content.export_metadata(refs))}

    def title(self,ref): return self.manager.shelf.content.title(ref)

    def restore(self,view):
        shelf = self.manager.shelf
        refs = list(view.get('permanent',[]))
        old = list(shelf.files['permanent'])
        for ref in old:
            if ref not in refs: shelf.resolver.cancel(ref)
        # In-flight file copies/archives must not deliver another account's
        # contents after a profile switch. This does not touch original files.
        if old != refs:
            shelf._copy_epoch += 1
            for job in tuple(shelf.copy_jobs): job.cancel()
            if shelf.archive_job:
                shelf._archive_epoch += 1; shelf.archive_job.cancel()
        if shelf.popup:
            current = shelf.popup.preview.current_path
            if current not in refs or shelf.content.texts.get(current) != view.get('texts',{}).get(current):
                shelf.popup.preview.media.release_source()
                shelf.popup.preview.image.clear_source()
                shelf.popup.preview.current_path = None
                shelf.popup.preview._pending_preview = False
            shelf.popup.selected.intersection_update(refs)
            if shelf.popup.primary_selection not in refs: shelf.popup.primary_selection = None
        shelf.image_cache.clear(); shelf.preview_cache.clear()
        shelf.content.texts = dict(view.get('texts',{}))
        shelf.content.metadata = copy.deepcopy(view.get('metadata',{}))
        shelf.files['permanent'] = refs
        shelf.refresh(); self.manager.save(); self.manager.update_handles()
