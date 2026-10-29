"""Native CI only: run the frozen old helper across a real bundle replacement.

The new target is a tiny separately frozen Qt fixture, never a release package.
No user application or settings are touched; all targets live in mkdtemp.
"""
import hashlib,json,os,plistlib,shutil,subprocess,sys,tempfile,time,zipfile
from pathlib import Path
from mac_updates import prepare_update,launch_update_helper,bundle_metadata

def main(binary,output):
    if sys.platform!='darwin' or os.environ.get('GITHUB_ACTIONS')!='true':
        raise RuntimeError('Requires the ephemeral native macOS CI runner')
    from app_version import APP_VERSION
    source=Path(binary).resolve().parents[2]
    with tempfile.TemporaryDirectory(prefix='flickdock-native-update-') as directory:
        root=Path(directory).resolve();target=root/'FlickDock.app'
        shutil.copytree(source,target,symlinks=True)
        probe=root/'probe.py'
        probe.write_text('''import json,sys,os\nfrom pathlib import Path\nfrom PySide6.QtCore import QCoreApplication,QTimer\na=QCoreApplication(sys.argv)\nif '--post-update-ready' in sys.argv:\n i=sys.argv.index('--post-update-ready');marker=Path(sys.argv[i+1]);root=marker.parent.parent\n (root/'probe-first-pid').write_text(str(os.getpid()));a.aboutToQuit.connect(lambda:(root/'probe-exited').write_text('yes'))\n QTimer.singleShot(20,lambda:marker.write_text(sys.argv[i+2]));QTimer.singleShot(1000,a.quit);a.exec()\nelif '--verify-platform' in sys.argv:\n Path(sys.argv[-1]).write_text(json.dumps(dict(ok=True,version='1.8.21',frozen=bool(getattr(sys,'frozen',False)))))\n''')
        subprocess.run([sys.executable,'-m','PyInstaller','--noconfirm','--onedir','--windowed',
            '--name','FlickDock','--osx-bundle-identifier','com.flickdock.app',
            '--distpath',str(root/'probe-dist'),'--workpath',str(root/'probe-work'),
            '--specpath',str(root),str(probe)],check=True,stdout=subprocess.DEVNULL)
        new=root/'probe-dist/FlickDock.app';info=new/'Contents/Info.plist'
        metadata=plistlib.loads(info.read_bytes());metadata.update(CFBundleShortVersionString='1.8.21',CFBundleVersion='1.8.21')
        info.write_bytes(plistlib.dumps(metadata))
        # Editing Info.plist invalidates the ad-hoc bundle signature.
        subprocess.run(['/usr/bin/codesign','--force','--deep','--sign','-',str(new)],check=True)
        archive=root/'update.zip'
        # ditto emits native symlink and executable metadata used by shipping.
        subprocess.run(['/usr/bin/ditto','-c','-k','--sequesterRsrc','--keepParent',str(new),str(archive)],check=True)
        manifest=dict(version='1.8.21',installer=dict(size=archive.stat().st_size,
            sha256=hashlib.sha256(archive.read_bytes()).hexdigest()))
        old_hash=hashlib.sha256((target/'Contents/MacOS/FlickDock').read_bytes()).hexdigest()
        prepared=prepare_update(archive,manifest,current_version=APP_VERSION,installed_app=target,staging_parent=root)
        prepared.owner_pid=99999999
        # Same production entry and copied-runtime launcher; only Popen is
        # injected because the test driver itself is an unfrozen interpreter.
        process=launch_update_helper(prepared,executable=target/'Contents/MacOS/FlickDock',
            popen=lambda *args,**kwargs:subprocess.Popen(*args,**kwargs,env=dict(os.environ,FLICKDOCK_CI_RUNTIME_READER='1')),quit_callback=lambda:None)
        status=process.wait(timeout=100)
        error=prepared.stage/'install-error.txt'
        if status:raise RuntimeError(error.read_text() if error.is_file() else 'Native helper failed: '+str(status))
        self_version=bundle_metadata(target)['CFBundleShortVersionString']
        assert self_version=='1.8.21'
        reader=json.loads((root/'runtime-reader.json').read_text());assert reader['cold_pyz_read_after_swap'] and reader['independent_executable']
        assert old_hash!=hashlib.sha256((target/'Contents/MacOS/FlickDock').read_bytes()).hexdigest()
        deadline=time.monotonic()+10
        from mac_updates import _pid_alive
        while time.monotonic()<deadline:
            pid_path=root/'probe-first-pid'
            if pid_path.is_file() and (root/'probe-exited').is_file() and not _pid_alive(int(pid_path.read_text())):break
            time.sleep(.1)
        else:raise AssertionError('First replacement process did not exit before reopening')
        # Exit/reopen from the canonical target must run the replacement.
        report=root/'reopened.json'
        subprocess.run([str(target/'Contents/MacOS/FlickDock'),'--verify-platform',str(report)],check=True,timeout=20)
        reopened=json.loads(report.read_text());assert reopened['ok'] and reopened['frozen'] and reopened['version']=='1.8.21'
        deadline=time.monotonic()+8
        while prepared.stage.exists() and time.monotonic()<deadline:time.sleep(.1)
        assert not prepared.stage.exists(),'Copied updater runtime was not cleaned after helper exit'
        result=dict(ok=True,frozen_old_helper=True,bundle_replaced=True,reopened_new_version=True,
                    runtime_cleaned=True,original_version=APP_VERSION,target_version=self_version,
                    cold_pyz_read_after_swap=True,privileged_authorization_tested=False)
        Path(output).write_text(json.dumps(result,indent=2));print(json.dumps(result))

if __name__=='__main__':main(*sys.argv[1:])
