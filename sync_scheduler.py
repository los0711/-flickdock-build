"""Coalesce push/reconnect checks; only active transfers have periodic deadlines."""
from dataclasses import dataclass
import math
import time
from PySide6.QtCore import QObject,QTimer,Qt,Signal
from PySide6.QtNetwork import QAbstractSocket

TRANSFER_INTERVAL_MS=4_000
FINISHING_INTERVAL_MS=1_500
MAX_TIMER_MS=2_147_483_647


@dataclass(frozen=True)
class SyncTiming:
    transfer_ms:int=TRANSFER_INTERVAL_MS
    finishing_ms:int=FINISHING_INTERVAL_MS

    def __post_init__(self):
        for value in (self.transfer_ms,self.finishing_ms):
            if type(value) is not int or not 0<value<=MAX_TIMER_MS:raise ValueError('Invalid sync interval')


@dataclass(frozen=True)
class SyncDecision:
    state:str
    delay_ms:int|None
    immediate:bool=False


def sync_decision(*,kind,enabled,connected,transferring=False,finishing=False,connection_changed=False,timing=SyncTiming()):
    if kind not in ('shelf','clipboard'):raise ValueError('Unknown cloud sync kind')
    if not enabled:return SyncDecision('paused',None)
    if not connected:return SyncDecision('disconnected',None)
    if transferring:return SyncDecision('transferring',timing.finishing_ms if finishing else timing.transfer_ms,connection_changed)
    return SyncDecision('connected',None,connection_changed)


class SyncRegistration:
    def __init__(self,scheduler,kind,callback,timing):
        self.scheduler,self.kind,self.callback,self.timing=scheduler,kind,callback,timing
        self.enabled=False;self.transferring=False;self.finishing=False
        self.periodic_due=None;self.requested_due=None;self.not_before=0.

    @property
    def decision(self):
        return sync_decision(kind=self.kind,enabled=self.enabled,connected=self.scheduler.connected,
            transferring=self.transferring,finishing=self.finishing,timing=self.timing)

    def configure(self,*,enabled,transferring=False,finishing=False):
        before=self.decision
        self.enabled=bool(enabled);self.transferring=bool(transferring);self.finishing=bool(finishing)
        after=self.decision
        if not self.enabled:self.periodic_due=None;self.requested_due=None;self.not_before=0.
        elif after.delay_ms is None:self.periodic_due=None
        elif before!=after or self.periodic_due is None:self.periodic_due=self.scheduler.clock()+after.delay_ms/1000
        self.scheduler._arm()

    def request(self,delay_ms=0):
        if type(delay_ms) is not int or not 0<=delay_ms<=MAX_TIMER_MS:raise ValueError('Invalid check delay')
        if not self.enabled:return
        deadline=self.scheduler.clock()+delay_ms/1000
        self.requested_due=deadline if self.requested_due is None else min(self.requested_due,deadline)
        self.scheduler._arm()

    def defer(self,delay_ms):
        """Honor a server backoff even when change notifications keep arriving."""
        if type(delay_ms) is not int or not 0<delay_ms<=MAX_TIMER_MS:raise ValueError('Invalid retry delay')
        if not self.enabled:return
        self.not_before=max(self.not_before,self.scheduler.clock()+delay_ms/1000)
        self.request(delay_ms)

    def close(self):self.scheduler.unregister(self)


class SyncScheduler(QObject):
    connection_changed=Signal(bool)
    def __init__(self,parent=None,*,connected=True,clock=time.monotonic):
        super().__init__(parent);self.connected=bool(connected);self.clock=clock
        self.registrations={};self.closed=False;self._socket=None
        self.timer=QTimer(self);self.timer.setSingleShot(True);self.timer.setTimerType(Qt.TimerType.PreciseTimer)
        self.timer.timeout.connect(self._run_due)

    def register(self,kind,callback,*,timing=SyncTiming()):
        if self.closed or kind in self.registrations:raise ValueError('Cloud sync registration already exists or scheduler closed')
        sync_decision(kind=kind,enabled=False,connected=self.connected,timing=timing)
        registration=SyncRegistration(self,kind,callback,timing);self.registrations[kind]=registration
        return registration

    def bind_socket(self,socket):
        if self._socket is socket:return
        if self._socket is not None:raise ValueError('A scheduler belongs to one event stream')
        self._socket=socket
        socket.connected.connect(self._connected);socket.disconnected.connect(self._disconnected)
        socket.errorOccurred.connect(self._disconnected)
        self.set_connected(socket.state()==QAbstractSocket.SocketState.ConnectedState)

    def _connected(self):self.set_connected(True)
    def _disconnected(self,*_):self.set_connected(False)

    def set_connected(self,connected):
        connected=bool(connected)
        if self.closed or connected==self.connected:return
        self.connected=connected;now=self.clock()
        for entry in self.registrations.values():
            decision=sync_decision(kind=entry.kind,enabled=entry.enabled,connected=connected,
                transferring=entry.transferring,finishing=entry.finishing,connection_changed=True,timing=entry.timing)
            entry.periodic_due=now+decision.delay_ms/1000 if decision.delay_ms is not None else None
            if decision.immediate:entry.requested_due=now
        self._arm()
        self.connection_changed.emit(connected)

    def unregister(self,entry):
        if self.registrations.get(entry.kind) is entry:
            del self.registrations[entry.kind];entry.configure(enabled=False)

    def _deadline(self,entry):
        if not self.connected:return None
        deadlines=[value for value in (entry.periodic_due,entry.requested_due) if value is not None]
        return max(min(deadlines),entry.not_before) if entry.enabled and deadlines else None

    def _arm(self):
        if self.closed:return
        deadlines=[value for entry in self.registrations.values() if (value:=self._deadline(entry)) is not None]
        if not deadlines:self.timer.stop();return
        delay=max(0,min(MAX_TIMER_MS,math.ceil((min(deadlines)-self.clock())*1000)))
        self.timer.start(delay)

    def _run_due(self):
        if self.closed:return
        now=self.clock()
        due=[entry for entry in self.registrations.values() if (deadline:=self._deadline(entry)) is not None and deadline<=now]
        try:
            for entry in due:
                if not self.connected:break
                if self.registrations.get(entry.kind) is not entry or not entry.enabled:continue
                # Consume the wake before invoking user code: notifications and
                # account changes during a callback belong to the next wake.
                entry.requested_due=None
                delay=entry.decision.delay_ms;entry.periodic_due=now+delay/1000 if delay is not None else None
                entry.callback()
        finally:self._arm()

    def close(self):
        self.closed=True;self.timer.stop()
        if self._socket is not None:
            self._socket.connected.disconnect(self._connected)
            self._socket.disconnected.disconnect(self._disconnected)
            self._socket.errorOccurred.disconnect(self._disconnected)
            self._socket=None
        for entry in self.registrations.values():entry.enabled=False
        self.registrations.clear()
