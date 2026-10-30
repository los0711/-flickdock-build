"""Keep cloud replication independent of the native shelf/animation host."""
from __future__ import annotations
import copy


class ShelfAdapter:
    def __init__(self,manager): self.manager = manager

    def snapshot(self):
        shelf = self.manager.shelf; refs = list(shelf.files['permanent'])
        return {'permanent':refs,'texts':shelf.content.export_texts(refs),
                'metadata':copy.deepcopy(shelf.content.export_metadata(refs))}

    def title(self,ref): return self.manager.shelf.content.title(ref,live=False)

    def cancel_pending(self):
        shelf=self.manager.shelf
        shelf._copy_epoch+=1
        for job in tuple(shelf.copy_jobs):job.cancel()
        shelf.content.pending.clear();shelf.content.transfers.clear()

    def remove_owned(self,ref):self.manager.shelf._delete_owned(ref)

    def restore(self,view):
        shelf = self.manager.shelf
        shelf.content.quiet_cloud_refresh=bool(getattr(getattr(self.manager,'sync',None),'catching_up',False))
        refs = list(view.get('permanent',[]))
        old = list(shelf.files['permanent'])
        texts=dict(view.get('texts',{}));metadata=copy.deepcopy(view.get('metadata',{}))
        if old==refs and shelf.content.texts==texts and shelf.content.metadata==metadata:return
        for ref in old:
            if ref not in refs: shelf.resolver.cancel(ref)
        # In-flight file copies/archives must not deliver another account's
        # contents after a profile switch. This does not touch original files.
        if old != refs:
            if shelf.archive_job:
                shelf._archive_epoch += 1; shelf.archive_job.cancel()
        if shelf.popup:
            # The reservation and verified local path describe the same cloud
            # item. Preserve selection across that identity-to-path handoff.
            targets={data.get('_cloud_item_id'):ref for ref,data in metadata.items()
                     if data.get('_cloud_item_id') and ref in refs}
            remap={ref:targets.get(ref.removeprefix('flickdock:transfer:') if shelf.content.is_pending(ref)
                    else shelf.content.metadata.get(ref,{}).get('_cloud_item_id'),ref)
                   for ref in shelf.display_refs()}
            current = shelf.popup.preview.current_path
            preferred=remap.get(current,current)
            shelf.popup.selected={remap.get(ref,ref) for ref in shelf.popup.selected}
            shelf.popup.primary_selection=remap.get(shelf.popup.primary_selection,shelf.popup.primary_selection)
            shelf.popup.hovered_ref=remap.get(shelf.popup.hovered_ref,shelf.popup.hovered_ref)
            if preferred!=current:shelf.popup._preferred_preview=preferred
            valid=set(refs)|set(shelf.content.pending)
            if current not in valid or preferred!=current or shelf.content.texts.get(current) != texts.get(current):
                shelf.popup.preview.media.release_source()
                shelf.popup.preview.image.clear_source()
                shelf.popup.preview.current_path = None
                shelf.popup.preview._pending_preview = False
            shelf.popup.selected.intersection_update(valid)
            if shelf.popup.primary_selection not in valid: shelf.popup.primary_selection = None
        # Preserve unrelated decoded thumbnails across metadata/status changes.
        for ref in set(old)|set(refs):
            if (ref not in refs or shelf.content.metadata.get(ref,{}).get('preview_image')!=metadata.get(ref,{}).get('preview_image')
                    or shelf.content.texts.get(ref)!=texts.get(ref)):
                shelf.image_cache.forget(ref);shelf.preview_cache.pop(ref,None)
        shelf.content.texts = texts
        shelf.content.metadata = metadata
        shelf.files['permanent'] = refs
        shelf.refresh(); self.manager.save(); self.manager.update_handles()
        # Local thumbnail paths from another computer are not portable.
        # Rehydrate web cards on this host without changing the saved payload.
        application=getattr(self.manager,'app',None)
        if application is not None and application.platformName() not in ('offscreen','minimal'):
            shelf.queue_link_previews(refs)
