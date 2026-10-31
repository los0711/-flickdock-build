"""Original SVG identity, controls, and uniform content icons for FlickDock.

Sizes are logical pixels. QPixmaps carry their DPR; draw them into a logical
QRectF instead of calling .scaled() in a second stage. Control icons can be
painted as vectors with draw_glyph() for zero rasterisation at rest.
"""
from __future__ import annotations

import math
from pathlib import Path
from xml.sax.saxutils import escape

from PySide6.QtCore import QByteArray, QFileInfo, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QGuiApplication, QIcon, QIconEngine, QImage, QPainter, QPainterPath, QPixmap
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QFileIconProvider

_provider: QFileIconProvider | None = None
_IMAGE_EXTENSIONS = {'.png','.jpg','.jpeg','.webp','.gif','.bmp','.ico','.tif','.tiff','.heic','.heif','.avif','.svg','.svgz'}
_CATEGORIES = {
    'audio': {'.mp3','.wav','.flac','.aac','.m4a','.ogg','.wma','.opus','.aiff','.aif','.mid','.midi'},
    'video': {'.mp4','.mov','.m4v','.webm','.avi','.mkv','.wmv','.mpeg','.mpg','.3gp','.ts','.vob'},
    'text': {'.txt','.md','.markdown','.rtf','.log','.csv','.tsv','.ini','.cfg'},
    'document': {'.doc','.docx','.odt','.pages','.epub','.mobi'},
    'sheet': {'.xls','.xlsx','.xlsm','.ods','.numbers'},
    'presentation': {'.ppt','.pptx','.key','.odp'},
    'archive': {'.zip','.7z','.rar','.tar','.gz','.bz2','.xz','.zst','.iso','.cab'},
    'code': {'.py','.js','.jsx','.tsx','.html','.htm','.css','.scss','.json','.xml','.yaml','.yml','.c','.cpp','.h','.cs','.java','.rs','.go','.sql','.sh','.ps1','.bat','.toml'},
    'pdf': {'.pdf'},
    'font': {'.ttf','.otf','.ttc','.woff','.woff2'},
}

# Every control shares a 24-unit grid, rounded joins and the same stroke width.
_GLYPHS = {
    'more':'<circle cx="5" cy="12" r="1.55" fill="{c}" stroke="none"/><circle cx="12" cy="12" r="1.55" fill="{c}" stroke="none"/><circle cx="19" cy="12" r="1.55" fill="{c}" stroke="none"/>',
    'close':'<path d="M6 6l12 12M18 6L6 18"/>',
    'plus':'<path d="M12 5v14M5 12h14"/>',
    'pin':'<path d="M8 4h8l-.5 6 3.5 4H5l3.5-4L8 4zm4 10v7"/>',
    'pin-filled':'<path d="M8 4h8l-.5 6 3.5 4H5l3.5-4L8 4z" fill="{c}" stroke="none"/><path d="M12 14v7"/>',
    'temporary':'<circle cx="12" cy="12" r="8"/><path d="M12 7.5V12l3.5 2"/>',
    'shelf':'<path d="M8 6V4.5A1.5 1.5 0 0 1 9.5 3h5A1.5 1.5 0 0 1 16 4.5V6M5.5 11V7.5A1.5 1.5 0 0 1 7 6h10a1.5 1.5 0 0 1 1.5 1.5V11"/><path d="M3 11h5l2 3h4l2-3h5v7a3 3 0 0 1-3 3H6a3 3 0 0 1-3-3z"/>',
    'chevron':'<path d="m9 6 6 6-6 6"/>',
    'arrow':'<path d="M4 12h13m-6-6 6 6-6 6"/>',
    'chevron-up':'<path d="m6 15 6-6 6 6"/>',
    'chevron-down':'<path d="m6 9 6 6 6-6"/>',
    'expand':'<path d="m14 10 6-6m-6 0h6v6M10 14l-6 6m0-6v6h6"/>',
    'collapse':'<path d="m20 4-6 6m0-6v6h6M4 20l6-6m-6 0h6v6"/>',
    'dock':'<path d="M20 4v16M3.5 12h12m-5-5 5 5-5 5"/>',
    'check':'<path d="m5 12 5 5L19 7"/>',
    'folder':'<path d="M3 8V6a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8z"/>',
    # Lucide monitor-smartphone, file-up and cloud, ISC; see THIRD_PARTY_NOTICES.md.
    'nearby':'<path d="M18 8V6a2 2 0 0 0-2-2H4a2 2 0 0 0-2 2v7a2 2 0 0 0 2 2h8"/><path d="M10 19v-3.96 3.15"/><path d="M7 19h5"/><rect width="6" height="10" x="16" y="12" rx="2"/>',
    'file-up':'<path d="M6 22a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h8a2.4 2.4 0 0 1 1.704.706l3.588 3.588A2.4 2.4 0 0 1 20 8v12a2 2 0 0 1-2 2z"/><path d="M14 2v5a1 1 0 0 0 1 1h5"/><path d="M12 12v6"/><path d="m15 15-3-3-3 3"/>',
    'cloud':'<path d="M17.5 19H9a7 7 0 1 1 6.71-9h1.79a4.5 4.5 0 1 1 0 9Z"/>',
    'user':'<circle cx="12" cy="8" r="3.5"/><path d="M5 20a7 7 0 0 1 14 0"/>',
    'search':'<circle cx="10.5" cy="10.5" r="6.5"/><path d="m15.5 15.5 5 5"/>',
    'list':'<path d="M9 6h12M9 12h12M9 18h12M3 6h1M3 12h1M3 18h1"/>',
    'grid':'<rect x="3" y="3" width="7" height="7" rx="1.2"/><rect x="14" y="3" width="7" height="7" rx="1.2"/><rect x="3" y="14" width="7" height="7" rx="1.2"/><rect x="14" y="14" width="7" height="7" rx="1.2"/>',
    'receive':'<rect x="3" y="11" width="18" height="9" rx="2.5"/><path d="M12 3v11m-4-4 4 4 4-4M7 16h2"/>',
    'file':'<path d="M7 3h7l5 5v12a1 1 0 0 1-1 1H6a1 1 0 0 1-1-1V5a2 2 0 0 1 2-2zm7 0v5h5"/>',
    'text':'<path d="M7 3h7l5 5v12a1 1 0 0 1-1 1H6a1 1 0 0 1-1-1V5a2 2 0 0 1 2-2zm7 0v5h5M8 11h7.5M8 14h7.5M8 17h4"/>',
    'image':'<rect x="3" y="4" width="18" height="16" rx="3"/><circle cx="8" cy="9" r="1.5"/><path d="m4 18 5-5 3 3 4-5 5 6"/>',
    'audio':'<path d="M9 17V6l10-2v11M9 9l10-2"/><ellipse cx="6" cy="17.5" rx="3" ry="2.5"/><ellipse cx="16" cy="15.5" rx="3" ry="2.5"/>',
    'video':'<rect x="3" y="4" width="18" height="16" rx="3"/><path d="m10 8 6 4-6 4z" fill="{c}" stroke="none"/>',
    'play':'<path d="m7 4 14 8L7 20z" fill="{c}" stroke="none"/>',
    'pause':'<rect x="6" y="4" width="4" height="16" rx="1.2" fill="{c}" stroke="none"/><rect x="14" y="4" width="4" height="16" rx="1.2" fill="{c}" stroke="none"/>',
    'stop':'<rect x="5" y="5" width="14" height="14" rx="2" fill="{c}" stroke="none"/>',
    'volume':'<path d="M4 9h4l5-4v14l-5-4H4zM16 8a6 6 0 0 1 0 8m3-11a10 10 0 0 1 0 14"/>',
    'volume-off':'<path d="M4 9h4l5-4v14l-5-4H4zM17 9l5 6m0-6-5 6"/>',
    'copy':'<rect x="8" y="8" width="11.5" height="12" rx="2.5"/><path d="M14.5 4.5H7a2.5 2.5 0 0 0-2.5 2.5v8"/>',
    'trash':'<path d="M4 6h16M9 6V3h6v3M6 6l1 13a2 2 0 0 0 2 2h6a2 2 0 0 0 2-2l1-13M10 10v7m4-7v7"/>',
}

_CATEGORY_SYMBOLS = {
    'generic':'<path d="M24 31h15M24 37h15M24 43h10"/>',
    'text':'<path d="M23 29h18M23 35h18M23 41h18M23 47h12"/>',
    'document':'<path d="M23 29h18M23 35h18M23 41h11"/><path d="M23 47h18" stroke-opacity=".45"/>',
    'audio':'<path d="M28 43V29l13-3v14M28 33l13-3"/><ellipse cx="24.5" cy="43.5" rx="3.5" ry="3"/><ellipse cx="37.5" cy="40.5" rx="3.5" ry="3"/>',
    'video':'<rect x="21" y="28" width="24" height="20" rx="4"/><path d="m30 32 9 6-9 6z" fill="{c}" stroke="none"/>',
    'image':'<rect x="21" y="28" width="24" height="20" rx="4"/><circle cx="27" cy="34" r="2"/><path d="m22 45 7-7 4 4 6-8 5 7"/>',
    'sheet':'<rect x="21" y="27" width="24" height="22" rx="3"/><path d="M21 34h24M29 27v22M37 34v15M21 41h24"/>',
    'presentation':'<path d="M21 28h24v17H21zM33 45v6m-6 0h12"/><path d="M26 40v-5m7 5V31m7 9v-7"/>',
    'archive':'<path d="M29 26h6m-6 4h6m-6 4h6m-6 4h6"/><rect x="28" y="41" width="8" height="9" rx="2"/><path d="M31 45h2"/>',
    'code':'<path d="m27 30-7 8 7 8m12-16 7 8-7 8m-4-18-5 20"/>',
    'pdf':'<path d="M25 47c7-11 12-28 8-23-3 4 3 16 12 19 5 2-13-5-22 1-6 5 2 6 5 1"/>',
    'font':'<path d="m22 48 10-22 10 22M26 40h12"/>',
    'link':'<path d="m29 31 4-4a7 7 0 0 1 10 10l-5 5m-2-8-5 5a7 7 0 0 1-10-10l4-4"/>',
}

_CONTENT_COLORS = {'generic':'#64748b','text':'#6a78bd','document':'#3976d1',
    'audio':'#9557c7','video':'#159cc6','image':'#299b83','sheet':'#258b63',
    'presentation':'#d77935','archive':'#bc8924','code':'#6565c4','pdf':'#d94e60',
    'font':'#ae5fa6','folder':'#d9a238','link':'#487dba'}
_FORMAT_COLORS = {'mp4':'#159cc6','mov':'#288eaa','mkv':'#388ac6','webm':'#258daa',
    'mp3':'#9557c7','wav':'#a05aaf','flac':'#805abd','pdf':'#d94e60',
    'doc':'#487fce','docx':'#3970c8','xls':'#30936a','xlsx':'#25845b',
    'ppt':'#db833c','pptx':'#d07434','zip':'#bc8924','7z':'#aa8a35',
    'rar':'#a26c50','txt':'#6a78bd','md':'#6f72a9','svg':'#cc7b38','svgz':'#b57536'}


def content_color(value: str, variant: int = 0) -> str:
    suffix = (Path(str(value)).suffix or str(value)).lower().lstrip('.')
    base = QColor(_FORMAT_COLORS.get(suffix, _CONTENT_COLORS[_category(str(value))]))
    if variant:
        hue, saturation, lightness, alpha = base.getHslF()
        base.setHslF((hue + variant * .147) % 1, saturation, lightness, alpha)
    return base.name()


def device_pixel_ratio(dpr: float | None = None) -> float:
    if dpr is not None:
        return max(1.0, min(4.0, float(dpr)))
    app = QGuiApplication.instance()
    screen = app.primaryScreen() if app else None
    return float(screen.devicePixelRatio()) if screen else 1.0


def _svg(body: str, viewbox: str = '0 0 24 24') -> str:
    return f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{viewbox}">{body}</svg>'


def glyph_svg(name: str, color: str = '#6e7078', stroke: float = 1.65) -> str:
    name = {'permanent':'pin','hide-to-edge':'dock'}.get(name, name)
    c = escape(QColor(color).name(QColor.NameFormat.HexArgb)[3:])
    c = '#' + c
    body = _GLYPHS.get(name, _GLYPHS['file']).format(c=c)
    return _svg(f'<g fill="none" stroke="{c}" stroke-width="{float(stroke):g}" stroke-linecap="round" stroke-linejoin="round">{body}</g>')


def _render_svg(svg: str, logical_size: int | QSize, dpr: float | None = None) -> QPixmap:
    ratio = device_pixel_ratio(dpr)
    size = QSize(logical_size, logical_size) if isinstance(logical_size, int) else logical_size
    pm = QPixmap(max(1, round(size.width()*ratio)), max(1, round(size.height()*ratio)))
    pm.setDevicePixelRatio(ratio)
    pm.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pm)
    renderer = QSvgRenderer(QByteArray(svg.encode('utf-8')))
    renderer.render(painter, QRectF(0, 0, size.width(), size.height()))
    painter.end()
    return pm


class _SvgIconEngine(QIconEngine):
    def __init__(self, svg: str):
        super().__init__()
        self.svg = svg

    def clone(self):
        return _SvgIconEngine(self.svg)

    def key(self):
        return 'FlickDockSvg'

    def isNull(self):
        return False

    def paint(self, painter, rect, mode, state):
        painter.save()
        if mode == QIcon.Mode.Disabled:
            painter.setOpacity(painter.opacity()*.40)
        QSvgRenderer(QByteArray(self.svg.encode('utf-8'))).render(painter, QRectF(rect))
        painter.restore()

    def pixmap(self, size, mode, state):
        return self.scaledPixmap(size, mode, state, 1.0)

    def scaledPixmap(self, size, mode, state, scale):
        return _render_svg(self.svg, size, scale)


def draw_glyph(painter: QPainter, name: str, rect: QRectF, color: str = '#6e7078', stroke: float = 1.65) -> None:
    """Paint SVG directly into logical coordinates on any DPI-aware painter."""
    QSvgRenderer(QByteArray(glyph_svg(name,color,stroke).encode('utf-8'))).render(painter, rect)


def glyph_pixmap(name: str, size: int = 20, color: str = '#6e7078', stroke: float = 1.65,
                 dpr: float | None = None) -> QPixmap:
    return _render_svg(glyph_svg(name,color,stroke), size, dpr)


def glyph_icon(name: str, size: int = 20, color: str = '#6e7078', dpr: float | None = None) -> QIcon:
    return QIcon(_SvgIconEngine(glyph_svg(name,color)))


def _mark_svg(color: str = '#ffffff') -> str:
    c = escape(QColor(color).name())
    return f'<rect x="20" y="13" width="24" height="22" rx="3.5" fill="{c}" opacity=".65"/><rect x="16" y="19" width="32" height="23" rx="4" fill="{c}" opacity=".86"/><path d="M13 30h9c3 0 3 7 8 7h4c5 0 5-7 8-7h9v12c0 5-3 7-7 7H20c-4 0-7-2-7-7z" fill="{c}"/>'


def app_svg() -> str:
    return _svg('<defs><linearGradient id="blue" x1="0" y1="0" x2="1" y2="1"><stop stop-color="#47a7ff"/><stop offset=".52" stop-color="#1686f6"/><stop offset="1" stop-color="#0964dd"/></linearGradient></defs><rect x="2.5" y="2.5" width="59" height="59" rx="14" fill="url(#blue)" stroke="#0d70df" stroke-width=".5"/><rect x="3.3" y="3.3" width="57.4" height="57.4" rx="13.5" fill="none" stroke="#ffffff" stroke-opacity=".27" stroke-width=".6"/>'+_mark_svg(), '0 0 64 64')


def app_icon(size: int = 64, dpr: float | None = None) -> QIcon:
    return QIcon(_SvgIconEngine(app_svg()))


def tray_icon(size: int = 32, color: str = '#147fe9', dpr: float | None = None) -> QIcon:
    return QIcon(_SvgIconEngine(_svg(_mark_svg(color),'0 0 64 64')))


def menu_bar_icon() -> QIcon:
    value=QIcon(_SvgIconEngine(_svg(_mark_svg('#000000'),'8 7 48 48')))
    value.setIsMask(True)
    return value


def _category(value: str) -> str:
    value = value.lower().strip()
    if value in _CATEGORY_SYMBOLS or value == 'folder':
        return value
    suffix = Path(value).suffix.lower() or ('.'+value.lstrip('.'))
    if suffix in _IMAGE_EXTENSIONS:
        return 'image'
    for category, extensions in _CATEGORIES.items():
        if suffix in extensions:
            return category
    return 'generic'


def document_svg(value: str = '', stack: int = 1) -> str:
    category = _category(value)
    if category == 'folder':
        return _svg('<path d="M8 21v-6a4 4 0 0 1 4-4h14l6 6h19a5 5 0 0 1 5 5v27a5 5 0 0 1-5 5H13a5 5 0 0 1-5-5V21z" fill="#d9a238"/><path d="M8 23h48" stroke="#ffe0a3" stroke-width="1.5"/>','0 0 64 64')
    back = ''
    for i in range(min(max(stack,1),5)-1,0,-1):
        back += f'<rect x="{13-i*1.6}" y="{5+i*2}" width="43" height="51" rx="7" fill="{content_color(value,i)}"/>'
    color = content_color(value)
    top = QColor(color).lighter(110).name()
    raw = str(value).lower().strip()
    suffix = (Path(raw).suffix or raw).lstrip('.')
    labels = {'generic':'FILE','text':'TEXT','document':'DOC','sheet':'XLS','presentation':'PPT',
              'audio':'AUDIO','video':'VIDEO','image':'IMG','archive':'ZIP','code':'CODE','font':'FONT','link':'WEB'}
    label = suffix.upper() if suffix not in _CATEGORY_SYMBOLS and 1 <= len(suffix) <= 4 else labels.get(category,category.upper())
    body = back + f'<defs><linearGradient id="paper" x1="0" y1="0" x2="1" y2="1"><stop stop-color="{top}"/><stop offset="1" stop-color="{color}"/></linearGradient></defs><rect x="13" y="5" width="43" height="51" rx="7" fill="url(#paper)"/><path d="M41 5h8a7 7 0 0 1 7 7v8z" fill="#ffffff" fill-opacity=".23"/><rect x="13.5" y="5.5" width="42" height="50" rx="6.5" fill="none" stroke="#ffffff" stroke-opacity=".18"/>'
    symbol = _CATEGORY_SYMBOLS[category].format(c='#ffffff')
    body += f'<g transform="translate(0,-11)" opacity=".3" fill="none" stroke="#ffffff" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">{symbol}</g>'
    from native_ui_common import reference_font
    family=escape(reference_font().family(),{'"':'&quot;',"'":'&apos;'})
    body += f'<text x="34.5" y="44" text-anchor="middle" font-family="{family},Arial,sans-serif" font-size="{12 if len(label)>3 else 14}" font-weight="600" fill="#ffffff">{escape(label)}</text>'
    return _svg(body,'0 0 64 64')


def document_pixmap(text: str = '', size: int = 90, stack: int = 1, dpr: float | None = None) -> QPixmap:
    """Return exactly ``size`` logical pixels; artwork has a consistent inset."""
    return _render_svg(document_svg(text,stack), size, dpr)


def _alpha_bounds(pm: QPixmap) -> QRectF | None:
    image = pm.toImage().convertToFormat(QImage.Format.Format_ARGB32)
    raw = bytes(image.constBits())
    width,height,stride = image.width(),image.height(),image.bytesPerLine()
    left,top,right,bottom = width,height,-1,-1
    for y in range(height):
        row = raw[y*stride:y*stride+width*4]
        xs = [i for i,a in enumerate(row[3::4]) if a>12]
        if xs:
            top=min(top,y); bottom=y; left=min(left,xs[0]); right=max(right,xs[-1])
    return QRectF(left,top,right-left+1,bottom-top+1) if right>=left else None


def _native_icon(path: Path, physical_size: int) -> QPixmap:
    global _provider
    if _provider is None:
        _provider = QFileIconProvider()
        _provider.setOptions(QFileIconProvider.Option.DontUseCustomDirectoryIcons)
    icon = _provider.icon(QFileInfo(str(path)))
    # Ask the shell engine for its best artwork, not a 16/32px raster later
    # stretched through several widget resizes.
    if icon.isNull():
        return QPixmap()
    requested = QSize(max(64,physical_size),max(64,physical_size))
    native = icon.pixmap(requested,1.0)
    generic = _provider.icon(QFileIconProvider.IconType.File).pixmap(requested,1.0)
    # A stock blank page contributes no actual app/type identity. Replace that
    # shell fallback with the same crisp document family used by known types.
    if not generic.isNull() and native.toImage() == generic.toImage():
        return QPixmap()
    return native


def file_pixmap(path: str, size: int = 90, stack: int = 1, dpr: float | None = None) -> QPixmap:
    """Common types use one SVG family; actual images and app/link icons stay real."""
    source = Path(path)
    ratio = device_pixel_ratio(dpr)
    total = size
    if source.is_dir():
        return document_pixmap('folder',size,stack,ratio)
    category = _category(source.suffix)
    if category == 'image' and source.is_file():
        if source.suffix.lower() in ('.svg','.svgz'):
            vector = QSvgRenderer(str(source))
            if vector.isValid():
                pm = QPixmap(round(total*ratio),round(total*ratio)); pm.setDevicePixelRatio(ratio); pm.fill(Qt.GlobalColor.transparent)
                natural = vector.viewBoxF().size()
                if natural.isEmpty():
                    natural = vector.defaultSize()
                scale = min(size*.84/max(1,natural.width()),size*.84/max(1,natural.height()))
                w,h = natural.width()*scale,natural.height()*scale
                p = QPainter(pm); vector.render(p,QRectF((total-w)/2,(total-h)/2,w,h)); p.end()
                return pm
        image = QPixmap(str(source))
        if not image.isNull():
            pm = QPixmap(round(total*ratio),round(total*ratio)); pm.setDevicePixelRatio(ratio); pm.fill(Qt.GlobalColor.transparent)
            p = QPainter(pm); p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            optical_size = size*.84
            scale = min(optical_size/image.width(),optical_size/image.height())
            w,h = image.width()*scale,image.height()*scale
            rect = QRectF((total-w)/2,(total-h)/2,w,h)
            from continuous_corners import continuous_rect_path
            p.setClipPath(continuous_rect_path(rect,5))
            p.drawPixmap(rect,image,QRectF(image.rect())); p.end()
            return pm
    if category != 'generic':
        return document_pixmap(source.suffix,size,stack,ratio)
    native = _native_icon(source,round(size*ratio))
    bounds = _alpha_bounds(native) if not native.isNull() else None
    if bounds is not None:
        pm = QPixmap(round(total*ratio),round(total*ratio)); pm.setDevicePixelRatio(ratio); pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm); p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        # All shell fallbacks fit one common optical box after alpha trimming.
        optical_size = size*.84
        scale = min(optical_size/bounds.width(),optical_size/bounds.height())
        w,h = bounds.width()*scale,bounds.height()*scale
        p.drawPixmap(QRectF((total-w)/2,(total-h)/2,w,h),native,bounds)
        p.end(); return pm
    return document_pixmap('generic',size,stack,ratio)


def export_svg_family(directory: str | Path) -> None:
    """Write the actual original vector sources for design review/source delivery."""
    directory = Path(directory); directory.mkdir(parents=True,exist_ok=True)
    for name in _GLYPHS:
        accent = '#1876db' if name in ('nearby','file-up','cloud') else '#6e7078'
        (directory/f'control-{name}.svg').write_text(glyph_svg(name,accent),encoding='utf-8')
    for name in (*_CATEGORY_SYMBOLS,'folder'):
        (directory/f'content-{name}.svg').write_text(document_svg(name),encoding='utf-8')
    for name in _FORMAT_COLORS:
        (directory/f'format-{name}.svg').write_text(document_svg(name),encoding='utf-8')
    (directory/'flickdock.svg').write_text(app_svg(),encoding='utf-8')
