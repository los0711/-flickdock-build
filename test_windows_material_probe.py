"""Detector/loader guard models only; these are NOT native material evidence."""
import os,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from ci_windows_material_probe import frequency_response


class MaterialProbeTests(unittest.TestCase):
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
