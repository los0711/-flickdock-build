"""Detector/loader guard models only; these are NOT native material evidence."""
import ctypes,os,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from ci_windows_material_probe import architecture_evidence,dummy_source_evidence,frequency_response,order_dummy_windows


class MaterialProbeTests(unittest.TestCase):
    def test_probe_info_keeps_native_hwnd_width_and_distinguishes_unattempted_api(self):
        from types import SimpleNamespace
        from window_material_win import NativeProbeLibrary
        def info(handle,hwnds,hwnd_count,codes,code_count):
            hwnds[0]=0x123456789 if ctypes.sizeof(ctypes.c_void_p)==8 else 0x12345678;hwnds[1]=4321
            for index,value in enumerate((0,0,0,-2147483638,-2147483638)):codes[index]=value
            return 0
        driver=object.__new__(NativeProbeLibrary);driver.library=SimpleNamespace(fm_probe_info=info);driver.handle=1;driver.calls=[]
        result=driver.probe_info()
        self.assertEqual(result['hwnds']['qt_foreground'],0x123456789 if ctypes.sizeof(ctypes.c_void_p)==8 else 0x12345678)
        self.assertTrue(result['api_hresult']['CreateHostBackdropBrush']['attempted'])
        self.assertFalse(result['api_hresult']['DwmExtendFrameIntoClientArea']['attempted'])
        self.assertEqual(driver.calls[0]['hresult'],0)

    def test_activation_diagnostics_refuse_default_or_noninteractive_scope(self):
        from windows_material_diagnostics import require_private_scope
        good=dict(thread_desktop=dict(name='FlickDockMaterialQA_model'),input_desktop=dict(name='FlickDockMaterialQA_model',receiving_input=True),station=dict(name='WinSta0',visible=True),session=2,active_console_session=2,windows_product_type=1)
        self.assertEqual(require_private_scope(good),'FlickDockMaterialQA_model')
        for key,value in (('input_desktop',dict(name='Default',receiving_input=True)),('station',dict(name='WinSta0',visible=False)),('active_console_session',3),('windows_product_type',3)):
            with self.subTest(key=key),self.assertRaises(RuntimeError):require_private_scope(dict(good,**{key:value}))

    def test_activation_records_real_foreground_result_without_product_eligibility(self):
        from types import SimpleNamespace
        from windows_material_diagnostics import ActivationDiagnostics
        windows=dict(qt_foreground=101,passive=102,backplate=103)
        for accepted in (False,True):
            state={'foreground_hwnd':102};calls=[]
            def set_foreground(hwnd):
                calls.append(hwnd)
                if accepted:state['foreground_hwnd']=hwnd
                return int(accepted)
            controller=object.__new__(ActivationDiagnostics);controller.api=SimpleNamespace(user=SimpleNamespace(SetForegroundWindow=set_foreground));controller.snapshot=lambda hwnds:dict(state)
            with patch('windows_material_diagnostics.require_hosted_fixture'):
                result=controller.activate('backplate',windows)
                with self.assertRaises(ValueError):controller.activate('foreign',dict(windows,foreign=999))
            self.assertEqual(calls,[103]);self.assertEqual(result['actual_requested_foreground'],accepted);self.assertFalse(result['product_eligible'])

    def test_activation_scope_failure_cannot_reach_set_foreground(self):
        from types import SimpleNamespace
        from windows_material_diagnostics import ActivationDiagnostics,graphics_power_diagnostics
        controller=object.__new__(ActivationDiagnostics);controller.api=SimpleNamespace(user=SimpleNamespace(SetForegroundWindow=lambda hwnd:self.fail('Native call outside scope')))
        with patch('windows_material_diagnostics.require_hosted_fixture',side_effect=RuntimeError('Wrong hosted scope')),patch('windows_material_diagnostics.ctypes.WinDLL',side_effect=AssertionError('Unexpected DLL load'),create=True):
            with self.assertRaises(RuntimeError):controller.activate('backplate',dict(qt_foreground=1,passive=2,backplate=3))
            with self.assertRaises(RuntimeError):graphics_power_diagnostics()

    def test_activation_pixel_record_retains_occlusion_and_frequency_separately(self):
        from windows_material_diagnostics import activation_case_record
        metrics=dict(verified=False,frequency_verified=True,foreground_pixels_identical=False,outside_mean_abs_diff=0)
        report=activation_case_record({'product_eligible':True},{'phase0':{'ready':True}},metrics,{},[])
        self.assertFalse(report['product_eligible']);self.assertNotIn('verified',report)
        self.assertTrue(report['observed_pixels']['frequency_verified']);self.assertFalse(report['observed_pixels']['foreground_pixels_identical']);self.assertFalse(report['observed_pixels']['passes_original_pixel_checks'])

    def test_activation_snapshot_rejects_foreign_pid_before_further_window_queries(self):
        from types import SimpleNamespace
        from windows_material_diagnostics import ActivationDiagnostics
        good=dict(thread_desktop=dict(name='FlickDockMaterialQA_model'),input_desktop=dict(name='FlickDockMaterialQA_model',receiving_input=True),station=dict(name='WinSta0',visible=True),session=2,active_console_session=2,windows_product_type=1)
        controller=object.__new__(ActivationDiagnostics);controller.desktop_name='FlickDockMaterialQA_model'
        controller.api=SimpleNamespace(evidence=lambda:good,kernel=SimpleNamespace(GetCurrentProcessId=lambda:50),user=object(),window=lambda hwnd,own:dict(pid=999,desktop=dict(name='FlickDockMaterialQA_model')))
        with self.assertRaises(RuntimeError):controller.snapshot(dict(qt_foreground=101,passive=102,backplate=103))

    def test_graphics_diagnostics_are_bounded_release_owned_objects_and_keep_raw_fallback(self):
        from types import SimpleNamespace
        from windows_material_diagnostics import graphics_power_diagnostics
        releases=[];enumerations=[]
        class Function:
            def __init__(self,name):self.name=name
            def __call__(self,*args):
                if self.name=='GetSystemPowerStatus':
                    args[0]._obj.ac=1;args[0]._obj.battery_flags=128;args[0]._obj.battery_saver=0;return 1
                if self.name=='CreateDXGIFactory1':
                    self_test.assertEqual(args[0]._obj.a,0x770aae78);args[1]._obj.value=10;return 0
                if self.name=='D3D11CreateDevice':
                    if args[1]==1:return -2147467259
                    args[-3]._obj.value=20;args[-2]._obj.value=0xb000;args[-1]._obj.value=21;return 0
                raise AssertionError(self.name)
        class Library:
            def __getattr__(self,name):function=Function(name);setattr(self,name,function);return function
        def method(pointer,index,*signature):
            if index==12:
                def enum(factory,ordinal,output):
                    enumerations.append(ordinal)
                    if ordinal==0:output._obj.value=11;return 0
                    return ctypes.c_long(0x887a0002).value
                return enum
            if index==10:
                def desc(adapter,output):
                    self_test.assertEqual(ctypes.sizeof(output._obj),312 if ctypes.sizeof(ctypes.c_void_p)==8 else 296)
                    output._obj.description='MODEL_ADAPTER';output._obj.flags=2;output._obj.shared_system=4096;return 0
                return desc
            raise AssertionError(index)
        self_test=self
        with patch('windows_material_diagnostics.require_hosted_fixture'),patch('windows_material_diagnostics.require_private_scope'),patch('windows_material_diagnostics.DesktopApi',return_value=SimpleNamespace(evidence=lambda:{})),patch('windows_material_diagnostics.ctypes.WinDLL',side_effect=lambda *args,**kwargs:Library(),create=True),patch('windows_material_diagnostics._method',side_effect=method),patch('windows_material_diagnostics._release',side_effect=lambda pointer:releases.append(pointer.value) if pointer and pointer.value else None):
            report=graphics_power_diagnostics()
        self.assertEqual(enumerations,[0,1]);self.assertEqual(releases,[11,10,21,20])
        self.assertEqual(report['adapters'][0]['description'],'MODEL_ADAPTER');self.assertEqual(report['power']['battery_saver'],0)
        self.assertLess(report['d3d11'][0]['hresult'],0);self.assertEqual(report['d3d11'][1]['feature_level'],0xb000);self.assertFalse(report['product_eligible'])

    def test_dummy_ordering_only_touches_owned_hwnds_and_does_not_activate(self):
        from types import SimpleNamespace
        calls=[]
        user=SimpleNamespace(GetForegroundWindow=lambda:999,SetWindowPos=lambda *args:calls.append(args) or 1)
        report=order_dummy_windows(user,100,101)
        self.assertTrue(report['foreground_unchanged']);self.assertEqual([row[0] for row in calls],[100,101])
        self.assertEqual(calls,[(100,-1,0,0,0,0,0x213),(101,-1,0,0,0,0,0x213)])

    def test_dummy_readiness_rejects_obstruction_and_wrong_background_phase(self):
        def baseline(x,y):
            if 100<=x<135 and 90<=y<112:return (255,0,0)
            if 145<=x<169 and 90<=y<114:return (0,0,0) if ((145+((x-145)//2)*2)//2+y//2)%2 else (255,255,255)
            return ((15,65,100),(240,180,70))[(x//6+y//6)%2]
        self.assertTrue(dummy_source_evidence(baseline,0)['ready'])
        self.assertFalse(dummy_source_evidence(baseline,1)['ready'])
        obstructed=dummy_source_evidence(lambda x,y:(250,250,250),0)
        self.assertFalse(obstructed['ready']);self.assertFalse(obstructed['known_source_matches']);self.assertFalse(obstructed['opaque_red_matches'])

    def test_dummy_readiness_matches_real_qt_image_paint_for_both_phases(self):
        from PySide6.QtGui import QColor,QImage,QPainter
        for phase,palette in enumerate((((15,65,100),(240,180,70)),((80,10,130),(180,255,255)))):
            image=QImage(620,400,QImage.Format_RGB32);painter=QPainter(image)
            for y in range(0,400,6):
                for x in range(0,620,6):painter.fillRect(x,y,6,6,QColor(*palette[(x//6+y//6)%2]))
            painter.fillRect(100,90,35,22,QColor(255,0,0))
            for y in range(90,114,2):
                for x in range(145,169,2):painter.fillRect(x,y,2,2,QColor('black' if (x//2+y//2)%2 else 'white'))
            painter.end()
            self.assertTrue(dummy_source_evidence(lambda x,y:image.pixelColor(x,y).getRgb()[:3],phase)['ready'])

    def test_architecture_report_records_emulation_without_changing_pixel_gates(self):
        from types import SimpleNamespace
        class Query:
            def __call__(self,process,machine,native):
                machine._obj.value=0x8664;native._obj.value=0xaa64;return 1
        class CurrentProcess:
            def __call__(self):return -1
        with patch.dict(os.environ,{'RUNNER_ARCH':'ARM64','PROCESSOR_ARCHITECTURE':'AMD64','PROCESSOR_ARCHITEW6432':'ARM64'}):
            report=architecture_evidence(SimpleNamespace(GetCurrentProcess=CurrentProcess(),IsWow64Process2=Query()))
        self.assertEqual(report['is_wow64_process2'],dict(ok=True,process_machine=0x8664,native_machine=0xaa64))
        self.assertEqual(report['environment']['RUNNER_ARCH'],'ARM64')
        self.assertNotIn('verified',report)

    def test_frequency_detector_rejects_plain_transparency_and_opaque_fallback(self):
        first=[(10,20,30),(210,220,230)]*20;second=[(40,80,100),(240,240,230)]*20
        tint=lambda rows:[tuple(value*.1+255*.9 for value in row) for row in rows]
        result=frequency_response(first,second,tint(first),tint(second))
        self.assertAlmostEqual(result['normalized_blur_ratio'],1.)
        self.assertFalse(result['frequency_verified'])
        flat=[(40,40,40)]*40;self.assertFalse(frequency_response(first,second,flat,flat)['frequency_verified'])
        smooth=lambda rows:[tuple(sum(row[i] for row in rows)/len(rows) for i in range(3))]*len(rows)
        self.assertTrue(frequency_response(first,second,smooth(first),smooth(second))['frequency_verified'])

    def test_product_factory_remains_unverified_and_does_not_load_any_dll(self):
        from window_material_win import create_backend
        with patch('window_material_win.ctypes.WinDLL',side_effect=AssertionError('Unexpected DLL load'),create=True):
            backend=create_backend();caps=backend.capabilities()
            self.assertFalse(caps.supported);self.assertFalse(caps.verified);self.assertEqual(caps.reason,'unverified')
            self.assertFalse(backend.attach(object(),'panel'));backend.set_regions([],revision=1);backend.set_visible(True);backend.close()

    def test_probe_loader_rejects_relative_paths_and_wrong_hash_before_load(self):
        from window_material_win import NativeProbeLibrary
        with tempfile.TemporaryDirectory() as temporary,patch.dict(os.environ,{'GITHUB_ACTIONS':'true'}),patch('window_material_win.sys.platform','win32'),patch('window_material_win.ctypes.WinDLL',side_effect=AssertionError('Unexpected DLL load'),create=True):
            with self.assertRaises(ValueError):NativeProbeLibrary(Path('untrusted.dll'),'0'*64)
            path=Path(temporary)/'untrusted.dll';path.write_bytes(b'not executable')
            with self.assertRaises(ValueError):NativeProbeLibrary(path,'0'*64)


if __name__=='__main__':unittest.main()
