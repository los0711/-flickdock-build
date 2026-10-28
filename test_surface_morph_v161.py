"""Actual shelf expand/collapse regression at 125% DPI, with no desktop UI."""
import json
import os
from pathlib import Path
import tempfile
import time
from unittest.mock import patch

os.environ['QT_QPA_PLATFORM']='offscreen'
os.environ['QT_SCALE_FACTOR']='1.25'
from PySide6.QtCore import QCoreApplication,QEvent,QPoint,QRect,qInstallMessageHandler
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication,QSystemTrayIcon

messages=[]
qInstallMessageHandler(lambda kind,context,text:messages.append(text))

class Screen:
    def availableGeometry(self):return QRect(0,0,1920,1080)
    def devicePixelRatio(self):return 1.25

def flush(app):
    QCoreApplication.sendPostedEvents(None,QEvent.Type.DeferredDelete)
    app.processEvents()

def wait_until(app,predicate,limit=1800):
    started=time.perf_counter()
    while not predicate() and (time.perf_counter()-started)*1000<limit:
        flush(app);QTest.qWait(5)
    flush(app)
    assert predicate(),'state failed to settle'

with tempfile.TemporaryDirectory(prefix='flickdock-morph-test-') as tmp:
    os.environ['APPDATA']=tmp
    import flickdock as mod,app_settings
    app=QApplication([]);mod.load_font(app)
    with patch.object(mod,'motion_enabled',return_value=True),patch.object(QSystemTrayIcon,'show'),patch.object(QSystemTrayIcon,'hide'),patch.object(app_settings,'_read_startup_value'),patch.object(app_settings,'_write_startup_value'),patch.object(mod.QGuiApplication,'screenAt',return_value=Screen()),patch.object(mod.QGuiApplication,'primaryScreen',return_value=Screen()):
        manager=mod.ShelfManager(app);manager.timer.stop()
        shelf=manager.shelf;shelf.pinned=True
        manager.settings.update(view_mode='icons')
        refs=[shelf.content.add_text(f'Native content item {i}: a readable text preview') for i in range(125)]
        shelf.add_paths('permanent',refs)
        shelf._show_at(QPoint(1400,160),'manual',QPoint())
        QTest.qWait(650);flush(app)
        right=shelf.visual_geometry().x()+shelf.visual_geometry().width()
        shelf.show_contents()
        popup=shelf.popup;snapshot=shelf._surface_snapshot
        assert len(popup.visible_refs())==125 and popup.count_label.text()=='125 项'
        assert not shelf.card.isHidden() and snapshot.active and snapshot.isHidden()
        size=shelf.card.size();captures=snapshot.capture_count
        shelf.geometry_morph.animation.pause()
        for moment in (25,65,100,145,190):
            shelf.geometry_morph.animation.setCurrentTime(moment);flush(app)
            visual=shelf.visual_geometry()
            assert shelf.card.size()==size,'descendant layout resized on a frame'
            assert visual.x()+visual.width()==right,'right edge drifted'
            assert snapshot.capture_count==captures,'live tree captured on a frame'
            # The reference overview keeps three native controls at its
            # right edge. Cloud, settings and LAN remain in its sidebar.
            for index,button in enumerate((shelf.pin_button,shelf.expand_button,shelf.close_button)):
                assert button.parentWidget() is shelf and button.isVisible()
                assert button.width()==28 and button.height()==28
                assert abs(button.mapToGlobal(QPoint()).x()-(right-126+index*34))<=1
        shelf.grab().save(str(Path(__file__).parent/'morph-v161-mid-125.png'))
        shelf.geometry_morph.complete();flush(app)
        wait_until(app,lambda:len(popup.rows_body.findChildren(mod.IconItem))==125)
        assert not shelf.card.isHidden() and not snapshot.active
        assert all(button.parentWidget() is shelf for button in (shelf.pin_button,shelf.expand_button,shelf.menu_button,shelf.close_button))
        epoch=popup._build_epoch
        widgets={id(item) for item in popup.rows_body.findChildren(mod.IconItem)}
        shelf.collapse_contents();shelf.geometry_morph.complete()
        shelf.show_contents();shelf.geometry_morph.complete();flush(app)
        assert popup._build_epoch==epoch,'unchanged warm open reconstructed content'
        assert widgets=={id(item) for item in popup.rows_body.findChildren(mod.IconItem)}

        # Category state is reset on the public expansion entry every time.
        popup.set_category('images');flush(app)
        assert not popup.visible_refs()
        shelf.collapse_contents();shelf.geometry_morph.complete()
        shelf.show_contents();shelf.geometry_morph.complete();flush(app)
        assert popup.category=='all' and popup.category_buttons['all'].isChecked()
        assert len(popup.visible_refs())==125
        wait_until(app,lambda:len(popup.rows_body.findChildren(mod.IconItem))==125)

        # Live header clicks can reverse an in-flight transition repeatedly.
        for _ in range(4):
            before=shelf.visual_geometry()
            shelf.expand_button.click()
            assert shelf.visual_geometry()==before,'reversal teleported to the old endpoint'
            shelf.geometry_morph.animation.pause()
            shelf.geometry_morph.animation.setCurrentTime(45);flush(app)
        shelf.geometry_morph.complete();flush(app)
        assert shelf.expanded and not snapshot.active and not shelf.card.isHidden()

        # Completion is safe before a child confirmation is opened and occurs once.
        completions=[]
        shelf.geometry_morph.animate(shelf.visual_geometry().translated(-8,0),lambda:completions.append(1))
        shelf.geometry_morph.finish();shelf.geometry_morph.finish()
        assert completions==[1] and not snapshot.active
        shelf.setProperty('flickdockConfirmationActive',True)
        state=(shelf.expanded,QRect(shelf.visual_geometry()),snapshot.capture_count)
        shelf.toggle_contents();shelf.show_contents();shelf.collapse_contents()
        assert state==(shelf.expanded,QRect(shelf.visual_geometry()),snapshot.capture_count)
        shelf.setProperty('flickdockConfirmationActive',False)

        # A drop resolving while the live card is hidden appears on restoration.
        shelf.collapse_contents();shelf.geometry_morph.complete()
        shelf.show_contents()
        new_ref=shelf.content.add_text('Resolved during the expand transition')
        shelf.add_paths('permanent',[new_ref]);shelf.geometry_morph.complete();flush(app)
        assert len(popup.visible_refs())==126 and popup.count_label.text()=='126 项'
        wait_until(app,lambda:len(popup.rows_body.findChildren(mod.IconItem))==126)
        shelf.grab().save(str(Path(__file__).parent/'morph-v161-expanded-125.png'))

        # Shake shelf hides every header button, including pin; restore stays clean.
        shelf.hide_immediately()
        shelf._show_at(QPoint(1400,160),'drag',QPoint())
        assert all(button.isHidden() for button in (shelf.pin_button,shelf.expand_button,shelf.menu_button,shelf.close_button))
        shelf.hide_immediately()
        shelf._show_at(QPoint(1400,160),'manual',QPoint())
        assert all(button.isVisible() for button in (shelf.pin_button,shelf.expand_button,shelf.menu_button,shelf.close_button))
        assert not [message for message in messages if 'QPainter' in message or 'paint device' in message],messages
        print('SURFACE_MORPH_V161_OK',json.dumps({'dpr':shelf.devicePixelRatioF(),'refs':len(popup.visible_refs()),'warnings':messages}))
        shelf.hide_immediately();manager.quit()
