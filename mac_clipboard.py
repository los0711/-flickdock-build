"""Read the current Cocoa pasteboard, independently of Qt activation caching."""

IMAGE_TYPES=(('public.png','image/png'),('public.jpeg','image/jpeg'),
             ('org.webmproject.webp','image/webp'),('com.microsoft.bmp','image/bmp'),('public.tiff','image/tiff'))
FILE_TYPES={'public.file-url','NSFilenamesPboardType'}


def read_snapshot():
    from AppKit import NSPasteboard
    from Foundation import NSURL
    board=NSPasteboard.generalPasteboard();types=set(board.types() or [])
    if types&FILE_TYPES:
        urls=board.readObjectsForClasses_options_([NSURL],{'NSPasteboardURLReadingFileURLsOnlyKey':True})
        if not urls:return False,None
        paths=[str(url.path()) for url in urls if url.isFileURL() and url.path()]
        if len(paths)!=len(urls):return False,None
        return True,('files',paths) if len(paths)==1 else None
    for native,mime in IMAGE_TYPES:
        if native not in types:continue
        data=board.dataForType_(native)
        if data:return True,('image',{'raw':bytes(data),'mime':mime})
    if 'public.utf8-plain-text' in types or 'NSStringPboardType' in types:
        text=board.stringForType_('public.utf8-plain-text')
        if text is None:return False,None
        html=board.stringForType_('public.html') if 'public.html' in types else None
        return True,('text',{'text':str(text),'html':str(html) if html is not None else ''}) if text else None
    # An announced image with no readable bytes is delayed, not an empty Copy.
    return not any(native in types for native,_ in IMAGE_TYPES),None
