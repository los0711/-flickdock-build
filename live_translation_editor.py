"""Read or insert text only in a verified, foreground, editable control."""
from contextlib import ExitStack, contextmanager
import ctypes
from dataclasses import dataclass
from functools import cache
import sys
from uuid import UUID

from translation_document import python_position
from translation_languages import MAX_TEXT_CHARS


@dataclass(frozen=True)
class EditorState:
    identity: tuple
    bounds: tuple
    text: str
    selection: tuple = (0, 0)


@dataclass(frozen=True)
class EditorSelection:
    identity: tuple
    text: str


def focused_editor(cancel, *, expected=None, insertion=None, sample=False):
    if cancel.is_set():
        return None
    if sys.platform == 'win32':
        return _windows_editor(cancel, expected, insertion, sample)
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


def _windows_editor(cancel, expected, insertion, sample):
    from ctypes.wintypes import RECT
    from clipboard_history_native import front_target, insert_text

    target = front_target()
    if target is None:
        return None
    with _automation() as apartment:
        if apartment is None or cancel.is_set():
            return None
        automation, com = apartment
        focused = com.object(automation, 8)
        if focused is None:
            return None
        process, control = (com.read(focused, slot, _INT) for slot in (20, 21))
        password, enabled = (com.read(focused, slot, _INT) for slot in (35, 28))
        if (process is None or process.value != target[1] or control is None
                or password is None or password.value or enabled is None or not enabled.value):
            return None
        if sample:
            pattern = com.pattern(focused, 10014, '32eba289-3583-42c9-9c59-3b6d9a1e9b6a')
            span = _first_windows_selection(com, pattern)
            identity = com.runtime_id(focused)
            if identity and front_target() == target and not cancel.is_set():
                selected = com.string(span, 12, _INT(1001)) if span is not None else ''
                return EditorSelection((target, identity), selected or '')
            return None
        if control.value != 50004:
            return None
        value = com.pattern(focused, 10002, 'a94cd8b1-0844-4cd6-9d2d-640537ab39e9')
        if value is not None:
            readonly = com.read(value, 5, _INT)
            if readonly is None or readonly.value:
                return None
        pattern = com.pattern(focused, 10014, '32eba289-3583-42c9-9c59-3b6d9a1e9b6a')
        document = com.object(pattern, 7) if pattern is not None else None
        if value is None and (document is None or not _windows_editable_range(com, document)):
            return None
        identifier = com.runtime_id(focused)
        bounds = com.read(focused, 43, RECT)
        if not identifier or bounds is None or bounds.right <= bounds.left or bounds.bottom <= bounds.top:
            return None
        text = com.string(document, 12, _INT(MAX_TEXT_CHARS + 1)) if document is not None else com.string(value, 4)
        if text is None or len(text) > MAX_TEXT_CHARS or cancel.is_set():
            return None
        selection = _windows_selection(com, pattern, document, text) if document is not None else (0, 0)
        bounds = bounds.left, bounds.top, bounds.right - bounds.left, bounds.bottom - bounds.top
        state = EditorState((target, identifier), bounds, text, selection)
        if front_target() != target:
            return None
        if insertion is not None:
            return bool(state == expected and not cancel.is_set() and insert_text(insertion, target, cancel=cancel))
        return state


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
    prefix = com.object(document, 3)
    if selected is None or prefix is None:
        return 0, 0
    move = _method(prefix, 15, _INT, _INT, _POINTER, _INT)
    if move(prefix, 1, selected, 0) < 0:
        return 0, 0
    before = com.string(prefix, 12, _INT(MAX_TEXT_CHARS + 1))
    chosen = com.string(selected, 12, _INT(MAX_TEXT_CHARS + 1))
    if before is None or not chosen or text[len(before):len(before) + len(chosen)] != chosen:
        return 0, 0
    return len(before), len(before) + len(chosen)


@cache
def _mac_editor_api():
    from translation_focus import _mac_api
    ax, cf = _mac_api()
    ax.AXUIElementGetPid.argtypes = [_POINTER, ctypes.POINTER(_INT)]
    ax.AXUIElementIsAttributeSettable.argtypes = [_POINTER, _POINTER, ctypes.POINTER(ctypes.c_ubyte)]
    ax.AXUIElementSetAttributeValue.argtypes = [_POINTER, _POINTER, _POINTER]
    ax.AXValueCreate.argtypes = [_INT, _POINTER]
    ax.AXValueCreate.restype = _POINTER
    cf.CFHash.argtypes = [_POINTER]
    cf.CFHash.restype = ctypes.c_size_t
    cf.CFStringGetTypeID.restype = ctypes.c_size_t
    cf.CFGetTypeID.argtypes = [_POINTER]
    cf.CFGetTypeID.restype = ctypes.c_size_t
    cf.CFStringGetLength.argtypes = [_POINTER]
    cf.CFStringGetLength.restype = ctypes.c_long
    cf.CFStringGetCString.argtypes = [_POINTER, _POINTER, ctypes.c_long, ctypes.c_uint32]
    cf.CFStringGetCString.restype = ctypes.c_bool
    return ax, cf


def _mac_editor(cancel, expected, insertion, sample, rejected=None):
    from clipboard_history_native import front_target
    target = front_target()
    ax, cf = _mac_editor_api()

    def reject(reason):
        if rejected is not None:
            rejected(reason)
        return None

    if not target or not ax.AXIsProcessTrusted():
        return reject('no external foreground application or Accessibility grant')
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

        root = retain(ax.AXUIElementCreateSystemWide())
        if not root:
            return None
        ax.AXUIElementSetMessagingTimeout(root, .055)
        editor = attr(root, b'AXFocusedUIElement')
        if not editor:
            return reject('no AX focused control')
        ax.AXUIElementSetMessagingTimeout(editor, .055)
        pid = _INT()
        if ax.AXUIElementGetPid(editor, ctypes.byref(pid)) or pid.value != target:
            return reject('AX focus belongs to another process')
        role = read_string(attr(editor, b'AXRole'))
        subrole = read_string(attr(editor, b'AXSubrole'))
        if subrole == 'AXSecureTextField':
            return None
        if sample:
            selected = read_string(attr(editor, b'AXSelectedText')) or ''
            if len(selected) <= 1000 and front_target() == target and not cancel.is_set():
                return EditorSelection((target, cf.CFHash(editor)), selected)
            return None
        if role not in ('AXTextArea', 'AXTextField'):
            return reject('AX focus does not expose an editable field role')
        selected_key, value_key = key(b'AXSelectedText'), key(b'AXValue')

        def settable(attribute):
            writable = ctypes.c_ubyte()
            return not ax.AXUIElementIsAttributeSettable(editor, attribute, ctypes.byref(writable)) and bool(writable.value)

        selection_writable = settable(selected_key)
        if not selection_writable and not settable(value_key):
            return reject('neither AXSelectedText nor AXValue is writable')
        text = read_string(attr(editor, b'AXValue'))
        if text is None or len(text) > MAX_TEXT_CHARS or cancel.is_set():
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
        if selected_range:
            ax.AXValueGetValue(selected_range, 4, selected)
        start, length = selected
        selection = python_position(text, start), python_position(text, start + length)
        state = EditorState((target, cf.CFHash(editor)), tuple(round(v) for v in (*xy, *wh)), text, selection)
        if front_target() != target:
            return reject('AX foreground changed during observation')
        if insertion is not None:
            if state != expected or cancel.is_set() or len(insertion) > 4096:
                return False
            if selection_writable:
                native = key(insertion.encode('utf8'))
                return bool(native and ax.AXUIElementSetAttributeValue(editor, selected_key, native) == 0)
            # Several Cocoa/Qt inputs provide a writable plain value but expose
            # selected text as read-only. Preserve their original text and range.
            if role != 'AXTextField' or not selected_range:
                return False
            begin, end = selection
            native = key((text[:begin] + insertion + text[end:]).encode('utf8'))
            if not native or ax.AXUIElementSetAttributeValue(editor, value_key, native):
                return False
            updated = text[:begin] + insertion
            caret = offsets(len(updated.encode('utf-16-le')) // 2, 0)
            range_value = retain(ax.AXValueCreate(4, ctypes.byref(caret)))
            if range_value:
                ax.AXUIElementSetAttributeValue(editor, key(b'AXSelectedTextRange'), range_value)
            return True
        return state
