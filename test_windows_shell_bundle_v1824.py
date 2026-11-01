"""Frozen COM policy: fake PE inputs, no DLL execution or native desktop."""
from pathlib import Path
import tempfile,unittest
from unittest.mock import patch
import bundle_policy as policy


class ShellBundleTests(unittest.TestCase):
    def test_dynamic_com_roots_survive_without_an_extension_import_edge(self):
        with tempfile.TemporaryDirectory() as directory:
            entries=[]
            for name in (*sorted(policy.WINDOWS_SHELL_ROOTS),'unreferenced-extra.dll'):
                path=Path(directory)/name;path.write_bytes(b'fixture, never loaded')
                entries.append(('pywin32_system32/'+name,str(path),'BINARY'))
            with patch.object(policy,'pe_dependencies',return_value=set()):
                binaries,_,report=policy.filter_bundle(entries,[])
            kept={Path(row[1]).name for row in binaries}
            self.assertEqual(kept,policy.WINDOWS_SHELL_ROOTS)
            self.assertFalse(policy.WINDOWS_SHELL_ROOTS.intersection(report['required_dynamic_roots_missing']))
            self.assertTrue(any(row['destination'].endswith('unreferenced-extra.dll') for row in report['removed']))

    def test_data_classification_preserves_binding_and_its_dependency(self):
        with tempfile.TemporaryDirectory() as directory:
            names=('pythoncom312.dll','pywintypes312.dll','com-dependency.dll')
            entries=[]
            for name in names:
                path=Path(directory)/name;path.write_bytes(b'fixture, never loaded')
                entries.append((name,str(path),'DATA'))
            def imports(path):
                return {'com-dependency.dll'} if Path(path).name=='pythoncom312.dll' else set()
            with patch.object(policy,'pe_dependencies',side_effect=imports):
                _,data,report=policy.filter_bundle([],entries)
            self.assertEqual({Path(row[1]).name for row in data},set(names))
            self.assertEqual(report['unresolved_non_system_imports'],[])

    def test_missing_runtime_binding_is_an_explicit_build_failure_condition(self):
        _,_,report=policy.filter_bundle([],[])
        self.assertTrue(policy.WINDOWS_SHELL_ROOTS.issubset(report['required_dynamic_roots_missing']))


if __name__=='__main__':unittest.main()
