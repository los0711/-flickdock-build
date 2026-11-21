"""Coalesced preference commits on one disk lane; no widgets on the worker."""
import logging
import threading
import time
from PySide6.QtCore import QObject,Signal
from PySide6.QtWidgets import QApplication
from shiboken6 import isValid


class PreferenceWriter(QObject):
    completed=Signal(object,str)

    def __init__(self,store):
        super().__init__(store)
        self.store=store
        self._condition=threading.Condition()
        self._pending={}
        self._active=False
        self._closed=False
        self._thread=None
        self._due=0.
        app=QApplication.instance()
        if app:app.aboutToQuit.connect(self.close)

    @property
    def busy(self):
        with self._condition:return bool(self._pending or self._active)

    def request(self,**values):
        if not values:return
        with self._condition:
            if self._closed:raise RuntimeError('设置服务已关闭。')
            self._pending.update(values)
            self._due=time.monotonic()+.04
            if self._thread is None:
                self._thread=threading.Thread(target=self._run,name='FlickDock preferences',daemon=True)
                self._thread.start()
            self._condition.notify_all()

    def flush(self):
        with self._condition:self._due=0.;self._condition.notify_all()

    def close(self):
        with self._condition:
            self._closed=True;self._due=0.;self._condition.notify_all();thread=self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(1.)
            if thread.is_alive():logging.error('Preference disk worker exceeded shutdown deadline')

    def _run(self):
        while True:
            with self._condition:
                if not self._pending:
                    self._thread=None
                    return
                remaining=self._due-time.monotonic()
                if remaining>0 and not self._closed:
                    self._condition.wait(remaining)
                    continue
                values,self._pending=self._pending,{}
                self._active=True
            error=''
            try:self.store.update(**values)
            except (OSError,ValueError,RuntimeError) as failure:error=str(failure) or '设置未能保存，请重试。'
            except Exception as failure:
                logging.exception('Unexpected preference commit failure; fields=%s',sorted(values))
                error=str(failure) or '设置未能保存，请重试。'
            finally:
                with self._condition:self._active=False
            if isValid(self):self.completed.emit(tuple(values),error)
