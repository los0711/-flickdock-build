"""HOSTED-ONLY native Windows format matrix; standalone QA, not the app.

Qt provides real clipboard MIME. Separate protected native and Qt children read
it. There are no accounts, application manager, services or cloud requests.
"""
from __future__ import annotations
import argparse,hashlib,json,os,secrets,sys,tempfile,time,traceback,uuid
from collections import Counter
from pathlib import Path
from probe_contract import (ChildOperation,GuardError,QuarantineError,checked_source,classify_fastpath,inside,normal_path,path_fingerprint,
                            read_legacy_trace,require_hosted,safe_error,scope_record,text_fingerprint,wait_operation)

SOURCES=('lan_panel.py','flickdock.py','pointer_paste.py','shelf_content.py','win_clipboard_native.py',
         'win_clipboard_io.py','win_clipboard_cache.py','win_owned_process.py','clipboard_audit.py')
HERE=Path(__file__).resolve().parent

def file_hash(path):return hashlib.sha256(path.read_bytes()).hexdigest()
def source_hashes(source):return {name:file_hash(source/name) for name in SOURCES}
def safe_exception(cause):
    return dict(type=type(cause).__name__,frames=[dict(file=Path(f.filename).name,function=f.name,line=f.lineno)
                for f in traceback.extract_tb(cause.__traceback__)])

def _exit_quarantined_controller(record):
    """Only this guarded QA controller exits itself; never a product/helper API."""
    require_hosted()
    if record['parent_pid']!=os.getpid():raise GuardError('CONTROLLER_EXIT_ROLE_MISMATCH')
    # Skip Qt/OLE destructors after the failure report is closed. OS process
    # teardown closes this controller's noninherited, scope-owned Job handles.
    # This is NOT an observation that unresolved children were reaped.
    os._exit(2)

def run(args):
    temp,workspace=require_hosted()
    if not inside(HERE.resolve(),workspace):raise GuardError('QA_OUTSIDE_CHECKOUT')
    source=checked_source(args.source_root,workspace)
    output=Path(args.output).resolve()
    if not inside(output,workspace/'build') or output==workspace/'build':raise GuardError('OUTPUT_OUTSIDE_BUILD')
    if output.exists():raise GuardError('OUTPUT_MUST_BE_FRESH')
    output.mkdir(parents=True)
    root=temp/('fd-explicit-format-'+uuid.uuid4().hex);root.mkdir()
    nonce=secrets.token_hex(32)
    (root/'scope.json').write_text(json.dumps(dict(schema=1,nonce=nonce,run_id=os.environ['GITHUB_RUN_ID'],
        attempt=os.environ['GITHUB_RUN_ATTEMPT'],parent_pid=os.getpid())),encoding='utf8')
    _,_,controller_record=scope_record(root,nonce,role='controller')
    # These fixture settings never share the normal application's primary profile.
    for name in ('APPDATA','LOCALAPPDATA','XDG_CONFIG_HOME'):
        target=root/name.lower();target.mkdir();os.environ[name]=str(target)
    temporary=root/'temp';temporary.mkdir()
    for name in ('TEMP','TMP','TMPDIR'):os.environ[name]=str(temporary)
    tempfile.tempdir=str(temporary)
    for key in tuple(os.environ):
        if key.startswith(('CF_','CLOUDFLARE_','WRANGLER_')) or key in ('FD_QA_NATIVE_EXE','FD_QA_EXE','FROZEN_EXE'):
            os.environ.pop(key,None)
    os.environ.pop('QT_QPA_PLATFORM',None)
    sys.path.insert(0,str(source))
    from win_owned_process import spawn_owned
    from PySide6.QtCore import QMimeData,QUrl
    from PySide6.QtWidgets import QApplication
    app=QApplication([])
    if app.platformName()!='windows':raise GuardError('NATIVE_WINDOWS_PLATFORM_REQUIRED')
    class CountingMime(QMimeData):
        def __init__(self):super().__init__();self.phase='publish';self.calls=[]
        def retrieveData(self,mime_type,preferred_type):
            self.calls.append((self.phase,str(mime_type)))
            return super().retrieveData(mime_type,preferred_type)
    active=[];before=source_hashes(source);rows=[];errors=[];mime=None;callback_errors=[];native_quarantined=False
    sys.excepthook=lambda kind,cause,tb:callback_errors.append(safe_error(cause.with_traceback(tb)))
    originals=root/'originals';originals.mkdir();first=originals/'文件 A.txt';second=originals/'file B.bin';folder=originals/'Directory';folder.mkdir()
    first.write_bytes(b'owned fixture original A\n');second.write_bytes(bytes(range(64)))
    originals_before={first.name:file_hash(first),second.name:file_hash(second)}
    known={normal_path(str(p)):kind for p,kind in ((first,'file'),(second,'file'),(folder,'directory'))}
    def child(script,phase,*,owner=None,deadline=3.):
        nonlocal native_quarantined
        if native_quarantined:raise RuntimeError('NO_NATIVE_ACTION_AFTER_QUARANTINE')
        if script not in ('native_observer.py','qt_legacy_observer.py'):raise GuardError('UNOWNED_HELPER_SCRIPT')
        command=[sys.executable,'-X','utf8',str(HERE/script),'--scope',str(root),'--nonce',nonce]
        if script=='native_observer.py':command+=['--phase',phase,'--expected-owner',str(owner or os.getpid())]
        elif script=='qt_legacy_observer.py':command+=['--phase',phase,'--source-root',str(source)]
        operation=ChildOperation(command,process_factory=spawn_owned,deadline=deadline).start();active.append(operation)
        pumps=0
        def pump():
            nonlocal pumps
            app.processEvents();pumps+=1
        receipt=wait_operation(operation,pump);receipt['producer_event_pumps']=pumps
        if receipt['quarantined']:
            native_quarantined=True;raise QuarantineError(receipt)
        if script=='qt_legacy_observer.py':
            try:receipt['legacy_stages']=read_legacy_trace(root,operation.process.pid) if operation.process is not None else []
            except (OSError,ValueError,UnicodeError) as cause:
                # A child killed during its diagnostic write can leave a partial
                # line. Keep the original deadline/resource receipt visible.
                receipt['legacy_trace_error']=safe_error(cause)
                if not receipt['error']:receipt['error']='QA_LEGACY_TRACE_INVALID'
            receipt['legacy_stage_scope']='Owned child after scope validation; startup and file trace overhead remain inside original3s'
        return receipt
    def private_path_summary(paths):
        normalized=[normal_path(p) for p in paths]
        return dict(count=len(paths),order_hash=path_fingerprint(paths),
                    per_path_hash=[text_fingerprint(p) for p in normalized],
                    controlled_kinds=[known.get(p,'not_a_controlled_path') for p in normalized],
                    controlled_only=all(p in known for p in normalized))
    def sanitized(receipt):
        view=dict(receipt);value=receipt.get('value')
        if isinstance(value,dict):
            value=dict(value)
            if isinstance(value.get('paths'),list):value['path_summary']=private_path_summary(value.pop('paths'))
            if isinstance(value.get('queued_paths'),list):value['queued_summaries']=[private_path_summary(p) for p in value.pop('queued_paths')]
            view['value']=value
        return view
    def require_ok(receipt):
        if receipt['error'] or not receipt['value'] or receipt['value']['status']!='ok':raise RuntimeError('OBSERVER_NOT_OK')
        return receipt['value']
    cases=[
        ('qt-single','urls',[first],True),
        ('qt-multi-directory','urls',[first,folder,second],True),
        ('raw-uri-local','raw',[first],True),
        ('raw-uri-https','raw-https',[],False),
        ('raw-uri-mixed','raw-mixed',[first],False),
        ('plain-text-file-uri','text',[first],False),
        ('qt-CF_HDROP-plus-text','urls-text',[first],True)]
    try:
        for identifier,kind,paths,expected_queue in cases:
            row=dict(id=identifier,producer='Qt QMimeData with retrieveData counter',producer_declares_all_local_paths=expected_queue,
                     phases={},expected_paths=private_path_summary([QUrl.fromLocalFile(str(p)).toLocalFile() for p in paths]))
            rows.append(row);mime=CountingMime()
            try:
                urls=[QUrl.fromLocalFile(str(p)) for p in paths]
                if kind in ('urls','urls-text'):mime.setUrls(urls)
                elif kind=='raw':mime.setData('text/uri-list',bytes(urls[0].toEncoded())+b'\r\n')
                elif kind=='raw-https':mime.setData('text/uri-list',b'https://example.invalid/owned\r\n')
                elif kind=='raw-mixed':mime.setData('text/uri-list',bytes(urls[0].toEncoded())+b'\r\nhttps://example.invalid/owned\r\n')
                elif kind=='text':mime.setText(QUrl.fromLocalFile(str(first)).toString())
                if kind=='urls-text':mime.setText('owned alternate plain text')
                app.clipboard().setMimeData(mime);app.processEvents()
                row['producer_declared_formats']=list(mime.formats())
                receipts={}
                for phase in ('metadata','paths','legacy-inspect','legacy-control','legacy-filtered'):
                    if not app.clipboard().ownsClipboard():raise RuntimeError('PRODUCER_OWNERSHIP_LOST')
                    mime.phase=phase;start=len(mime.calls)
                    receipt=child('native_observer.py' if phase in ('metadata','paths') else 'qt_legacy_observer.py',phase)
                    receipts[phase]=receipt;row['phases'][phase]=sanitized(receipt)
                    row['phases'][phase]['provider_payload_requests']=[fmt for label,fmt in mime.calls[start:] if label==phase]
                    # A terminal failed default editor paste is still a failed
                    # observation, but must not prevent measuring the actual
                    # file-routing filter, which does not use that editor path.
                    if phase!='legacy-control':require_ok(receipt)
                meta=receipts['metadata']['value'];native=receipts['paths']['value'];legacy=receipts['legacy-inspect']['value']
                control=receipts['legacy-control']['value'];filtered=receipts['legacy-filtered']['value']
                assert row['phases']['metadata']['provider_payload_requests']==[],'METADATA_RENDERED_PAYLOAD'
                assert meta['owner_pid']==os.getpid() and meta['owner_pid_after']==os.getpid(),'NATIVE_OWNER_CHANGED'
                assert not legacy['qt_owns_clipboard'] and not filtered['qt_owns_clipboard'],'CONSUMER_MUST_BE_EXTERNAL'
                legacy_routable=bool(legacy['has_urls'] and legacy['url_count'] and all(legacy['local_flags']) and legacy['paths'])
                row['expected_old_queue_from_measured_legacy']=legacy_routable
                row['legacy_bridge_support']='recognized' if legacy_routable else 'not_recognized' if expected_queue else 'nonlocal_or_text'
                if legacy_routable:
                    expected=[QUrl.fromLocalFile(str(p)).toLocalFile() for p in paths]
                    assert path_fingerprint(legacy['paths'])==path_fingerprint(expected),'LEGACY_PATHS_DIFFER'
                    assert filtered['queue_dispatch_count']==1 and len(filtered['queued_paths'])==1,'QUEUE_NOT_ONCE'
                    assert path_fingerprint(filtered['queued_paths'][0])==path_fingerprint(expected),'QUEUE_PATHS_DIFFER'
                    assert filtered['input_unchanged'],'FILES_INSERTED_IN_INPUT'
                    # CF_HDROP absence is the observation this job is designed to discover, not fabricated failure/success.
                    if native['paths'] is not None:assert path_fingerprint(native['paths'])==path_fingerprint(expected),'NATIVE_PATHS_DIFFER'
                else:
                    control=require_ok(receipts['legacy-control'])
                    assert filtered['queue_dispatch_count']==0,'NONLOCAL_WAS_QUEUED'
                    assert filtered['input_hash']==control['input_hash'],'DEFAULT_TEXT_CHANGED'
                    assert filtered['cursor']==control['cursor'] and filtered['selection']==control['selection'],'INPUT_STATE_CHANGED'
                    assert filtered['undo_restored']==control['undo_restored'],'DEFAULT_UNDO_CHANGED'
                row['status']='observed';row['fastpath_coverage']=classify_fastpath(legacy_routable,legacy['paths'],meta,native,expected_queue)
                control_receipt=receipts['legacy-control']
                if control_receipt['error'] or not control_receipt['value'] or control_receipt['value']['status']!='ok':
                    row['status']='observed_with_control_failure';errors.append(identifier+'-legacy-control')
            except QuarantineError as cause:
                row['status']='quarantined';row['quarantine_receipt']=sanitized(cause.receipt);errors.append(identifier);raise
            except Exception as cause:row['status']='failed';row['error']=safe_exception(cause);errors.append(identifier)
            finally:
                row['provider_payload_request_counts']=[dict(phase=phase,format=fmt,count=n) for (phase,fmt),n in sorted(Counter(mime.calls).items())]
                mime.phase='retire'
                if not native_quarantined and app.clipboard().ownsClipboard():app.clipboard().clear();app.processEvents()
                mime=None
        # Different owned process intentionally blocks its renderer. It is not an Explorer provider.
        delayed=dict(id='owned-native-blocked-renderer',producer='owned Win32 delayed CF_HDROP fixture',status='not_run');rows.append(delayed)
        provider=None
        try:
            command=[sys.executable,'-X','utf8',str(HERE/'native_blocked_provider.py'),'--scope',str(root),'--nonce',nonce]
            provider=ChildOperation(command,process_factory=spawn_owned,deadline=12.).start();active.append(provider)
            ready=root/'blocked-provider-ready.json';deadline=time.monotonic()+5.
            while not ready.exists() and not provider.done.is_set() and time.monotonic()<deadline:
                app.processEvents();provider.done.wait(.002)
            if not ready.exists():raise RuntimeError('BLOCKED_PROVIDER_NOT_READY')
            ack=json.loads(ready.read_text(encoding='utf8'))
            if (ack.get('nonce')!=nonce or ack.get('controller_pid')!=os.getpid() or
                provider.process is None or ack.get('pid')!=provider.process.pid):raise RuntimeError('FIXTURE_IDENTITY_MISMATCH')
            (root/'observer-owner.json').write_text(json.dumps(dict(schema=1,nonce=nonce,controller_pid=os.getpid(),
                fixture_pid=ack['pid'],purpose='blocked_fixture')),encoding='utf8')
            receipt=child('native_observer.py','paths',owner=ack['pid'],deadline=3.)
            delayed['observer']=sanitized(receipt)
            assert receipt['error']=='DEADLINE' and receipt['reaped'] and receipt['closed'],'DEADLINE_NOT_REAPED'
            assert receipt['events']==['GETDATA_ENTER'],'GETDATA_PRECALL_MARKER_NOT_OBSERVED'
            delayed['status']='operation_deadline_and_reap_with_postmortem_precall_marker'
            delayed['timing_scope']='3s operation budget includes process startup/import; marker is before GetClipboardData and visible only after EOF, not API duration or renderer-entry evidence'
        except QuarantineError as cause:
            delayed['status']='quarantined';delayed['observer']=sanitized(cause.receipt);errors.append(delayed['id']);raise
        except Exception as cause:delayed['status']='failed';delayed['error']=safe_exception(cause);errors.append(delayed['id'])
        finally:
            if provider is not None:
                provider.cancel('FIXTURE_COMPLETE')
                receipt=wait_operation(provider,(lambda:None) if native_quarantined else app.processEvents)
                delayed['fixture_receipt']=sanitized(receipt)
                if receipt['quarantined']:native_quarantined=True
                if receipt['quarantined'] or receipt['cleanup_errors']:errors.append('blocked-fixture-cleanup')
                if not native_quarantined and (root/'observer-owner.json').exists():(root/'observer-owner.json').unlink()
    except QuarantineError:
        errors.append('native-quarantine-no-further-actions')
    finally:
        for operation in active:
            if not operation.done.is_set():operation.cancel('CONTROLLER_CLOSE')
        cleanup_views=[]
        for operation in active:
            receipt=wait_operation(operation,(lambda:None) if native_quarantined else app.processEvents)
            cleanup_views.append(sanitized(receipt))
            if receipt['quarantined']:native_quarantined=True
        if not native_quarantined and app.clipboard().ownsClipboard():app.clipboard().clear();app.processEvents()
        after=source_hashes(source);originals_after={first.name:file_hash(first),second.name:file_hash(second)}
        if before!=after:errors.append('source-drift')
        if originals_before!=originals_after or not folder.is_dir():errors.append('originals-changed')
        if callback_errors:errors.append('qt-callback-error')
        if native_quarantined:errors.append('controller-self-exit-quarantine')
        report=dict(schema=1,purpose='isolated Windows explicit paste format observation',ok=not errors,
                    source_before=before,source_after=after,source_stable=before==after,
                    originals_before=originals_before,originals_after=originals_after,rows=rows,errors=errors,callback_errors=callback_errors,
                    unverified=['Explorer actual provider','virtual files/FileGroupDescriptorW','FileNameW only','UNC','Mac/AppKit',
                                'frozen FlickDock EXE','physical OS SendInput; this uses Qt QTest key events on native Qt windows',
                                'full application duplicate-deduplicated draft; this observes the exact legacy filter queue dispatch'],
                    profiles_isolated=True,primary_first_copy_touched=False,cloud_requests='not_applicable_no_cloud_client_started',
                    cleanup={'all_owned_children_terminal':all(r['operation_done'] and r['reaped'] is True and r['closed'] is True for r in cleanup_views),
                             'all_owned_resources_verified':all(r.get('resource_cleanup_verified') is True for r in cleanup_views),
                             'operations':cleanup_views,'quarantined':native_quarantined,'controller_self_exit_requested':native_quarantined,
                             'self_exit_is_not_child_reap_evidence':True,'fixture_retained_in_owned_runner_temp':True})
        try:(output/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8')
        except Exception:
            if native_quarantined:
                os.write(2,b'QA_QUARANTINE_REPORT_WRITE_FAILED\n');_exit_quarantined_controller(controller_record)
            raise
        if native_quarantined:_exit_quarantined_controller(controller_record)
    return 0 if not errors else 1

def main(argv=None):
    parser=argparse.ArgumentParser();parser.add_argument('--source-root',required=True);parser.add_argument('--output',required=True)
    args=parser.parse_args(argv)
    try:return run(args)
    except Exception as cause:
        # No raw exception messages, argv, environment or clipboard values.
        sys.stderr.write(json.dumps(dict(status='failed',error=safe_exception(cause)))+'\n');return 2
if __name__=='__main__':raise SystemExit(main())
