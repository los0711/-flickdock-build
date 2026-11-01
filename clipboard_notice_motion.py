"""One finite clock for a bounded, overlapping notification canvas."""
from PySide6.QtCore import QObject
from frame_animation import FrameAnimation as QVariantAnimation
from ui_transitions import exit_curve


class ClipboardNoticeMotion(QObject):
    max_active=3
    step=16.
    rise=6.
    duration=200
    def __init__(self,owner,changed,released,finished):
        super().__init__(owner);self.active=[];self.ghosts=[];self.changed=changed;self.released=released;self.finished=finished
        self.animation=QVariantAnimation(self);self.animation.setDuration(self.duration);self.animation.setEasingCurve(exit_curve())
        self._completion_pending=False
        self.animation.setStartValue(0.);self.animation.setEndValue(1.)
        self.animation.valueChanged.connect(self._frame);self.animation.finished.connect(self._finish)

    def _capture(self):
        self.animation.stop();self._completion_pending=False
        for entry in self.ghosts+self.active:entry['from']=(entry['y'],entry['alpha'])

    def drop_ghosts(self):
        for entry in self.ghosts:self.released(entry)
        self.ghosts=[]

    def append(self,entry,*,animate):
        self._capture()
        if len(self.active)>=self.max_active:
            self.drop_ghosts();self.ghosts.append(self.active.pop(0))
        entry.update(y=len(self.active)*self.step+self.rise,alpha=0.)
        entry['from']=(entry['y'],entry['alpha']);self.active.append(entry);self._start(animate)

    def remove(self,predicate,*,animate):
        self._capture();removed=[entry for entry in self.active if predicate(entry)]
        self.active=[entry for entry in self.active if not predicate(entry)]
        if removed:
            self.drop_ghosts()
            for entry in removed[:-1]:self.released(entry)
            if animate:self.ghosts=removed[-1:]
            else:
                for entry in removed[-1:]:self.released(entry)
        if not animate:
            retained=[]
            for entry in self.ghosts:
                if predicate(entry):self.released(entry)
                else:retained.append(entry)
            self.ghosts=retained
        self._start(animate)

    def _start(self,animate):
        self._completion_pending=True
        for index,entry in enumerate(self.active):entry['to']=(index*self.step,1.)
        for entry in self.ghosts:entry['to']=(entry['y'],0.)
        if not animate:self._frame(1.);self._finish();return
        if not self.active and not self.ghosts:self._finish();return
        self._frame(0.);self.animation.start()

    def _frame(self,value):
        from native_ui_common import reduced_motion
        settle=self.animation.state()==self.animation.State.Running and reduced_motion()
        if settle:self.animation.stop();value=1.
        value=float(value)
        for entry in self.ghosts+self.active:
            origin=entry.get('from',(entry['y'],entry['alpha']));target=entry.get('to',origin)
            entry['y']=origin[0]+(target[0]-origin[0])*value
            entry['alpha']=origin[1]+(target[1]-origin[1])*value
        self.changed()
        if settle:self._finish()

    def _finish(self):
        if not self._completion_pending:return
        self._completion_pending=False
        self.drop_ghosts();self.finished();self.changed()

    def clear(self):
        self.animation.stop();self._completion_pending=False
        for entry in self.ghosts+self.active:self.released(entry)
        self.active=[];self.ghosts=[];self.changed()
