import os
os.environ['QT_QPA_PLATFORM']='offscreen'
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication
from flickdock_motion import ContentTile
app=QApplication([])
counts={'single':0,'old':0,'new':0,'reduced':0}

def count(name):
    counts[name]+=1

a=ContentTile(); b=ContentTile(); c=ContentTile(motion=False)
a.pulse(finished=lambda:count('single'))
for ms,fn in [(35,lambda:a.set_hover(True)),(70,lambda:a.set_hover(False)),(105,lambda:a.set_pressed(True)),(145,lambda:a.set_pressed(False))]:
    QTimer.singleShot(ms,fn)
b.pulse(finished=lambda:count('old'))
QTimer.singleShot(60,lambda:b.pulse(finished=lambda:count('new')))
QTimer.singleShot(100,lambda:b.set_hover(False))
QTimer.singleShot(150,lambda:b.set_pressed(True))
QTimer.singleShot(185,lambda:b.set_pressed(False))
c.pulse(finished=lambda:count('reduced'))
QTimer.singleShot(25,lambda:c.set_hover(True))
QTimer.singleShot(65,lambda:c.set_pressed(True))
QTimer.singleShot(100,lambda:c.set_pressed(False))
reduced_values=[]
timer=QTimer(); timer.setInterval(10); timer.timeout.connect(lambda:reduced_values.append(dict(c.spring.values))); timer.start()
QTimer.singleShot(1100,app.quit)
app.exec()
assert counts=={'single':1,'old':0,'new':1,'reduced':1},counts
assert all(v['sx']==1 and v['sy']==1 and v['y']==0 for v in reduced_values), reduced_values
assert not a._pulse_active and not b._pulse_active and not c._pulse_active
print('CONTENT_TILE_COMPLETION_OK',counts,'reduced_frames',len(reduced_values))
