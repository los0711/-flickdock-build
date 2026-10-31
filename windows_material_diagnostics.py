"""CI candidate diagnostics only; never a product material implementation.

Activation is allowed only on nonce-owned HWNDs on our active private hosted-CI
desktop. No input injection, hooks, focus-lock changes or production eligibility.
"""
import ctypes,uuid
from ci_windows_desktop_fixture import DesktopApi,require_hosted_fixture


def require_private_scope(evidence):
    name=evidence.get('thread_desktop',{}).get('name','');active=evidence.get('input_desktop',{})
    if not name.startswith('FlickDockMaterialQA_') or active!={'name':name,'receiving_input':True}:raise RuntimeError('Activation diagnostics require our active private desktop')
    if evidence.get('station',{}).get('name','').casefold()!='winsta0' or not evidence.get('station',{}).get('visible'):raise RuntimeError('Not visible WinSta0')
    if evidence.get('session') in (None,0,0xffffffff) or evidence.get('session')!=evidence.get('active_console_session'):raise RuntimeError('Not the same active interactive session')
    if evidence.get('windows_product_type')!=1:raise RuntimeError('Not a Windows client diagnostic fixture')
    return name


class ActivationDiagnostics:
    def __init__(self,api=None):
        require_hosted_fixture();self.api=api or DesktopApi();self.desktop_name=require_private_scope(self.api.evidence())
        w=self.api.w;user=self.api.user
        self.api._configure(user,'SetForegroundWindow',[w.HWND],w.BOOL)
        self.api._configure(user,'GetActiveWindow',[],w.HWND)
        self.api._configure(user,'IsWindowEnabled',[w.HWND],w.BOOL)
        self.api._configure(user,'GetAncestor',[w.HWND,w.UINT],w.HWND)
        self.api._configure(user,'GetWindowRgn',[w.HWND,w.HANDLE],ctypes.c_int)
        self.gdi=ctypes.WinDLL('gdi32',use_last_error=True,winmode=0x800)
        self.api._configure(self.gdi,'CreateRectRgn',[ctypes.c_int]*4,w.HANDLE)
        self.api._configure(self.gdi,'GetRgnBox',[w.HANDLE,ctypes.POINTER(w.RECT)],ctypes.c_int)
        self.api._configure(self.gdi,'DeleteObject',[w.HANDLE],w.BOOL)
        class GuiThreadInfo(ctypes.Structure):
            _fields_=[('cbSize',w.DWORD),('flags',w.DWORD),('hwndActive',w.HWND),('hwndFocus',w.HWND),('hwndCapture',w.HWND),('hwndMenuOwner',w.HWND),('hwndMoveSize',w.HWND),('hwndCaret',w.HWND),('caretRect',w.RECT)]
        self.GuiThreadInfo=GuiThreadInfo
        self.api._configure(user,'GetGUIThreadInfo',[w.DWORD,ctypes.POINTER(GuiThreadInfo)],w.BOOL)

    def snapshot(self,windows):
        context=self.api.evidence();require_private_scope(context)
        if context['thread_desktop']['name']!=self.desktop_name:raise RuntimeError('Private desktop generation changed')
        user=self.api.user;current=int(self.api.kernel.GetCurrentProcessId());rows={}
        for role,hwnd in windows.items():
            row=self.api.window(hwnd,own=True)
            if row['pid']!=current or row.get('desktop',{}).get('name')!=self.desktop_name:raise RuntimeError('Refusing a foreign or different-desktop HWND')
            row.update(enabled=bool(user.IsWindowEnabled(hwnd)),owner_hwnd=int(user.GetWindow(hwnd,4) or 0),root_owner_hwnd=int(user.GetAncestor(hwnd,3) or 0))
            region=self.gdi.CreateRectRgn(0,0,0,0)
            if not region:raise OSError(ctypes.get_last_error(),'Diagnostic region allocation')
            try:
                kind=int(user.GetWindowRgn(hwnd,region));box=self.api.w.RECT()
                box_kind=int(self.gdi.GetRgnBox(region,ctypes.byref(box))) if kind else None
                row['window_region']=dict(kind=kind,box_kind=box_kind,bounds=[box.left,box.top,box.right,box.bottom] if kind else None)
            finally:row['region_temp_deleted']=bool(self.gdi.DeleteObject(region))
            backdrop=self.api.w.DWORD();nc=self.api.w.BOOL();frame=self.api.w.RECT()
            backdrop_hr=int(self.api.dwm.DwmGetWindowAttribute(hwnd,38,ctypes.byref(backdrop),ctypes.sizeof(backdrop)))
            nc_hr=int(self.api.dwm.DwmGetWindowAttribute(hwnd,1,ctypes.byref(nc),ctypes.sizeof(nc)))
            frame_hr=int(self.api.dwm.DwmGetWindowAttribute(hwnd,9,ctypes.byref(frame),ctypes.sizeof(frame)))
            row['dwm_attributes']=dict(system_backdrop_hresult=backdrop_hr,system_backdrop=backdrop.value if backdrop_hr>=0 else None,nc_rendering_hresult=nc_hr,nc_rendering_enabled=bool(nc.value) if nc_hr>=0 else None,
                                       extended_frame_hresult=frame_hr,extended_frame_bounds=[frame.left,frame.top,frame.right,frame.bottom] if frame_hr>=0 else None)
            rows[role]=row
        gui=self.GuiThreadInfo();gui.cbSize=ctypes.sizeof(gui);gui_ok=bool(user.GetGUIThreadInfo(self.api.kernel.GetCurrentThreadId(),ctypes.byref(gui)))
        return dict(context=context,foreground_hwnd=int(user.GetForegroundWindow() or 0),thread_active_hwnd=int(user.GetActiveWindow() or 0),
                    gui_thread=dict(ok=gui_ok,flags=gui.flags,active_hwnd=int(gui.hwndActive or 0),focus_hwnd=int(gui.hwndFocus or 0),capture_hwnd=int(gui.hwndCapture or 0)),windows=rows)

    def activate(self,role,windows):
        require_hosted_fixture()
        if role not in ('qt_foreground','passive','backplate') or set(windows)!={'qt_foreground','passive','backplate'}:raise ValueError('Exact owned diagnostic roles required')
        before=self.snapshot(windows);hwnd=int(windows[role])
        requested=bool(self.api.user.SetForegroundWindow(hwnd))
        after=self.snapshot(windows)
        return dict(role=role,requested_hwnd=hwnd,set_foreground_result=requested,actual_requested_foreground=after['foreground_hwnd']==hwnd,
                    before=before,after=after,product_eligible=False,
                    caveat='Explicit owned-fixture activation only; z-order/foreground occlusion is measured, never silently repaired')


def activation_case_record(activation,source_readiness,metrics,native_info,calls):
    observed=dict(metrics)
    observed['passes_original_pixel_checks']=observed.pop('verified',False)
    return dict(product_eligible=False,scope='Owned private-desktop activation control only; excluded from the original four-mode verified gate',
                activation=activation,source_readiness=source_readiness,observed_pixels=observed,native_info=native_info,api_call_results=list(calls))


def _method(pointer,index,result,*arguments):
    address=ctypes.cast(pointer,ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents[index]
    return ctypes.WINFUNCTYPE(result,ctypes.c_void_p,*arguments)(address)


def _release(pointer):
    if pointer and pointer.value:_method(pointer,2,ctypes.c_ulong)(pointer)


def graphics_power_diagnostics():
    """Transient public DXGI/D3D device queries; never change display/power policy."""
    require_hosted_fixture();require_private_scope(DesktopApi().evidence())
    from ctypes import wintypes as w
    class Guid(ctypes.Structure):_fields_=[('a',w.DWORD),('b',w.WORD),('c',w.WORD),('d',w.BYTE*8)]
    class Luid(ctypes.Structure):_fields_=[('low',w.DWORD),('high',w.LONG)]
    class Desc(ctypes.Structure):
        _fields_=[('description',ctypes.c_wchar*128),('vendor',w.UINT),('device',w.UINT),('subsystem',w.UINT),('revision',w.UINT),('dedicated_video',ctypes.c_size_t),('dedicated_system',ctypes.c_size_t),('shared_system',ctypes.c_size_t),('luid',Luid),('flags',w.UINT)]
    class Power(ctypes.Structure):_fields_=[('ac',w.BYTE),('battery_flags',w.BYTE),('battery_percent',w.BYTE),('battery_saver',w.BYTE),('lifetime',w.DWORD),('full_lifetime',w.DWORD)]
    report=dict(scope='Adapter/device capability and power facts; not proof of DWM sampling or product eligibility',product_eligible=False,adapters=[])
    kernel=ctypes.WinDLL('kernel32',use_last_error=True,winmode=0x800);kernel.GetSystemPowerStatus.argtypes=[ctypes.POINTER(Power)];kernel.GetSystemPowerStatus.restype=w.BOOL
    power=Power();power_ok=bool(kernel.GetSystemPowerStatus(ctypes.byref(power)))
    report['power']=dict(ok=power_ok,ac=power.ac,battery_flags=power.battery_flags,battery_saver=power.battery_saver)
    if not power_ok:report['power']['error']=ctypes.get_last_error()
    factory=ctypes.c_void_p()
    try:
        dxgi=ctypes.WinDLL('dxgi',use_last_error=True,winmode=0x800)
        dxgi.CreateDXGIFactory1.argtypes=[ctypes.POINTER(Guid),ctypes.POINTER(ctypes.c_void_p)];dxgi.CreateDXGIFactory1.restype=ctypes.c_long
        iid=Guid.from_buffer_copy(uuid.UUID('770aae78-f26f-4dba-a829-253c83d1b387').bytes_le)
        code=int(dxgi.CreateDXGIFactory1(ctypes.byref(iid),ctypes.byref(factory)));report['create_dxgi_factory_hresult']=code
        if code>=0:
            for index in range(8):
                adapter=ctypes.c_void_p();enum_code=int(_method(factory,12,ctypes.c_long,w.UINT,ctypes.POINTER(ctypes.c_void_p))(factory,index,ctypes.byref(adapter)))
                if enum_code<0:
                    report['adapter_enumeration_end_hresult']=enum_code;break
                try:
                    desc=Desc();desc_code=int(_method(adapter,10,ctypes.c_long,ctypes.POINTER(Desc))(adapter,ctypes.byref(desc)))
                    entry=dict(index=index,enum_hresult=enum_code,description_hresult=desc_code)
                    if desc_code>=0:entry.update(description=desc.description,vendor=desc.vendor,device=desc.device,flags=desc.flags,dedicated_video=desc.dedicated_video,dedicated_system=desc.dedicated_system,shared_system=desc.shared_system)
                    report['adapters'].append(entry)
                finally:_release(adapter)
    except Exception as error:report['dxgi_error']=type(error).__name__+': '+str(error)
    finally:_release(factory)
    d3d=ctypes.WinDLL('d3d11',use_last_error=True,winmode=0x800)
    d3d.D3D11CreateDevice.argtypes=[ctypes.c_void_p,ctypes.c_int,ctypes.c_void_p,w.UINT,ctypes.POINTER(w.UINT),w.UINT,w.UINT,ctypes.POINTER(ctypes.c_void_p),ctypes.POINTER(w.UINT),ctypes.POINTER(ctypes.c_void_p)];d3d.D3D11CreateDevice.restype=ctypes.c_long
    report['d3d11']=[]
    for name,driver_type in (('hardware',1),('warp',5)):
        device=ctypes.c_void_p();context=ctypes.c_void_p();level=w.UINT()
        try:
            code=int(d3d.D3D11CreateDevice(None,driver_type,None,0,None,0,7,ctypes.byref(device),ctypes.byref(level),ctypes.byref(context)))
            report['d3d11'].append(dict(driver=name,hresult=code,feature_level=level.value,feature_level_hex=f'0x{level.value:04x}'))
        finally:_release(context);_release(device)
    return report
