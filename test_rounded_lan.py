"""Composited transfer-page edge checks at supported physical scale factors."""
import os
from pathlib import Path
import subprocess
import sys
import unittest


class TransferSurfacePixels(unittest.TestCase):
    def test_transfer_page_on_all_supported_scales(self):
        for scale in ('1','1.25','1.5','1.75','2'):
            with self.subTest(scale=scale):
                env=dict(os.environ,QT_QPA_PLATFORM='offscreen',QT_SCALE_FACTOR=scale,PYTHONUTF8='1')
                run=subprocess.run([sys.executable,str(Path(__file__).resolve()),'--probe',scale],
                    cwd=Path(__file__).parent,env=env,capture_output=True,text=True,encoding='utf-8',
                    errors='replace',timeout=30)
                self.assertEqual(run.returncode,0,run.stdout+'\n'+run.stderr)


def _probe(scale):
    import tempfile
    from PySide6.QtCore import QPoint,Signal
    from PySide6.QtGui import QColor
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QPushButton
    from app_settings import SettingsStore
    from lan_panel import LanDialog
    from native_ui_common import colors,qss_color
    from test_html_native_lan import APP,FakeLan
    import shelf_ui_common,ui_theme

    import tempfile
    try:
        root=Path(tempfile.mkdtemp(prefix='flickdock-rounded-'))
    except OSError:
        root=Path(__file__).with_name('diagnostics')/'rounded-pages'
    output=root/'transfer'/scale
    output.mkdir(parents=True,exist_ok=True);APP.setQuitOnLastWindowClosed(False)
    def verify(image,dialog,widget,edge,theme):
        assert widget is not None and widget.isVisible(),(theme,scale,widget)
        dpr=image.devicePixelRatioF();point=widget.mapTo(dialog,QPoint());x,y=point.x(),point.y();w,h=widget.width(),widget.height()
        target=qss_color(edge);cx=round((x+w/2)*dpr);cy=round((y+h/2)*dpr)
        edges=((cx,round(y*dpr),0,1),(cx,round((y+h)*dpr),0,1),
               (round(x*dpr),cy,1,0),(round((x+w)*dpr),cy,1,0))
        for px,py,dx,dy in edges:
            samples=[image.pixelColor(px+dx*n,py+dy*n) for n in range(-3,4)]
            first,last=samples[0],samples[-1];alpha=target.alphaF()
            expected=tuple(round((a+b)/2*(1-alpha)+line*alpha)
                           for a,b,line in zip(first.getRgb()[:3],last.getRgb()[:3],target.getRgb()[:3]))
            matches=[value for value in samples if max(abs(a-b) for a,b in zip(value.getRgb()[:3],expected))<=3]
            assert len(matches)==1,(theme,scale,widget.objectName(),[v.name() for v in samples],edge,expected)

    class FakeWan(FakeLan):
        mode='wan';identity_changed=Signal(dict)
        def code_identity(self):return dict(code='K9X2',online=True,refresh_remaining=120,refresh_duration=240)
        def check_code(self):pass

    with tempfile.TemporaryDirectory(prefix='flickdock-aa-transfer-') as root:
        base=Path(root);dialog=LanDialog(FakeLan(),SettingsStore(base/'profile'))
        dialog.chrome.prepare_open=lambda:None
        try:
            panel=dialog.panel;source=base/'item.zip';source.write_bytes(b'fixture');panel.queue_paths([str(source)])
            panel._history=[dict(when='11-15 14:43',name='report.zip',peer='Mac',state='已取消',direction='send')]
            panel._render_history()
            panel.show_offer(dict(id='offer',peer='Mac Studio',count=1,names=['demo.zip'],total=2048,code='964191'))
            dialog.show();QTest.qWait(100)
            for theme in ('dark','light'):
                ui_theme.set_theme(theme);ui_theme.apply_application(APP,shelf_ui_common.FONT_FAMILY)
                dialog.apply_theme(theme);dialog.close_button.setFocus();QTest.qWait(80)
                image=dialog.grab().toImage();c=colors(theme)
                cases=((panel.queue_area,c['border']),(panel.history_rows[0],c['border']),
                       (panel.devices,c['input_border']),
                       (panel._rows['offer']['extra'].findChild(QPushButton,'lanOfferReject'),c['border']))
                for widget,edge in cases:verify(image,dialog,widget,edge,theme)
                assert image.save(str(output/(theme+'.png')))
        finally:dialog.close();dialog.deleteLater();APP.processEvents()

        remote=LanDialog(FakeWan(),SettingsStore(base/'remote-profile'));remote.chrome.prepare_open=lambda:None
        try:
            panel=remote.panel;panel.queue_paths([str(source)]);panel.peer_code_input.setText('K9X2')
            remote.show();QTest.qWait(100)
            for theme in ('dark','light'):
                ui_theme.set_theme(theme);ui_theme.apply_application(APP,shelf_ui_common.FONT_FAMILY)
                remote.apply_theme(theme);remote.close_button.setFocus();QTest.qWait(80)
                image=remote.grab().toImage();c=colors(theme)
                for widget,edge in ((panel.queue_area,c['border']),(panel.devices,c['input_border']),
                                    (panel.peer_code_input,c['input_border'])):
                    verify(image,remote,widget,edge,theme)
                assert image.save(str(output/(theme+'-remote.png')))
        finally:remote.close();remote.deleteLater();APP.processEvents()


if __name__=='__main__':
    if sys.argv[1:2]==['--probe']:_probe(sys.argv[2])
    else:unittest.main()
