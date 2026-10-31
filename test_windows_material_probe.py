"""Detector/loader guard models only; these are NOT native material evidence."""
import os,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from ci_windows_material_probe import architecture_evidence,dummy_source_evidence,frequency_response,order_dummy_windows


class MaterialProbeTests(unittest.TestCase):
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
