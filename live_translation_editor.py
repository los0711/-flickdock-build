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
    selection: tuple[int, int] | None = None


@dataclass(frozen=True)
class EditorSelection:
    identity: tuple
    text: str


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


def _windows_editor(cancel, expected, insertion, sample, at_pointer):
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
            selection = _sample_windows_selection(com, automation, focused, target, cancel, at_pointer)
            return selection if front_target() == target and not cancel.is_set() else None
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
        selection = _windows_selection(com, pattern, document, text) if document is not None else None
        bounds = bounds.left, bounds.top, bounds.right - bounds.left, bounds.bottom - bounds.top
        state = EditorState((target, identifier), bounds, text, selection)
        if front_target() != target:
            return None
        if insertion is not None:
            if (not _same_draft(state, expected) or cancel.is_set() or not insertion
                    or len(insertion) > 24000 or '\0' in insertion):
                return False
            if value is not None and _method(value, 3, _INT, ctypes.c_wchar_p)(value, insertion) >= 0:
                return True
            if document is None or len(insertion) > 4096 or _method(document, 16, _INT)(document) < 0:
                return False
            return bool(front_target() == target and not cancel.is_set() and insert_text(insertion, target, cancel=cancel))
        return state


def _same_draft(state, expected):
    return expected is not None and state.identity == expected.identity and state.text == expected.text


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
    prefix = com.object(document, 3)
    if selected is None or prefix is None:
        return None
    move = _method(prefix, 15, _INT, _INT, _POINTER, _INT)
    if move(prefix, 1, selected, 0) < 0:
        return None
    before = com.string(prefix, 12, _INT(MAX_TEXT_CHARS + 1))
    chosen = com.string(selected, 12, _INT(MAX_TEXT_CHARS + 1))
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
    cf.CFGetTypeID.argtypes = [_POINTER]
    cf.CFGetTypeID.restype = ctypes.c_size_t
    cf.CFStringGetLength.argtypes = [_POINTER]
    cf.CFStringGetLength.restype = ctypes.c_long
    cf.CFStringGetCString.argtypes = [_POINTER, _POINTER, ctypes.c_long, ctypes.c_uint32]
    cf.CFStringGetCString.restype = ctypes.c_bool
    return ax, cf, secure_input


def _mac_editor(cancel, expected, insertion, sample, rejected=None):
    from clipboard_history_native import front_target
    target = front_target()
    ax, cf, secure_input = _mac_editor_api()

    def reject(reason):
        if rejected is not None:
            rejected(reason)
        return None

    if secure_input():
        return reject('protected input is active')
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
            application = retain(ax.AXUIElementCreateApplication(target))
            if application:
                ax.AXUIElementSetMessagingTimeout(application, .055)
                editor = attr(application, b'AXFocusedUIElement')
        if not editor:
            return reject('no AX focused control from the system or foreground application')
        ax.AXUIElementSetMessagingTimeout(editor, .055)
        pid = _INT()
        if ax.AXUIElementGetPid(editor, ctypes.byref(pid)) or pid.value != target:
            return reject('AX focus belongs to another process')
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
        selected_key, value_key = key(b'AXSelectedText'), key(b'AXValue')

        def settable(attribute):
            writable = ctypes.c_ubyte()
            return not ax.AXUIElementIsAttributeSettable(editor, attribute, ctypes.byref(writable)) and bool(writable.value)

        selection_writable = settable(selected_key)
        if not selection_writable and not settable(value_key):
            return reject('neither AXSelectedText nor AXValue is writable')
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
        state = EditorState((target, cf.CFHash(editor)), tuple(round(v) for v in (*xy, *wh)), text, selection)
        if front_target() != target:
            return reject('AX foreground changed during observation')
        if insertion is not None:
            if not _same_draft(state, expected) or cancel.is_set() or secure_input() or len(insertion) > 24000:
                return False
            native = key(insertion.encode('utf8'))
            if not native:
                return False
            range_key = key(b'AXSelectedTextRange')
            if selection_writable and settable(range_key):
                entire = offsets(0, len(text.encode('utf-16-le')) // 2)
                span = retain(ax.AXValueCreate(4, ctypes.byref(entire)))
                if span and ax.AXUIElementSetAttributeValue(editor, range_key, span) == 0:
                    if not secure_input() and ax.AXUIElementSetAttributeValue(editor, selected_key, native) == 0:
                        return True
                    if selected_range:
                        ax.AXUIElementSetAttributeValue(editor, range_key, selected_range)
                    return False
            if role != 'AXTextField' or not settable(value_key) or ax.AXUIElementSetAttributeValue(editor, value_key, native):
                return False
            caret = offsets(len(insertion.encode('utf-16-le')) // 2, 0)
            range_value = retain(ax.AXValueCreate(4, ctypes.byref(caret)))
            if range_value:
                ax.AXUIElementSetAttributeValue(editor, range_key, range_value)
            return True
        return state
