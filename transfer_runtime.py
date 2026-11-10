"""Own the existing manual LAN/WAN services, without automatic sync or views."""
from lan_account import LanAccountAuthorizer
from lan_transfer import LanTransferService
from wan_transfer import WanTransferService


class ManualTransferRuntime:
    def __init__(self, base, app, account, *, enabled=True):
        self._app = app
        self.enabled = enabled
        self.started = False
        self.closed = False
        self.lan = LanTransferService(app, device_name=account.device_name)
        self.wan = WanTransferService(base, app, device_name=account.device_name, enabled=enabled)
        self.wan.attach_account(account, lan_id=self.lan.device_id, nearby_peers=self.lan.peers)
        self.authorizer = LanAccountAuthorizer(self.lan, self.wan)
        self.lan.peers_changed.connect(self._nearby_changed)
        app.aboutToQuit.connect(self.close)

    def _nearby_changed(self, _peers):
        self.wan.peers_changed.emit(self.wan.peers())

    def start(self):
        if self.closed or self.started:
            return
        self.started = True
        # Install the application's offer handlers before listeners can deliver.
        self.wan.start()
        if self.enabled:
            self.lan.start()

    def close(self):
        if self.closed:
            return
        self.closed = True
        self._app.aboutToQuit.disconnect(self.close)
        self.lan.close()
        self.wan.close()
