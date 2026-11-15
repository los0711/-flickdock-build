def thumbnail(shelf,ref,size=40):
    pm=QPixmap(size*2,size*2);pm.setDevicePixelRatio(2);pm.fill(Qt.transparent)
    p=QPainter(pm);p.setRenderHints(QPainter.Antialiasing|QPainter.SmoothPixmapTransform)
    rect=QRectF(.5,.5,size-1,size-1);category=shelf.content.category(ref)
    backgrounds={'images':'#26364A','video':'#202344','audio':'#7E22CE','text':'#4A3D28','links':'#164354'}
    ext=Path(shelf.content.title(ref)).suffix.lower()
    bg=backgrounds.get(category,'#3B3B3F' if ext=='.zip' else '#593140' if ext=='.pdf' else '#263A55')
    p.setPen(QPen(QColor(255,255,255,38),1));p.setBrush(QColor(bg));p.drawRoundedRect(rect,size*.25,size*.25)
    cached=getattr(shelf,'preview_cache',{}).get(ref)
    actual=category=='images' or ext in ('.lnk','.exe','.app') or cached is not None and not cached.isNull()
    if actual:
        provider=getattr(shelf,'item_pixmap',shelf.content.item_pixmap)
        art=provider(ref,round(size),dpr=2)
        cache=getattr(shelf,'image_cache',None)
        decoded=cache.image(ref,128) if cache and category=='images' else cached
        if decoded is not None and not decoded.isNull():
            art=QPixmap.fromImage(decoded) if isinstance(decoded,QImage) else decoded
        clip=QPainterPath();clip.addRoundedRect(rect,9,9);p.setClipPath(clip)
        scale=min((size-2)/max(1,art.width()),(size-2)/max(1,art.height()))
        width,height=art.width()*scale,art.height()*scale
        p.drawPixmap(QRectF((size-width)/2,(size-height)/2,width,height),art,QRectF(art.rect()));p.setClipping(False)
    elif category=='text':
        p.setPen(Qt.NoPen)
        for i in range(4):
            p.setBrush(QColor('#FBBF24' if i==0 else '#8F897B'));p.drawRoundedRect(QRectF(size*.23,size*.22+i*size*.18,size*(.5 if i!=2 else .36),size*.07),1,1)
    elif category=='audio':
        p.setPen(QPen(QColor('white'),2))
        for i,h in enumerate((.25,.48,.65,.4,.55,.24)):
            x=size*.22+i*size*.11;p.drawLine(QPointF(x,size*(.5-h/2)),QPointF(x,size*(.5+h/2)))
    elif category in ('video','links'):
        glyph='play' if category=='video' else 'web'
        if category=='video':
            p.setPen(Qt.NoPen);p.setBrush(QColor(255,255,255,42));p.drawEllipse(QRectF(size*.22,size*.22,size*.56,size*.56))
        icon(glyph,'#D8E1F4' if category=='video' else '#64D2FF').paint(p,QRect(round(size*.26),round(size*.26),round(size*.48),round(size*.48)))
    else:
        cache=getattr(shelf,'file_metadata',None);info=cache.request(ref) if cache is not None and not ext else None
        name=ext.lstrip('.').upper() or ('DIR' if info and info['is_dir'] or cache is None and Path(ref).is_dir() else 'FILE')
        color='#B52B47' if name=='PDF' else '#466B99' if name=='ZIP' else palette()['action']
        p.setPen(Qt.NoPen);p.setBrush(QColor(color));p.drawRoundedRect(QRectF(size*.12,size*.34,size*.76,size*.33),3,3)
        font=reference_font();font.setPixelSize(max(8,round(size*.23)));set_ui_weight(font,600);p.setFont(font);p.setPen(QColor('white'))
        p.drawText(QRectF(size*.1,size*.3,size*.8,size*.4),Qt.AlignCenter,name[:4])
    p.end();return pm\n