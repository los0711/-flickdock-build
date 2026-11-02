"""One local body owner for equal verified shelf snapshots.

This is admission state, not a second upload protocol or persistent journal.
Each item still owns its revision, immutable stage and server receipt. A peer
can reuse bytes only after the server really commits the first item.
"""
from dataclasses import dataclass


@dataclass
class _Group:
    owner:dict


class ShelfPayloadGate:
    def __init__(self,owner):
        self.owner=owner;self.groups={};self.waiters={}

    def reset(self):
        self.groups.clear();self.waiters.clear()

    def _key(self,snapshot):
        return (self.owner.account_id,self.owner.epoch,snapshot['size'],snapshot['sha256'])

    def _live(self,snapshot):
        """Metadata identity only; stage/hash verification stays in disk work."""
        state=self.owner.state
        if state is None or state.get('resumable_uploads',{}).get(snapshot['id']) is not snapshot:return False
        entry=state['entries'].get(snapshot['id'])
        return bool(entry and entry.get('dirty') and not entry.get('deleted') and not entry.get('paused_upload')
                    and entry.get('blocked_fingerprint')!=entry.get('fingerprint')
                    and all(entry.get(k)==snapshot.get(k) for k in ('id','ref','seq','fingerprint','name','kind','version')))

    def _promote(self,key):
        group=self.groups.get(key)
        if group is not None and self._live(group.owner):return group
        self.groups.pop(key,None)
        for identity,(candidate_key,snapshot) in tuple(self.waiters.items()):
            if candidate_key!=key:continue
            self.waiters.pop(identity)
            if self._live(snapshot):
                group=_Group(snapshot);self.groups[key]=group;return group
        return None

    def waiting(self,entry):
        row=self.waiters.get(entry['id'])
        if row is None:return False
        key,snapshot=row
        if not self._live(snapshot):
            self.waiters.pop(entry['id']);return False
        group=self._promote(key)
        return group is not None and group.owner is not snapshot

    def admit(self,snapshot):
        """Called only after normal stage or durable-stage verification."""
        assert self._live(snapshot),'Payload admission requires the current staged revision'
        key=self._key(snapshot);group=self._promote(key)
        if group is None:
            # An interrupted reservation is preferred to a fresh duplicate.
            # Its ordinary verification/GET still runs before any body, and
            # invalid journal stages are retired by the existing pipeline.
            reserved=next((row for row in self.owner.state['resumable_uploads'].values()
                           if row is not snapshot and row.get('upload_id')
                           and self._live(row) and self._key(row)==key),None)
            group=_Group(reserved or snapshot);self.groups[key]=group
        if group.owner is snapshot:
            self.waiters.pop(snapshot['id'],None);return True
        if snapshot.get('upload_id'):
            # Preserve recovery of historical independently reserved objects.
            # These accepted bytes must not be discarded or rebound to a new
            # item merely because this process now coalesces new admissions.
            return True
        self.waiters[snapshot['id']]=(key,snapshot);return False

    def release(self,snapshot):
        """Only the exact retired snapshot can release its execution right."""
        row=self.waiters.get(snapshot['id'])
        if row is not None and row[1] is snapshot:self.waiters.pop(snapshot['id'])
        key=self._key(snapshot);group=self.groups.get(key)
        if group is not None and group.owner is snapshot:
            self.groups.pop(key);self._promote(key)
