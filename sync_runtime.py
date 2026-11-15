"""Own automatic cloud replication without depending on any shelf widget."""
from account_service import AccountService
from account_store import SessionStore
from clipboard_sync import ClipboardSync
from cloud_config import DEFAULT_CLOUD_URL
from cloud_events import CloudEvents
from shelf_sync import ShelfSync
from sync_contracts import ShelfStatePort
from sync_scheduler import SyncScheduler
from transfer_priority import set_clipboard_priority


class CloudSyncRuntime:
    def __init__(self, base, app, adapter: ShelfStatePort, *, cocoa_smoke=False,
                 clipboard_enabled=True, upload_enabled=True, receive_enabled=True):
        self.closed = False
        if cocoa_smoke:
            self.account = AccountService(base, store=SessionStore(base, DEFAULT_CLOUD_URL, enabled=False))
        else:
            self.account = AccountService(base)
        self.account.api.diagnostics_dir = base
        self.scheduler = SyncScheduler(app, connected=False)
        self.shelf = ShelfSync(base, self.account, adapter, scheduler=self.scheduler)
        self.shelf.set_directions(upload_enabled, receive_enabled)
        self.events = CloudEvents(self.account, app,
            enabled=not cocoa_smoke and app.platformName() not in ('offscreen', 'minimal'))
        self.events.content_changed.connect(self.shelf.request_refresh)
        self.events.transfer_progress.connect(self.shelf.remote_progress)
        self.events.devices_changed.connect(self.account.list_devices)
        self.clipboard = ClipboardSync(self.account, self.events, base, app,
            enabled=clipboard_enabled, scheduler=self.scheduler)
        self.shelf.attach_clipboard(self.clipboard)
        self.clipboard.activity_changed.connect(self.shelf.set_clipboard_priority)
        self.clipboard.activity_changed.connect(set_clipboard_priority)

    def close(self):
        if self.closed:
            return
        self.closed = True
        self.shelf.close()
        self.clipboard.close()
        self.events.close()
        self.scheduler.close()
        self.account.close()
