"""One accepted shelf action owns its filesystem preflight and GUI completion."""
from PySide6.QtCore import QObject,QThreadPool,Qt,Signal,Slot
from sync_files import DiskWork
from shelf_intake_limit import prepare_intake

MAX_PENDING_ADMISSIONS=16


class ShelfAdmission(QObject):
    finished=Signal(object,object,object)

    def __init__(self,owner,paths,context):
        super().__init__(owner)
        self.context=context
        self.work=DiskWork(prepare_intake,tuple(paths))
        self.work.signals.done.connect(self._completed,Qt.QueuedConnection)
        self._started=False
        cancel=self.work.cancel
        self.destroyed.connect(lambda *_:cancel.set())

    def start(self):
        if self._started:raise RuntimeError('Shelf admission is one-shot')
        self._started=True;QThreadPool.globalInstance().start(self.work)

    def cancel(self):self.work.cancel.set()

    @Slot(object,object)
    def _completed(self,result,error):
        self.finished.emit(self,result,error)
