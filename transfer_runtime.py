"""Own the existing manual LAN/WAN services, without automatic sync or views."""
from lan_account import LanAccountAuthorizer
from lan_transfer import LanTransferService
from wan_transfer import WanTransferService


class ManualTransferRuntime:
    def __init__(self, base, app, account, *, enabled=True):
        self._app = app
        self.account = account
        self.enabled = enabled
        self.started = False
        self.closed = False
        self.lan = LanTransferService(app, device_name=account.device_name, diagnostics_base=base)
        self.wan = WanTransferService(base, app, device_name=account.device_name, enabled=enabled)
        self.wan.set_signaling_allowed(bool(account.signed_in))
        self.wan.attach_account(account, lan_id=self.lan.device_id, nearby_peers=self.lan.peers)
        self.authorizer = LanAccountAuthorizer(self.lan, self.wan)
        self.lan.peers_changed.connect(self._nearby_changed)
        account.account_changed.connect(self._account_changed)
        app.aboutToQuit.connect(self.close)

    def _nearby_changed(self, _peers):
        self.wan.peers_changed.emit(self.wan.peers())

    def _account_changed(self,*_):
        self.wan.set_signaling_allowed(bool(self.account.signed_in))
        if self.started and self.account.signed_in:self.wan.start()

    def enable_cloud_signaling(self):
        """Explicit choice for existing anonymous code/relay functionality."""
        if self.closed:return
        self.wan.set_signaling_allowed(True);self.wan.start()

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
        self.account.account_changed.disconnect(self._account_changed)
        self.lan.close()
        self.wan.close()
