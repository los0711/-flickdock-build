"""Probe the same cloud route the file client uses; never infer a VPN process."""
from PySide6.QtCore import QObject,QTimer,Signal


class CloudConnectivity(QObject):
    changed=Signal(str)
    environment_changed=Signal(dict)
    def __init__(self,api,parent=None,*,enabled=True):
        super().__init__(parent);self.api=api;self.state='checking';self._key=None;self._closed=False
        from network_environment import NetworkEnvironment
        self.environment=NetworkEnvironment(self,enabled=enabled)
        self.environment.changed.connect(self._environment_changed)
        self.timer=QTimer(self);self.timer.setInterval(12000);self.timer.timeout.connect(self.check)
        if enabled:self.timer.start();QTimer.singleShot(0,self,self.check)

    def _environment_changed(self,state):
        if self._closed:return
        self.environment_changed.emit(state);self.check()

    def _set(self,state):
        if state!=self.state:self.state=state;self.changed.emit(state)

    def check(self):
        if self._closed or self._key is not None:return
        # Set a sentinel before request(): validation errors may complete inline.
        pending=object();self._key=pending
        def done(result,error):
            self._key=None
            if self._closed:return
            self._set('online' if error is None and isinstance(result,dict) and result.get('service')=='flickdock-cloud' else 'offline')
        key=self.api.request('GET','/v1/health',done,authenticated=False,timeout_ms=4500)
        if self._key is pending:self._key=key

    def close(self):
        self._closed=True;self.timer.stop();self.environment.close()
        if self._key is not None:self.api.cancel(self._key)
        self._key=None
