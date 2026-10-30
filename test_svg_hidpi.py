import os,sys,tempfile,hashlib,json
from pathlib import Path
os.environ['QT_QPA_PLATFORM']='offscreen'
from PySide6.QtCore import Qt,QSize,QRect,QRectF,QTimer,qInstallMessageHandler
from PySide6.QtGui import QColor,QFont,QFontDatabase,QImage,QPainter,QPixmap
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QApplication,QFrame,QLabel,QWidget
from flickdock_icons import app_icon,glyph_pixmap,glyph_svg,document_pixmap,file_pixmap,export_svg_family,draw_glyph
from flickdock_motion import MotionShadow,FeedbackButton
warnings=[]
qInstallMessageHandler(lambda kind,context,text:warnings.append(text))
app=QApplication([])
for font in [r'C:\Windows\Fonts\PingFang Regular.ttf',r'C:\Windows\Fonts\NotoSansSC-VF.ttf']:
    if Path(font).exists(): QFontDatabase.addApplicationFont(font)
app.setFont(QFont('PingFang SC',10))
ratio=app.primaryScreen().devicePixelRatio()
root=Path(__file__).parent

def bytes_of(pm):
    return bytes(pm.toImage().convertToFormat(QImage.Format.Format_ARGB32).constBits())
def alpha_count(pm):
    return sum(a>10 for a in bytes_of(pm)[3::4])

with tempfile.TemporaryDirectory() as tmp:
    folder=Path(tmp)
    sample=folder/'vector.svg'; sample.write_text('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><rect x="4" y="4" width="56" height="56" rx="8" fill="#087eff"/><path d="m18 33 10 10 20-23" fill="none" stroke="white" stroke-width="4"/></svg>')
    specs=['mp4','mp3','txt','png','pdf','docx','xlsx','pptx','zip','py','otf','lnk','unknown']
    for ext in specs:(folder/f'item.{ext}').write_bytes(b'preview fallback must exist')
    for dpr in [1,1.25,1.5,2]:
        pm=app_icon().pixmap(QSize(32,32),dpr)
        assert pm.size()==QSize(round(32*dpr),round(32*dpr)) and pm.devicePixelRatio()==dpr
        hashes=set()
        for ext in specs:
            pm=file_pixmap(str(folder/f'item.{ext}'),64,dpr=dpr)
            assert pm.size()==QSize(round(64*dpr),round(64*dpr)) and pm.devicePixelRatio()==dpr,(ext,dpr,pm.size(),pm.devicePixelRatio())
            assert alpha_count(pm)>64*64*dpr*dpr*.08,(ext,dpr,'blank')
            if ext not in ['lnk','unknown']:hashes.add(hashlib.sha256(bytes_of(pm)).hexdigest())
        assert len(hashes)>=10,'common category icons should have distinct symbols'
        assert bytes_of(file_pixmap(str(folder/'item.mp4'),64,dpr=dpr))==bytes_of(document_pixmap('mp4',64,dpr=dpr))
        vector=file_pixmap(str(sample),128,dpr=dpr)
        assert vector.width()==round(128*dpr) and alpha_count(vector)>128*128*dpr*dpr*.4
        for name in ['close','more','plus','pin','pin-filled','play','pause','stop','volume','volume-off']:
            glyph=glyph_pixmap(name,24,dpr=dpr)
            assert glyph.width()==round(24*dpr) and glyph.devicePixelRatio()==dpr
            assert alpha_count(glyph)>8
    assert bytes_of(glyph_pixmap('close',24,'#000000',dpr=1.25))!=bytes_of(glyph_pixmap('close',24,'#ffffff',dpr=1.25))
    exported=folder/'exported-icons'
    export_svg_family(exported)
    assert all(QSvgRenderer(str(p)).isValid() for p in exported.glob('*.svg'))
    assert all(QSvgRenderer(str(p)).isValid() for p in (root/'assets/icons').glob('*.svg'))

    sheet=QPixmap(round(900*ratio),round(430*ratio));sheet.setDevicePixelRatio(ratio);sheet.fill(QColor('#f5f7fa'));p=QPainter(sheet)
    p.setPen(QColor('#253348'));p.setFont(QFont('PingFang SC',13));p.drawText(24,32,f'SVG family · {round(ratio*100)}% display scaling')
    for i,name in enumerate(['more','close','plus','pin','pin-filled','play','pause','stop','volume','expand','dock','copy','trash']):
        x=25+i*64;draw_glyph(p,name,QRectF(x,58,28,28),'#425a79',1.8)
    for i,ext in enumerate(specs+['svg']):
        x=22+(i%7)*123;y=125+(i//7)*140
        pm=file_pixmap(str(sample if ext=='svg' else folder/f'item.{ext}'),72,dpr=ratio)
        p.drawPixmap(QRectF(x,y,72,72),pm,QRectF(pm.rect()))
        p.setFont(QFont('PingFang SC',10));p.drawText(QRectF(x,y+78,80,20),Qt.AlignmentFlag.AlignCenter,ext.upper())
    p.end(); sheet.save(str(root/f'svg-icons-{round(ratio*100)}.png'))

    def make_root(effect=False,scale=1):
        w=QWidget();w.resize(320,160);w.setStyleSheet('background:#f6f7fa')
        c=QFrame(w);c.setGeometry(32,24,220,100);c.setStyleSheet('QFrame{background:white;border:1px solid #e1e5ec;border-radius:20px;}')
        label=QLabel('FlickDock · sharp at rest',c);label.setGeometry(14,12,190,22);label.setStyleSheet('background:transparent;border:0;color:#253348')
        for i,name in enumerate(['close','more','pin','play']):b=FeedbackButton(name,c);b.move(12+i*46,48)
        if effect:
            e=MotionShadow(c,alpha=35);c.setGraphicsEffect(e);e.set_motion(sx=scale,sy=scale)
        w.show();return w
    plain=make_root();effect=make_root(True);scaled=make_root(True,.76)
    def complete():
        a=plain.grab();b=effect.grab();c=scaled.grab()
        # At rest the source is drawn directly: the control's physical pixels
        # match a card without an effect. Its shadow is outside this crop.
        crop=QRect(round((32+12)*ratio),round((24+48)*ratio),round(174*ratio),round(36*ratio))
        aa=a.toImage().copy(crop);bb=b.toImage().copy(crop)
        av=bytes(aa.constBits());bv=bytes(bb.constBits())
        mean=sum(abs(x-y) for x,y in zip(av,bv))/len(av)
        assert mean<2,(ratio,'effect settled source blurred',mean)
        assert bytes_of(b)!=bytes_of(c),'scaled presentation must differ'
        b.save(str(root/f'svg-effect-{round(ratio*100)}.png'))
        assert not [x for x in warnings if 'QPainter' in x or 'Invalid SVG' in x],warnings
        print('SVG_HIDPI_OK',json.dumps({'scale':ratio,'settled_mean_pixel_delta':round(mean,5),'svg_assets':len(list((root/'assets/icons').glob('*.svg'))),'warnings':warnings}))
        app.quit()
    QTimer.singleShot(100,complete)
    QTimer.singleShot(3000,app.quit)
    app.exec()
