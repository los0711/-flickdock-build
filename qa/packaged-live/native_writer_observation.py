"""Opt-in real frozen-client disk failure/recovery on hosted synthetic files only.

No application imports, test hooks or live model writes. The controller owns
the exact fixture directory ACL; all exported events omit paths, SID and SDDL.
"""
import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time

UUID=re.compile(r'[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}\Z')
NAME='Actual local peer item'
SAVE_ERROR='无法保存云端文件，请检查保存位置或文件占用。（DOWNLOAD_SAVE_FAILED）'


def require_hosted():
    if (sys.platform!='win32' or os.environ.get('GITHUB_ACTIONS')!='true'
            or os.environ.get('RUNNER_OS')!='Windows'
            or os.environ.get('RUNNER_ENVIRONMENT')!='github-hosted'):
        raise RuntimeError('Writer observation requires an ephemeral hosted Windows runner')


def reject_links(path,anchor):
    path=Path(path);anchor=Path(anchor)
    if not path.is_relative_to(anchor):raise AssertionError('Synthetic cache escaped TEMP anchor')
    for current in (path,*path.parents):
        if current.is_symlink() or getattr(current,'is_junction',lambda:False)():
            raise AssertionError('Synthetic cache has a link or junction ancestor')
        if current==anchor:break
    else:raise AssertionError('Synthetic cache has no expected TEMP ancestor')


def guarded_profile(profile,*,emit=lambda **values:None):
    require_hosted();profile=Path(profile).absolute();lexical_temp=Path(tempfile.gettempdir()).absolute()
    if profile.name!='FlickDock' or not profile.parent.name.startswith('flickdock-frozen-normal-ci-'):
        raise AssertionError('Not the exact disposable frozen-client profile')
    lexical_inside=profile.is_relative_to(lexical_temp)
    if lexical_inside:
        # Check the original spelling before following resolution. A short
        # Windows filename is not a reparse point; a real symlink still fails.
        reject_links(profile,lexical_temp)
        temporary=lexical_temp.resolve()
    else:
        # Subsequent fixture checks receive the already canonical profile.
        # Validate the trusted TEMP spelling itself before resolving its alias.
        reject_links(lexical_temp,lexical_temp)
        temporary=lexical_temp.resolve()
        if not profile.is_relative_to(temporary):
            emit(phase='temp-path-guard',raw_within_lexical_temp=False,raw_within_canonical_temp=False)
            raise AssertionError('Synthetic cache escaped TEMP anchor')
        reject_links(profile,temporary)
    canonical=profile.resolve()
    canonical_inside=canonical.is_relative_to(temporary)
    emit(phase='temp-path-guard',raw_within_lexical_temp=lexical_inside,
         raw_within_canonical_temp=profile.is_relative_to(temporary),
         temp_spelling_changed=lexical_temp!=temporary,profile_spelling_changed=profile!=canonical,
         short_name_component=any(re.search(r'~[0-9]+(?:\.|$)',part) is not None for part in (*profile.parts,*lexical_temp.parts)),
         canonical_within_temp=canonical_inside,original_ancestry_links_checked=True)
    if not canonical_inside:raise AssertionError('Resolved synthetic cache escaped TEMP anchor')
    reject_links(canonical,temporary)
    return canonical,temporary


def fixture_parent(profile,cloud,identity):
    """Only a known object in this run's synthetic account may get an ACL."""
    profile,temporary=guarded_profile(profile)
    account=cloud.session['account']['id'];version=getattr(cloud,'shelf_versions',{}).get(identity)
    if not UUID.fullmatch(str(account)) or not UUID.fullmatch(str(identity)) or type(version) is not int or not 0<version<2**53:
        raise AssertionError('Not a known synthetic account/object/version')
    parent=profile/'cloud-shelves'/account/'files'/identity/str(version)
    reject_links(parent,temporary);parent.mkdir(parents=True,exist_ok=True)
    reject_links(parent,temporary)
    if parent.resolve()!=parent:raise AssertionError('Synthetic cache changed on resolution')
    if any(parent.iterdir()):raise AssertionError('ACL injection requires an empty new fixture version')
    return parent


def write_probe(parent):
    """A real same-token filesystem open, not a simulated permission status."""
    path=Path(parent)/'.qa-writer-permission-probe'
    if path.exists():raise AssertionError('Unexpected existing permission probe')
    with path.open('xb') as stream:stream.write(b'synthetic ACL probe')
    path.unlink()


class WindowsAcl:
    """Exact directory DACL backup/restore; SID and ACL never leave this object."""
    def __init__(self,client_pid):
        require_hosted()
        import win32api,win32con,win32security
        self.api=win32api;self.con=win32con;self.security=win32security
        token=win32security.OpenProcessToken(win32api.GetCurrentProcess(),win32con.TOKEN_QUERY)
        try:self.sid=win32security.GetTokenInformation(token,win32security.TokenUser)[0]
        finally:token.Close()
        process=win32api.OpenProcess(0x1000,False,int(client_pid))
        try:
            token=win32security.OpenProcessToken(process,win32con.TOKEN_QUERY)
            try:client_sid=win32security.GetTokenInformation(token,win32security.TokenUser)[0]
            finally:token.Close()
        finally:process.Close()
        if self.sid!=client_sid:raise AssertionError('Frozen client and ACL probe use different Windows identities')

    def snapshot(self,parent):
        s=self.security
        descriptor=s.GetFileSecurity(str(parent),s.DACL_SECURITY_INFORMATION)
        original=s.ConvertSecurityDescriptorToStringSecurityDescriptor(descriptor,1,s.DACL_SECURITY_INFORMATION)
        protected=bool(descriptor.GetSecurityDescriptorControl()[0]&s.SE_DACL_PROTECTED)
        return descriptor,original,protected

    def _flags(self,protected):
        s=self.security
        return s.DACL_SECURITY_INFORMATION|(s.PROTECTED_DACL_SECURITY_INFORMATION if protected else s.UNPROTECTED_DACL_SECURITY_INFORMATION)

    def deny(self,parent,saved):
        s=self.security;old=saved[0].GetSecurityDescriptorDacl()
        if old is None:raise AssertionError('Synthetic directory has an unrestricted null DACL')
        acl=s.ACL();acl.AddAccessDeniedAceEx(s.ACL_REVISION_DS,0,0x0002,self.sid) # FILE_ADD_FILE only; no inheritance.
        for index in range(old.GetAceCount()):acl.AddAce(s.ACL_REVISION_DS,0xFFFFFFFF,old.GetAce(index))
        descriptor=s.SECURITY_DESCRIPTOR();descriptor.SetSecurityDescriptorDacl(1,acl,0)
        s.SetFileSecurity(str(parent),self._flags(saved[2]),descriptor)

    def restore(self,parent,saved):
        self.security.SetFileSecurity(str(parent),self._flags(saved[2]),saved[0])
        actual=self.snapshot(parent)[1]
        if actual!=saved[1]:raise AssertionError('Exact synthetic directory DACL restoration did not verify')


@contextlib.contextmanager
def blocked_directory(parent,*,backend,emit=lambda **values:None):
    """Restore even if deny/probe/body fails; never export native error text."""
    anchor=Path(tempfile.gettempdir()).resolve();reject_links(Path(parent),anchor)
    saved=backend.snapshot(parent)
    try:
        backend.deny(parent,saved)
        try:write_probe(parent)
        except PermissionError:emit(phase='acl-denial-confirmed',same_token_probe_denied=True)
        else:raise AssertionError('Real same-token cache write was not denied')
        yield
    finally:
        # DACL changes are atomic, but restore even if deny() raised halfway.
        reject_links(Path(parent),anchor);backend.restore(parent,saved)
        write_probe(parent)
        emit(phase='acl-restored',exact_dacl_restored=True,same_token_probe_writable=True)


def bounded_wait(predicate,label,seconds):
    end=time.monotonic()+seconds
    while time.monotonic()<end:
        result=predicate()
        if result:return result
        time.sleep(.05)
    raise AssertionError('Writer observation timeout: '+label)


def observe(profile,cloud,driver,emit):
    """Call only as an explicit observation scope; not 15-group acceptance."""
    failure=None;original=None
    try:
        # All preflight IO belongs inside the same payload-free error boundary.
        # Do not attempt preference cleanup until the original bool is verified.
        profile,_=guarded_profile(profile,emit=emit)
        settings=profile/'settings.json'
        if settings.is_symlink() or settings.stat().st_size>65536:raise AssertionError('Unsafe synthetic settings')
        value=json.loads(settings.read_text(encoding='utf8'))['sync_receive_enabled']
        if type(value) is not bool:raise AssertionError('Invalid original receive state')
        original=value
        return observe_fixture(profile,cloud,driver,emit)
    except BaseException as error:
        failure=error
        values=dict(phase='observation-failed',error_type=type(error).__name__)
        for field in ('errno','winerror'):
            value=getattr(error,field,None)
            if type(value) is int:values[field]=value
        emit(**values)
        # Controller traceback output must not embed the OS's raw path, SID
        # or security-descriptor payload. Type and native numeric reason stay.
        if isinstance(error,AssertionError):raise
        raise RuntimeError('Writer observation failed: '+type(error).__name__+'; payload-free type/code retained') from None
    finally:
        # ACL restore is independently handled by blocked_directory even if
        # UIA has failed. Keep any original failure when UI cleanup also fails.
        if original is not None:
            try:
                driver.setting('sync_receive_enabled',original)
                driver.click('关闭设置')
            except Exception as error:
                emit(phase='settings-cleanup-failed',error_type=type(error).__name__)
                if failure is None:raise RuntimeError('Writer observation settings cleanup failed: '+type(error).__name__) from None


def observe_fixture(profile,cloud,driver,emit):
    driver.click('设置');driver.click('搁架与剪贴板设置')
    driver.setting('sync_receive_enabled',False)
    identity=cloud.publish_shelf();parent=fixture_parent(profile,cloud,identity)
    version=cloud.shelf_versions[identity];account=cloud.session['account']['id']
    key=hashlib.sha256(identity.encode()).hexdigest()[:16]
    expected=('Synthetic remote shelf object '+identity+'\n').encode()
    journal=profile/'cloud-shelves'/account/'state.json'
    def state():
        try:
            if journal.is_symlink() or journal.stat().st_size>4*1024*1024:raise AssertionError('Unsafe synthetic journal')
            return json.loads(journal.read_text(encoding='utf8'))
        except (FileNotFoundError,json.JSONDecodeError):return {}
    def ready(item):return state().get('entries',{}).get(item,{}).get('version')==cloud.shelf_versions[item]
    def failed():
        deferred=state().get('deferred_downloads',{}).get(identity,{})
        if not deferred:return False
        if deferred.get('last_error')!=SAVE_ERROR:
            code=re.search(r'（([A-Z_]{1,48})）$',str(deferred.get('last_error','')))
            emit(phase='unexpected-frozen-save-error',error_code=code[1] if code else 'UNKNOWN')
            raise AssertionError('Unexpected actual frozen download error; payload-free code retained')
        if ready(identity):raise AssertionError('Denied fixture was falsely marked received')
        return True
    def received_part():
        return any(row.get('method')=='GET' and row.get('path')=='/v1/items/:id/parts/0'
                   and row.get('status')==200 and row.get('object_key')==key for row in cloud.stats()['rows'])
    with blocked_directory(parent,backend=WindowsAcl(driver.pid),emit=emit):
        emit(phase='frozen-client-identity-verified',same_windows_identity=True)
        driver.setting('sync_receive_enabled',True)
        bounded_wait(failed,'actual frozen save failure recorded',20)
        if not received_part():raise AssertionError('No successful real part response joined to denied fixture')
        if (parent/NAME).exists():raise AssertionError('Denied cache unexpectedly published target')
        emit(phase='frozen-save-failure',object_key=key,download_save_error_matches=True,
             actual_part_response_200=True,target_absent=True)
        sibling=cloud.publish_shelf()
        bounded_wait(lambda:ready(sibling),'adjacent unblocked object received while ACL denied',20)
        emit(phase='unaffected-object-received',object_key=hashlib.sha256(sibling.encode()).hexdigest()[:16],present=True)
    bounded_wait(lambda:ready(identity),'automatic same-object retry after ACL restoration',75)
    target=parent/NAME
    if target.is_symlink() or not target.is_file() or target.stat().st_size!=len(expected) or target.read_bytes()!=expected:
        raise AssertionError('Recovered synthetic original bytes did not match')
    if identity in state().get('deferred_downloads',{}):raise AssertionError('Recovered deferred state remained')
    if any(parent.glob('*.part')):raise AssertionError('Recovered cache retained temporary writer files')
    emit(phase='frozen-save-recovered',object_key=key,version=version,size=len(expected),
         synthetic_original_sha256_matches=True,deferred_cleared=True,no_temporary_parts=True)
    return dict(observation_only=True,installer_acceptance=False,writer_failure_recovery=True)
