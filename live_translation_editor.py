"""Read or insert text only in a verified, foreground, editable control."""
from contextlib import ExitStack, contextmanager
import ctypes
from dataclasses import dataclass
from functools import cache, lru_cache
from ntpath import basename
import sys
from time import monotonic
from urllib.parse import urlsplit
from uuid import UUID

from translation_document import python_position
from translation_languages import MAX_TEXT_CHARS


@dataclass(frozen=True)
class EditorState:
    identity: tuple
    bounds: tuple
    text: str
    selection: tuple[int, int] | None = None


@dataclass(frozen=True)
class EditorSelection:
    identity: tuple
    text: str


_WINDOWS_DOCUMENTS = frozenset((
    'winword.exe', 'excel.exe', 'powerpnt.exe', 'wps.exe', 'wpp.exe', 'et.exe',
    'soffice.exe', 'soffice.bin', 'wordpad.exe', 'notepad.exe', 'notepad++.exe',
))
_MAC_DOCUMENTS = frozenset((
    'com.microsoft.word', 'com.microsoft.excel', 'com.microsoft.powerpoint',
    'com.apple.iwork.pages', 'com.apple.iwork.numbers', 'com.apple.iwork.keynote',
    'com.apple.textedit', 'com.apple.notes', 'com.apple.preview', 'com.kingsoft.wpsoffice.mac',
    'org.libreoffice.script', 'md.obsidian',
))
_MAC_DOCUMENT_EXECUTABLES = frozenset((
    'textedit', 'pages', 'numbers', 'keynote', 'microsoft word', 'microsoft excel',
    'microsoft powerpoint', 'libreoffice', 'soffice', 'wpsoffice',
))
_DOCUMENT_EXTENSIONS = frozenset((
    '.txt', '.md', '.rtf', '.rtfd', '.pdf', '.doc', '.docx', '.xls', '.xlsx',
    '.ppt', '.pptx', '.odt', '.ods', '.odp', '.pages', '.numbers', '.key',
))
_WINDOWS_BROWSERS = frozenset((
    'chrome.exe', 'msedge.exe', 'firefox.exe', 'brave.exe', 'opera.exe', 'vivaldi.exe', 'arc.exe',
))
_MAC_BROWSERS = frozenset((
    'com.apple.safari', 'com.google.chrome', 'com.microsoft.edgemac', 'org.mozilla.firefox',
    'com.brave.browser', 'com.operasoftware.opera', 'com.vivaldi.vivaldi', 'company.thebrowser.browser',
))
_DOCUMENT_HOSTS = frozenset((
    'docs.google.com', 'docs.qq.com', 'kdocs.cn', 'www.kdocs.cn',
    'shimo.im', 'www.shimo.im', 'notion.so', 'www.notion.so',
    'word.cloud.microsoft', 'excel.cloud.microsoft', 'powerpoint.cloud.microsoft',
))
_OFFICE_PORTALS = frozenset(('office.com', 'www.office.com', 'm365.cloud.microsoft'))
_OFFICE_EDITORS = frozenset(('word', 'excel', 'powerpoint'))


def _application_scope(platform, identifier):
    name = (identifier or '').casefold()
    if platform == 'win32':
        name = basename(name)
        documents, browsers = _WINDOWS_DOCUMENTS, _WINDOWS_BROWSERS
    else:
        documents, browsers = _MAC_DOCUMENTS, _MAC_BROWSERS
    if name in documents or (platform == 'darwin' and name.startswith('com.kingsoft.wpsoffice.')):
        return 'document'
    if name in browsers:
        return 'browser'
    return 'editor'


def _is_web_document(address):
    if not address or len(address) > 8192:
        return False
    try:
        url = urlsplit(address)
        host = (url.hostname or '').rstrip('.')
    except ValueError:
        return False
    if url.scheme not in ('https', 'http'):
        return False
    path = url.path.casefold()
    if host in _DOCUMENT_HOSTS or host.endswith(('.officeapps.live.com', '.kdocs.cn')):
        return True
    if host in _OFFICE_PORTALS:
        return any(part in _OFFICE_EDITORS for part in path.split('/'))
    return host.endswith('.sharepoint.com') and path.endswith(('/doc.aspx', '/wopiframe.aspx'))


def _is_mac_document_address(address):
    if _is_web_document(address):
        return True
    if not address or len(address) > 8192:
        return False
    try:
        url = urlsplit(address)
    except ValueError:
        return False
    suffix = url.path.rsplit('/', 1)[-1].rpartition('.')
    return url.scheme == 'file' and bool(suffix[1]) and '.' + suffix[2].casefold() in _DOCUMENT_EXTENSIONS


@cache
def _process_image_api():
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.QueryFullProcessImageNameW.argtypes = (
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD))
    kernel.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    return kernel


@lru_cache(maxsize=32)
def _windows_application_scope(pid, _period):
    from ctypes import wintypes
    kernel = _process_image_api()
    process = kernel.OpenProcess(0x1000, False, pid)
    if not process:
        return 'editor'
    try:
        size = wintypes.DWORD(1024)
        image = ctypes.create_unicode_buffer(size.value)
        if kernel.QueryFullProcessImageNameW(process, 0, image, ctypes.byref(size)):
            if basename(image.value).casefold() == 'seda.exe':
                return 'seda'
            return _application_scope('win32', image.value)
        return 'editor'
    finally:
        kernel.CloseHandle(process)


@lru_cache(maxsize=32)
def _mac_application_scope(pid, _period):
    from AppKit import NSRunningApplication
    application = NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
    if application is None:
        return 'editor'
    scope = _application_scope('darwin', application.bundleIdentifier())
    executable = application.executableURL()
    if (scope == 'editor' and executable is not None
            and (executable.lastPathComponent() or '').casefold() in _MAC_DOCUMENT_EXECUTABLES):
        return 'document'
    return scope


def focused_editor(cancel, *, expected=None, insertion=None, sample=False, at_pointer=False):
    if cancel.is_set():
        return None
    if sys.platform == 'win32':
        return _windows_editor(cancel, expected, insertion, sample, at_pointer)
    if sys.platform == 'darwin':
        return _mac_editor(cancel, expected, insertion, sample)
    return None


_POINTER = ctypes.c_void_p
_INT = ctypes.c_int32


def _guid(value):
    return (ctypes.c_ubyte * 16).from_buffer_copy(UUID(value).bytes_le)


@cache
def _windows_libraries():
    # A raw DLL preserves the HRESULT for the normal S_FALSE case.
    ole = ctypes.WinDLL('ole32')
    ole.CoInitializeEx.argtypes = [_POINTER, ctypes.c_uint]
    ole.CoInitializeEx.restype = _INT
    ole.CoCreateInstance.argtypes = [_POINTER, _POINTER, ctypes.c_uint, _POINTER, ctypes.POINTER(_POINTER)]
    ole.CoCreateInstance.restype = _INT
    ole.CoUninitialize.restype = None
    auto = ctypes.WinDLL('oleaut32')
    auto.SysFreeString.argtypes = [_POINTER]
    auto.VariantClear.argtypes = [_POINTER]
    auto.SafeArrayDestroy.argtypes = [_POINTER]
    auto.SafeArrayGetUBound.argtypes = [_POINTER, ctypes.c_uint, ctypes.POINTER(_INT)]
    auto.SafeArrayAccessData.argtypes = [_POINTER, ctypes.POINTER(_POINTER)]
    auto.SafeArrayUnaccessData.argtypes = [_POINTER]
    return ole, auto


def _method(interface, index, result_type, *types):
    table = ctypes.cast(interface, ctypes.POINTER(ctypes.POINTER(_POINTER))).contents
    return ctypes.WINFUNCTYPE(result_type, _POINTER, *types)(table[index])


@contextmanager
def _automation():
    ole, library = _windows_libraries()
    if ole.CoInitializeEx(None, 0) < 0:
        yield None
        return
    with ExitStack() as stack:
        stack.callback(ole.CoUninitialize)
        objects = _AutomationObjects(stack, library)
        automation = _POINTER()
        status = ole.CoCreateInstance(
            _guid('ff48dba4-60ef-4201-aa87-54103eef594e'), None, 1,
            _guid('30cbe57d-d9d0-452a-ab13-7ac5ac4825ee'), ctypes.byref(automation),
        )
        if status < 0 or not automation:
            yield None
            return
        objects.own(automation)
        limits = objects.object(automation, 0, ctypes.cast(
            _guid('34723aff-0c9d-49d0-9896-7ab52df8cd8a'), _POINTER))
        if limits is not None:
            _method(limits, 61, _INT, ctypes.c_ulong)(limits, 150)
            _method(limits, 63, _INT, ctypes.c_ulong)(limits, 150)
        yield automation, objects


class _AutomationObjects:
    # Indices below are the IUnknown-derived vtable slots in Windows SDK
    # UIAutomationClient.h. COM is initialized and released on the worker.
    def __init__(self, stack, ole):
        self.stack = stack
        self.ole = ole

    def own(self, value):
        self.stack.callback(_method(value, 2, ctypes.c_ulong), value)
        return value

    def read(self, owner, slot, kind, *arguments):
        value = kind()
        status = _method(owner, slot, _INT, *(type(arg) for arg in arguments),
                         ctypes.POINTER(kind))(owner, *arguments, ctypes.byref(value))
        return value if status >= 0 else None

    def object(self, owner, slot, *arguments):
        value = self.read(owner, slot, _POINTER, *arguments)
        return self.own(value) if value else None

    def pattern(self, element, pattern, iid):
        pointer = ctypes.cast(_guid(iid), _POINTER)
        return self.object(element, 14, _INT(pattern), pointer)

    def string(self, owner, slot, *arguments):
        value = self.read(owner, slot, _POINTER, *arguments)
        if not value:
            return '' if value is not None else None
        try:
            return ctypes.wstring_at(value)
        finally:
            self.ole.SysFreeString(value)

    def runtime_id(self, element):
        array = self.read(element, 4, _POINTER)
        if not array:
            return ()
        upper, raw = _INT(), _POINTER()
        try:
            if self.ole.SafeArrayGetUBound(array, 1, ctypes.byref(upper)) != 0 or not 0 <= upper.value < 128:
                return ()
            if self.ole.SafeArrayAccessData(array, ctypes.byref(raw)) != 0:
                return ()
            try:
                return tuple(ctypes.cast(raw, ctypes.POINTER(_INT))[:upper.value + 1])
            finally:
                self.ole.SafeArrayUnaccessData(array)
        finally:
            self.ole.SafeArrayDestroy(array)


def _windows_editor(cancel, expected, insertion, sample, at_pointer, rejected=None):
    from ctypes.wintypes import RECT
    from clipboard_history_native import front_target

    def reject(reason):
        if rejected is not None:rejected(reason)
        return None

    target = front_target()
    if target is None:
        return reject('no external foreground window')
    scope = None if sample else _windows_application_scope(target[1], int(monotonic() // 5))
    if scope == 'document':
        return None
    with _automation() as apartment:
        if apartment is None or cancel.is_set():
            return reject('UI Automation unavailable')
        automation, com = apartment
        focused = com.object(automation, 8)
        if focused is None:
            return reject('no focused UI Automation element')
        process, control = (com.read(focused, slot, _INT) for slot in (20, 21))
        password, enabled = (com.read(focused, slot, _INT) for slot in (35, 28))
        if (process is None or process.value != target[1] or control is None
                or password is None or password.value or enabled is None or not enabled.value):
            return reject(('focus rejected',process.value if process is not None else None,target[1],
                           control.value if control is not None else None,
                           password.value if password is not None else None,
                           enabled.value if enabled is not None else None))
        if sample:
            selection = _sample_windows_selection(com, automation, focused, target, cancel, at_pointer)
            return selection if front_target() == target and not cancel.is_set() else None
        if control.value != 50004:
            return reject(('focus is not an edit control',control.value))
        value = com.pattern(focused, 10002, 'a94cd8b1-0844-4cd6-9d2d-640537ab39e9')
        if value is not None:
            readonly = com.read(value, 5, _INT)
            if readonly is None or readonly.value:
                return reject('value pattern is read-only')
        pattern = com.pattern(focused, 10014, '32eba289-3583-42c9-9c59-3b6d9a1e9b6a')
        document = com.object(pattern, 7) if pattern is not None else None
        if value is None and (document is None or not _windows_editable_range(com, document)):
            return reject(('no writable editor range',document is not None))
        identifier = com.runtime_id(focused)
        bounds = com.read(focused, 43, RECT)
        if not identifier or bounds is None or bounds.right <= bounds.left or bounds.bottom <= bounds.top:
            return reject('missing editor identity or geometry')
        web, document_site = False, False
        if scope == 'browser' or insertion is not None:
            web, document_site = _windows_web_context(com, automation, focused, cancel)
        if document_site:
            return None
        if cancel.is_set():
            return None
        text = com.string(document, 12, _INT(MAX_TEXT_CHARS + 1)) if document is not None else com.string(value, 4)
        if text is None or len(text) > MAX_TEXT_CHARS or cancel.is_set():
            return reject('editor text unavailable or too long')
        selection = _windows_selection(com, pattern, document, text) if document is not None else None
        bounds = bounds.left, bounds.top, bounds.right - bounds.left, bounds.bottom - bounds.top
        state = EditorState((target, identifier), bounds, text, selection)
        if front_target() != target:
            return reject('foreground changed during editor read')
        if insertion is not None:
            if (not _same_draft(state, expected) or cancel.is_set() or not insertion
                    or len(insertion) > 24000 or '\0' in insertion):
                return False
            web = web or (com.string(focused,40) or '').casefold() in ('chrome','mozilla')
            def read_current():
                if value is not None:
                    return com.string(value,4)
                latest=com.object(pattern,7)
                return com.string(latest,12,_INT(len(insertion)+3)) if latest is not None else None

            if scope == 'seda' and web:
                def read_seda_draft():
                    latest = com.object(pattern,7) if pattern is not None else None
                    if latest is None:
                        return read_current()
                    capacity = max(MAX_TEXT_CHARS, len(insertion.encode('utf-16-le')) // 2) + 1
                    current = com.string(latest,12,_INT(capacity))
                    # Each Chromium paragraph may contribute a terminal
                    # layout character absent from the editable DOM value.
                    if current is not None and current.rstrip() == insertion.rstrip():
                        return insertion
                    return current

                # Embedded Chromium hosts can intercept Ctrl+A or select
                # across the page. The focused editor exposes its own range.
                if document is not None:
                    def select_range():
                        return _method(document,16,_INT)(document) >= 0

                    if _paste_replacement(cancel,state,insertion,target,read_seda_draft,
                                          select_scope=select_range,allow_unicode=True,
                                          ignore_trailing_whitespace=True):
                        return True
                return _paste_replacement(cancel,state,insertion,target,read_seda_draft,
                                          within_editor=True,allow_unicode=True,
                                          ignore_trailing_whitespace=True)
            if web:
                return _paste_replacement(cancel,state,insertion,target,read_current,
                                          accept_unreported_selection=True)
            if value is not None and _method(value, 3, _INT, ctypes.c_wchar_p)(value, insertion) >= 0:
                if _confirmed_replacement(cancel,state,insertion,read_current):
                    return True
            return _paste_replacement(cancel,state,insertion,target,read_current)
        return state


def _windows_web_context(com, automation, editor, cancel):
    walker = com.object(automation, 16)
    if walker is None:
        return False, False
    deadline = monotonic() + .12
    web = False
    node = editor
    for _ in range(32):
        if node is None or cancel.is_set() or monotonic() > deadline:
            break
        control = com.read(node, 21, _INT)
        if control is not None and control.value == 50030:
            web = True
            # Browser-provided document URL, separate from the editor's text.
            legacy = com.pattern(node, 10018, '828055ad-355b-4435-86d5-3b51c14a9b1b')
            if legacy is not None and _is_web_document(com.string(legacy, 8)):
                return web, True
        node = com.object(walker, 3, node)
    return web, False


def _same_draft(state, expected):
    return state is not None and expected is not None and state.identity == expected.identity and state.text == expected.text


def _confirmed_replacement(cancel, expected, text, read_current):
    if text == expected.text:
        return True
    deadline = monotonic() + .48
    normalized = text.replace('\r\n','\n').replace('\r','\n')
    while not cancel.is_set():
        current = read_current()
        if current is not None and current.replace('\r\n','\n').replace('\r','\n') == normalized:
            return True
        if monotonic() >= deadline or cancel.wait(.03):
            return False
    return False


def _paste_replacement(cancel, expected, text, target, read_current, *,
                       accept_unreported_selection=False, within_editor=False,
                       select_scope=None, allow_unicode=False, ignore_trailing_whitespace=False):
    from clipboard_history_native import paste_editor_text, select_editor_text, type_windows_editor_text

    def same_editor():
        return _same_draft(focused_editor(cancel),expected)

    def selection_covers_draft(current):
        end = len(expected.text)
        if current.selection == (0,end):
            return True
        return ignore_trailing_whitespace and current.selection == (0,len(expected.text.rstrip()))

    def all_selected():
        current = focused_editor(cancel)
        return _same_draft(current,expected) and selection_covers_draft(current)

    def still_safe():
        current = focused_editor(cancel)
        if not _same_draft(current,expected):
            return False
        if selection_covers_draft(current):
            return True
        # Some web providers report the caret after a successful Select All.
        return (accept_unreported_selection and (current.selection is None
                or current.selection[0]==current.selection[1]))

    if cancel.is_set() or not same_editor():
        return False
    if not (select_scope() if select_scope is not None else select_editor_text(target,within_editor=within_editor)):
        return False
    deadline = monotonic() + .28
    while not all_selected():
        if monotonic() >= deadline or cancel.wait(.02):
            if not still_safe():
                return False
            break
    paste_editor_text(text,target,cancel,still_safe)
    if _confirmed_replacement(cancel,expected,text,read_current):
        return True
    if allow_unicode and not cancel.is_set() and all_selected():
        if type_windows_editor_text(text,target,all_selected):
            return _confirmed_replacement(cancel,expected,text,read_current)
    return False


def _sample_windows_selection(com, automation, focused, target, cancel, at_pointer):
    from ctypes.wintypes import POINT

    def read(element):
        process, password = (com.read(element, slot, _INT) for slot in (20, 35))
        if process is None or process.value != target[1] or password is None or password.value or cancel.is_set():
            return None
        pattern = com.pattern(element, 10014, '32eba289-3583-42c9-9c59-3b6d9a1e9b6a')
        span = _first_windows_selection(com, pattern)
        if span is None:
            return None
        text = com.string(span, 12, _INT(1001)) or ''
        identity = com.runtime_id(element) if text.strip() and len(text) <= 1000 else ()
        return EditorSelection((target, identity), text) if identity else None

    if at_pointer:
        point = POINT()
        pointer_ready = ctypes.windll.user32.GetCursorPos(ctypes.byref(point))
        if pointer_ready:
            element = com.object(automation, 7, point)
            walker = com.object(automation, 16)
            # Browsers commonly expose selection on the document ancestor,
            # even though the pointer is over one of its text descendants.
            for _ in range(7):
                if element is None or cancel.is_set():
                    break
                process, password = (com.read(element, index, _INT) for index in (20, 35))
                if process is None or process.value != target[1] or password is None or password.value:
                    break
                selected = read(element)
                if selected is not None:
                    return selected
                element = com.object(walker, 3, element) if walker is not None else None
    return read(focused)


class _Variant(ctypes.Union):
    class _Fields(ctypes.Structure):
        _fields_ = [('kind', ctypes.c_ushort), ('reserved', ctypes.c_ushort * 3), ('value', ctypes.c_short)]
    _fields_ = [('layout', _Fields), ('storage', ctypes.c_uint64 * (3 if ctypes.sizeof(_POINTER) == 8 else 2))]


def _windows_editable_range(com, document):
    value = com.read(document, 9, _Variant, _INT(40015))  # UIA_IsReadOnlyAttributeId
    if value is None:
        return False
    try:
        return value.layout.kind == 11 and value.layout.value == 0  # VT_BOOL
    finally:
        com.ole.VariantClear(ctypes.byref(value))


def _first_windows_selection(com, pattern):
    if pattern is not None:
        spans = com.object(pattern, 5)
        count = com.read(spans, 3, _INT) if spans is not None else None
        if count is not None and count.value == 1:
            return com.object(spans, 4, _INT(0))
    return None


def _windows_selection(com, pattern, document, text):
    selected = _first_windows_selection(com, pattern)
    if selected is None:
        return None
    chosen = com.string(selected,12,_INT(MAX_TEXT_CHARS+1))
    if text and chosen == text:
        return 0,len(text)
    prefix = com.object(document,3)
    if prefix is None:
        return None
    move = _method(prefix, 15, _INT, _INT, _POINTER, _INT)
    if move(prefix, 1, selected, 0) < 0:
        return None
    before = com.string(prefix, 12, _INT(MAX_TEXT_CHARS + 1))
    if before is None or chosen is None or text[len(before):len(before) + len(chosen)] != chosen:
        return None
    return len(before), len(before) + len(chosen)


@cache
def _mac_editor_api():
    from translation_focus import _mac_api
    ax, cf = _mac_api()
    carbon = ctypes.CDLL('/System/Library/Frameworks/Carbon.framework/Carbon')
    secure_input = carbon.IsSecureEventInputEnabled
    secure_input.argtypes = []
    secure_input.restype = ctypes.c_ubyte
    ax.AXUIElementCreateApplication.argtypes = [_INT]
    ax.AXUIElementCreateApplication.restype = _POINTER
    ax.AXUIElementGetPid.argtypes = [_POINTER, ctypes.POINTER(_INT)]
    ax.AXUIElementIsAttributeSettable.argtypes = [_POINTER, _POINTER, ctypes.POINTER(ctypes.c_ubyte)]
    ax.AXUIElementSetAttributeValue.argtypes = [_POINTER, _POINTER, _POINTER]
    ax.AXValueCreate.argtypes = [_INT, _POINTER]
    ax.AXValueCreate.restype = _POINTER
    cf.CFHash.argtypes = [_POINTER]
    cf.CFHash.restype = ctypes.c_size_t
    cf.CFStringGetTypeID.restype = ctypes.c_size_t
    cf.CFURLGetTypeID.restype = ctypes.c_size_t
    cf.CFURLGetString.argtypes = [_POINTER]
    cf.CFURLGetString.restype = _POINTER
    cf.CFGetTypeID.argtypes = [_POINTER]
    cf.CFGetTypeID.restype = ctypes.c_size_t
    cf.CFStringGetLength.argtypes = [_POINTER]
    cf.CFStringGetLength.restype = ctypes.c_long
    cf.CFStringGetCString.argtypes = [_POINTER, _POINTER, ctypes.c_long, ctypes.c_uint32]
    cf.CFStringGetCString.restype = ctypes.c_bool
    return ax, cf, secure_input


def _mac_editor(cancel, expected, insertion, sample, rejected=None):
    from clipboard_history_native import (MacPasteResult, front_target, mac_history_paste_blocker,
                                          select_editor_text, type_mac_editor_text)
    target = front_target()
    ax, cf, secure_input = _mac_editor_api()

    def reject(reason):
        if rejected is not None:
            rejected(reason)
        return None

    if secure_input():
        return reject('protected input is active')
    if not target:
        return reject('no external foreground application')
    if not ax.AXIsProcessTrusted():
        if insertion is not None:return MacPasteResult.ACCESSIBILITY
        return reject('missing Accessibility grant')
    scope = None if sample else _mac_application_scope(target, int(monotonic() // 5))
    if scope == 'document':
        return None
    with ExitStack() as stack:
        def retain(ref):
            if ref:
                stack.callback(cf.CFRelease, ref)
            return ref

        def key(value):
            return retain(cf.CFStringCreateWithCString(None, value, 0x08000100))

        def attr(element, name):
            result = _POINTER()
            if ax.AXUIElementCopyAttributeValue(element, key(name), ctypes.byref(result)) == 0:
                return retain(result.value)
            return None

        def read_string(value):
            if not value or cf.CFGetTypeID(value) != cf.CFStringGetTypeID():
                return None
            length = cf.CFStringGetLength(value)
            if not 0 <= length <= MAX_TEXT_CHARS * 2:
                return None
            data = ctypes.create_string_buffer(length * 4 + 1)
            if cf.CFStringGetCString(value, data, len(data), 0x08000100):
                return data.value.decode('utf8')
            return None

        def read_address(value):
            if value and cf.CFGetTypeID(value) == cf.CFURLGetTypeID():
                value = cf.CFURLGetString(value)
            return read_string(value)

        root = retain(ax.AXUIElementCreateSystemWide())
        if not root:
            return None
        ax.AXUIElementSetMessagingTimeout(root, .055)
        def belongs_to_target(element):
            pid = _INT()
            return element and ax.AXUIElementGetPid(element,ctypes.byref(pid))==0 and pid.value==target

        editor = attr(root,b'AXFocusedUIElement')
        if not belongs_to_target(editor):
            application = retain(ax.AXUIElementCreateApplication(target))
            if application:
                ax.AXUIElementSetMessagingTimeout(application, .055)
                editor = attr(application, b'AXFocusedUIElement')
        if not belongs_to_target(editor):
            return reject('no AX focused control from the foreground application')
        ax.AXUIElementSetMessagingTimeout(editor, .055)
        role = read_string(attr(editor, b'AXRole'))
        subrole = read_string(attr(editor, b'AXSubrole'))
        if subrole == 'AXSecureTextField' or secure_input():
            return None
        if sample:
            selected = read_string(attr(editor, b'AXSelectedText')) or ''
            if not selected.strip() or all(char in '•●* ' for char in selected):
                return None
            if len(selected) <= 1000 and front_target() == target and not cancel.is_set() and not secure_input():
                return EditorSelection((target, cf.CFHash(editor)), selected)
            return None
        if role not in ('AXTextArea', 'AXTextField'):
            return reject('AX focus does not expose an editable field role')
        window = attr(editor, b'AXWindow')
        if window and _is_mac_document_address(read_address(attr(window, b'AXDocument'))):
            return reject('focused window belongs to a document')
        selected_key, value_key = key(b'AXSelectedText'), key(b'AXValue')

        def settable(attribute):
            writable = ctypes.c_ubyte()
            return not ax.AXUIElementIsAttributeSettable(editor, attribute, ctypes.byref(writable)) and bool(writable.value)

        selection_writable = settable(selected_key)
        if not selection_writable and not settable(value_key):
            return reject('neither AXSelectedText nor AXValue is writable')
        web = False
        if scope == 'browser' or insertion is not None:
            deadline = monotonic() + .12
            node = editor
            for _ in range(32):
                if not node or cancel.is_set() or monotonic() > deadline:
                    break
                if read_string(attr(node, b'AXRole')) == 'AXWebArea':
                    web = True
                    if _is_mac_document_address(read_address(attr(node, b'AXURL'))):
                        return None
                node = attr(node, b'AXParent')
        if cancel.is_set() or secure_input():
            return None
        text = read_string(attr(editor, b'AXValue'))
        if text and all(char in '•●* ' for char in text):
            return None
        if text is None or len(text) > MAX_TEXT_CHARS or cancel.is_set() or secure_input():
            return reject('AX field value unavailable')
        coordinates = ctypes.c_double * 2
        xy, wh = coordinates(), coordinates()
        point, size = attr(editor, b'AXPosition'), attr(editor, b'AXSize')
        if not point or not size or not ax.AXValueGetValue(point, 1, xy) or not ax.AXValueGetValue(size, 2, wh):
            return reject('AX field rectangle unavailable')
        if min(wh) <= 0:
            return None
        offsets = ctypes.c_long * 2
        selected = offsets()
        selected_range = attr(editor, b'AXSelectedTextRange')
        selection = None
        if selected_range and ax.AXValueGetValue(selected_range, 4, selected):
            start, length = selected
            selection = python_position(text, start), python_position(text, start + length)
        if text and selection != (0,len(text)) and (insertion is not None or selection is None):
            chosen=read_string(attr(editor,b'AXSelectedText'))
            if chosen == text:
                selection = 0,len(text)
        state = EditorState((target, cf.CFHash(editor)), tuple(round(v) for v in (*xy, *wh)), text, selection)
        if front_target() != target:
            return reject('AX foreground changed during observation')
        if insertion is not None:
            if (not _same_draft(state,expected) or cancel.is_set() or secure_input() or not insertion
                    or len(insertion)>24000 or '\0' in insertion):
                return False
            def read_current():
                return read_string(attr(editor,b'AXValue'))

            range_key = key(b'AXSelectedTextRange')
            def select_range():
                entire = offsets(0,len(text.encode('utf-16-le'))//2)
                span = retain(ax.AXValueCreate(4,ctypes.byref(entire))) if settable(range_key) else None
                return bool(span and ax.AXUIElementSetAttributeValue(editor,range_key,span)==0)

            def full_selection():
                current=focused_editor(cancel)
                if not _same_draft(current,state) or secure_input():return False
                if current.selection==(0,len(text)):return True
                chosen=read_string(attr(editor,b'AXSelectedText'))
                return bool(chosen and chosen.rstrip()==text.rstrip())

            blocker=mac_history_paste_blocker()
            if blocker is None:
                # Use the focused range before a page's Select All command.
                if _paste_replacement(cancel,state,insertion,target,read_current,select_scope=select_range):
                    return True
                if _paste_replacement(cancel,state,insertion,target,read_current,
                                      accept_unreported_selection=True,within_editor=web):
                    return True
                if not web and _paste_replacement(cancel,state,insertion,target,read_current,
                                                   accept_unreported_selection=True,within_editor=True):
                    return True
            if not _same_draft(focused_editor(cancel),state):return False
            if web:
                # Pasteboard access is independent of Accessibility. A
                # verified rich editor can accept text without a clipboard read.
                for select_text in (select_range,lambda:select_editor_text(target,within_editor=True)):
                    if not full_selection() and select_text():
                        deadline=monotonic()+.25
                        while not full_selection() and monotonic()<deadline and not cancel.wait(.02):
                            pass
                    if (full_selection() and type_mac_editor_text(insertion,target,cancel,full_selection)
                            and _confirmed_replacement(cancel,state,insertion,read_current)):
                        return True
                    if not _same_draft(focused_editor(cancel),state):return False
                return MacPasteResult.CLIPBOARD_PERMISSION if blocker=='clipboard' else False
            native = key(insertion.encode('utf8'))
            if not native:
                return False
            if selection_writable and select_range():
                if (not secure_input() and ax.AXUIElementSetAttributeValue(editor, selected_key, native) == 0
                        and _confirmed_replacement(cancel,state,insertion,read_current)):
                    return True
                if selected_range and _same_draft(focused_editor(cancel),state):
                    ax.AXUIElementSetAttributeValue(editor, range_key, selected_range)
            if not _same_draft(focused_editor(cancel),state):
                return False
            if not settable(value_key) or ax.AXUIElementSetAttributeValue(editor, value_key, native):
                return MacPasteResult.CLIPBOARD_PERMISSION if blocker=='clipboard' else False
            caret = offsets(len(insertion.encode('utf-16-le')) // 2, 0)
            range_value = retain(ax.AXValueCreate(4, ctypes.byref(caret)))
            if range_value:
                ax.AXUIElementSetAttributeValue(editor, range_key, range_value)
            return _confirmed_replacement(cancel,state,insertion,read_current)
        return state
