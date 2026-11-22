"""Read actual macOS permission state; requests run only from an explicit setup action."""
import ctypes,sys,time
from pathlib import Path
from platform_native import native_input_available

_clipboard_cache=None


class MacPermissionBackend:
    PANES={'clipboard':'Privacy_Pasteboard','accessibility':'Privacy_Accessibility',
           'input':'Privacy_ListenEvent','network':'Privacy_LocalNetwork','files':'Privacy_FilesAndFolders'}
    def __init__(self,*,appkit=None,quartz=None,ax=None,opener=None):
        self.appkit=appkit;self.quartz=quartz;self.ax=ax;self.opener=opener

    def _appkit(self):
        if self.appkit is None:import AppKit;self.appkit=AppKit
        return self.appkit

    def _quartz(self):
        if self.quartz is None:import Quartz;self.quartz=Quartz
        return self.quartz

    def _ax(self):
        if self.ax is None:
            self.ax=ctypes.CDLL('/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices')
            self.ax.AXIsProcessTrusted.argtypes=[];self.ax.AXIsProcessTrusted.restype=ctypes.c_bool
        return self.ax

    def clipboard(self):
        try:
            board=self._appkit().NSPasteboard.generalPasteboard()
            getter=getattr(board,'accessBehavior',None)
            if getter is None:return 'legacy'
            return {0:'default',1:'ask',2:'allowed',3:'denied'}.get(int(getter()),'unknown')
        except (ImportError,OSError,AttributeError,RuntimeError,TypeError,ValueError):return 'unknown'

    def accessibility(self):
        try:return 'allowed' if self._ax().AXIsProcessTrusted() else 'denied'
        except (ImportError,OSError,AttributeError,RuntimeError):return 'unknown'

    def paste(self):
        # Existing Accessibility trust authorizes sending input. Some macOS
        # releases still report a false event-preflight value for this process.
        if self.accessibility()=='allowed':return 'allowed'
        try:
            query=getattr(self._quartz(),'CGPreflightPostEventAccess',None)
            return 'allowed' if query and query() else 'denied'
        except (ImportError,OSError,AttributeError,RuntimeError):return 'unknown'

    def input(self):
        try:
            query=getattr(self._quartz(),'CGPreflightListenEventAccess',None)
            return 'allowed' if query and query() else 'denied' if query else 'legacy'
        except (ImportError,OSError,AttributeError,RuntimeError):return 'unknown'

    def inspect(self,*,pointer_ok=False,peers=False):
        return dict(clipboard=self.clipboard(),accessibility=self.accessibility(),
                    input='not_required' if pointer_ok else self.input(),network='observed' if peers else 'unknown',files='on_demand')

    def open_settings(self,kind):
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices
        return (self.opener or QDesktopServices.openUrl)(QUrl('x-apple.systempreferences:com.apple.preference.security?'+self.PANES[kind]))

    def reveal_application(self,bundle):
        from Foundation import NSURL
        workspace=self._appkit().NSWorkspace.sharedWorkspace()
        workspace.activateFileViewerSelectingURLs_([NSURL.fileURLWithPath_(str(bundle))])

    def choose_screenshot_folders(self):
        from Foundation import NSURL,NSUserDefaults
        from pathlib import Path
        kit=self._appkit()
        defaults=NSUserDefaults.standardUserDefaults().persistentDomainForName_('com.apple.screencapture') or {}
        location=Path(str(defaults.get('location') or Path.home()/'Desktop')).expanduser()
        panel=kit.NSOpenPanel.openPanel()
        panel.setTitle_('允许读取截图保存文件夹')
        panel.setMessage_('请选择系统和截图软件保存截图的文件夹，可一次选择多个。')
        panel.setCanChooseDirectories_(True);panel.setCanChooseFiles_(False)
        panel.setAllowsMultipleSelection_(True)
        panel.setDirectoryURL_(NSURL.fileURLWithPath_(str(location)))
        if panel.runModal()!=kit.NSModalResponseOK:return False
        self.screenshot_folders=[str(url.path()) for url in panel.URLs()]
        return True

    def request(self,kind):
        if kind=='files':return self.choose_screenshot_folders()
        # Never infer a grant from opening Settings, a successful read, or a
        # remembered setup step. inspect() always queries the current OS state.
        if kind=='clipboard' and self.clipboard()=='default':
            # Register this app in the pasteboard privacy pane if necessary.
            # This explicit user action neither writes nor uploads the content.
            self._appkit().NSPasteboard.generalPasteboard().pasteboardItems()
        elif kind=='input' and self.input()=='denied':
            request=getattr(self._quartz(),'CGRequestListenEventAccess',None)
            if request:request()
        return self.open_settings(kind)


def _setup_identity(executable=None):
    from app_version import APP_BUILD
    path=Path(executable or sys.executable).resolve()
    try:
        stat=path.stat()
        return f'{APP_BUILD}\n{path}\n{stat.st_dev}:{stat.st_ino}:{stat.st_mtime_ns}:{stat.st_size}'
    except OSError:
        return f'{APP_BUILD}\n{path}'


def setup_needed(base,backend=None,*,executable=None):
    backend=backend or MacPermissionBackend()
    if backend.accessibility()=='allowed' and backend.clipboard() in ('allowed','legacy'):
        return False
    try:
        return (Path(base)/'mac-permissions-shown').read_text(encoding='utf8')!=_setup_identity(executable)
    except (OSError,UnicodeError):
        return True


def mark_setup_shown(base,*,executable=None):
    path=Path(base)/'mac-permissions-shown'
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(_setup_identity(executable),encoding='utf8')


def clipboard_access(*,force=False):
    """Query metadata only; background polling must not repeatedly ask to paste."""
    global _clipboard_cache
    if sys.platform!='darwin' or not native_input_available():return 'not_required'
    now=time.monotonic()
    if not force and _clipboard_cache and now-_clipboard_cache[0]<2:return _clipboard_cache[1]
    state=MacPermissionBackend().clipboard();_clipboard_cache=(now,state);return state


def background_clipboard_allowed(*,force=False):
    return clipboard_access(force=force) in ('not_required','legacy','allowed')
