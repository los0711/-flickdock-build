"""Replacement cancellation/ownership; native fixtures only on disposable CI."""
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from uuid import uuid4

import mac_install


class MacInstallTests(unittest.TestCase):
    def test_native_replacement_helper_distinguishes_cancellation_and_real_failure(self):
        calls=[]
        def run(command,**options):
            calls.append(command);return SimpleNamespace(returncode=0,stderr='',stdout='closed')
        self.assertTrue(mac_install.close_for_replacement(runner=run,exclude_pid=42))
        self.assertEqual(calls[0][:3],['/usr/bin/osascript','-l','JavaScript'])
        self.assertEqual(calls[0][-1],'42')
        self.assertFalse(mac_install.close_for_replacement(runner=lambda *a,**k:SimpleNamespace(returncode=1,stderr='User cancelled. (-128)')))
        with self.assertRaisesRegex(RuntimeError,'application unavailable'):
            mac_install.close_for_replacement(runner=lambda *a,**k:SimpleNamespace(returncode=1,stderr='application unavailable'))

    @unittest.skipUnless(sys.platform=='darwin' and os.environ.get('GITHUB_ACTIONS')=='true',
                         'Native application shutdown runs only in ephemeral macOS CI')
    def test_native_background_quit_excludes_installer_and_unrelated_bundle(self):
        # Only generated fixture processes are eligible. The unique bundle ID
        # replaces the production ID in the same native shutdown script.
        with tempfile.TemporaryDirectory(prefix='flickdock-installer-quit-') as directory:
            root=Path(directory).resolve();source=root/'probe.swift'
            source.write_text('''import AppKit
class Delegate: NSObject, NSApplicationDelegate {
    func applicationDidFinishLaunching(_ note: Notification) {
        try! String(ProcessInfo.processInfo.processIdentifier).write(toFile: CommandLine.arguments[2], atomically: true, encoding: .utf8)
    }
    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        return CommandLine.arguments[1] == "refuse" ? .terminateCancel : .terminateNow
    }
}
let app = NSApplication.shared
let delegate = Delegate()
app.delegate = delegate
app.setActivationPolicy(.accessory)
app.run()
''',encoding='utf8')
            compiled=root/'probe';subprocess.run(['/usr/bin/xcrun','swiftc',str(source),'-o',str(compiled)],check=True,capture_output=True,timeout=90)
            identity='com.flickdock.installer-fixture.'+uuid4().hex
            import shutil
            children=[]
            try:
                for index,mode in enumerate(('quit','refuse','other')):
                    bundle=root/(mode+'.app');binary=bundle/'Contents/MacOS/FlickDock';binary.parent.mkdir(parents=True)
                    shutil.copy2(compiled,binary)
                    (bundle/'Contents/Info.plist').write_bytes(plistlib.dumps(dict(CFBundleIdentifier=identity if mode!='other' else identity+'.other',CFBundleExecutable='FlickDock',CFBundlePackageType='APPL',LSUIElement=True)))
                    marker=root/(mode+'.pid');child=subprocess.Popen([str(binary),mode,str(marker)],stdout=subprocess.DEVNULL,stderr=subprocess.PIPE);children.append(child)
                    deadline=time.monotonic()+10
                    while not marker.is_file() and time.monotonic()<deadline:
                        if child.poll() is not None:raise AssertionError(child.stderr.read().decode(errors='replace'))
                        time.sleep(.05)
                    self.assertTrue(marker.is_file(),'Native fixture did not start')
                script=mac_install.INSTALL_CLOSE_JXA.replace("'com.flickdock.app'",repr(identity)).replace('    confirmReplacement();','    // Disposable fixture: confirmation already supplied by the test.')
                def close(excluded):
                    result=subprocess.run(['/usr/bin/osascript','-l','JavaScript','-e',script,str(excluded)],capture_output=True,text=True,timeout=20)
                    self.assertEqual(result.returncode,0,result.stderr)
                close(children[1].pid);children[0].wait(timeout=5)
                self.assertIsNone(children[1].poll());self.assertIsNone(children[2].poll())
                close(0);children[1].wait(timeout=5)
                self.assertIsNone(children[2].poll(),'Other bundle with same process name was terminated')
            finally:
                for child in children:
                    if child.poll() is None:child.kill()
                    child.communicate(timeout=5)


if __name__=='__main__':unittest.main(verbosity=2)
