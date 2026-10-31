"""One-key reversible accessibility fixture on a disposable hosted Mac only.

Not imported by the app. It never overrides the production policy detector,
posts synthetic policy notifications, kills a daemon, changes TCC, or touches
any preference key except reduceTransparency. A fresh process must observe
the real native policy before the real pixel probe is allowed to run.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
from pathlib import Path
import plistlib
import subprocess
import sys

DOMAIN = 'com.apple.universalaccess'
KEY = 'reduceTransparency'
QA_REF = 'refs/heads/qa/material-probe-1823-20261031'
QA_REPOSITORY = 'los0711/-flickdock-build'
SCOPES = ('any-host','current-host')


def _guard(environment=None, platform=None):
    env = os.environ if environment is None else environment
    platform = sys.platform if platform is None else platform
    required = {'GITHUB_ACTIONS':'true','GITHUB_REPOSITORY':QA_REPOSITORY,
                'GITHUB_REF':QA_REF,'RUNNER_ENVIRONMENT':'github-hosted','RUNNER_OS':'macOS'}
    if platform != 'darwin' or any(env.get(k) != v for k,v in required.items()):
        raise RuntimeError('This single-key fixture is restricted to the exact disposable hosted Mac QA branch')


def _pack(value):
    if value is None:
        return dict(present=False,plist_base64=None,python_type='builtins.NoneType')
    data = plistlib.dumps(value,fmt=plistlib.FMT_BINARY,sort_keys=True)
    # Verify serialization is reversible before any preference mutation.
    restored = plistlib.loads(data)
    if plistlib.dumps(restored,fmt=plistlib.FMT_BINARY,sort_keys=True) != data:
        raise RuntimeError('Original preference value cannot be backed up exactly')
    return dict(present=True,plist_base64=base64.b64encode(data).decode('ascii'),
                python_type=type(value).__module__+'.'+type(value).__name__)


def _typed_equal(a,b):
    return (a['present'],a['plist_base64']) == (b['present'],b['plist_base64'])


class _Preferences:
    """Native boundary has no API for passing a different domain or key."""
    def __init__(self, corefoundation=None):
        if corefoundation is None: import CoreFoundation as corefoundation
        self.cf = corefoundation

    def _host(self, scope):
        if scope not in SCOPES: raise ValueError('Unknown preference host scope')
        return self.cf.kCFPreferencesCurrentHost if scope == 'current-host' else self.cf.kCFPreferencesAnyHost

    def forced(self):
        return bool(self.cf.CFPreferencesAppValueIsForced(KEY,DOMAIN))

    def sync(self, scope):
        return bool(self.cf.CFPreferencesSynchronize(DOMAIN,self.cf.kCFPreferencesCurrentUser,self._host(scope)))

    def read(self, scope):
        return self.cf.CFPreferencesCopyValue(KEY,DOMAIN,self.cf.kCFPreferencesCurrentUser,self._host(scope))

    def write(self, scope, value):
        self.cf.CFPreferencesSetValue(KEY,value,DOMAIN,self.cf.kCFPreferencesCurrentUser,self._host(scope))

    def snapshot(self):
        for scope in SCOPES:
            if not self.sync(scope): raise RuntimeError('Preference synchronization failed before readback')
        return {scope:self.read(scope) for scope in SCOPES}


def _policy_value(readback):
    entry = readback['policy']['reduce_transparency']
    if not entry.get('bridge_matches_native') or not entry.get('scalar_bool'):
        raise RuntimeError('Real NSWorkspace/native BOOL policy readback is unavailable or inconsistent')
    return bool(entry['native_bool'])


def _write_backup(path, backup):
    path = Path(path); path.parent.mkdir(parents=True,exist_ok=True)
    data = json.dumps(backup,ensure_ascii=False,indent=2).encode('utf-8')
    # Never overwrite the first original value with a temporary false value.
    with path.open('xb') as stream:
        stream.write(data);stream.flush();os.fsync(stream.fileno())
    if path.read_bytes() != data: raise RuntimeError('Durable original-key backup could not be verified')


def _exercise(preferences, read_policy, run_pixels, backup_writer):
    """Bounded transaction; tests inject storage and child-process readbacks."""
    result = dict(ok=False,blur_verified=False,restoration_verified=False,
                  domain=DOMAIN,key=KEY,mutation_attempted=False,errors=[])
    original = preferences.snapshot()
    packed = {scope:_pack(value) for scope,value in original.items()}
    scope = 'current-host' if original['current-host'] is not None else 'any-host'
    result.update(original=packed,target_scope=scope)
    # Backup raw key existence and typed plist, not its effective boolean.
    backup_writer(dict(domain=DOMAIN,key=KEY,target_scope=scope,original=packed))
    if preferences.forced():
        result.update(outcome='managed-policy',errors=['Forced preference; no mutation attempted'])
        return result
    before = read_policy('before')
    result['before'] = before
    original_policy = _policy_value(before)
    for name in SCOPES:
        if not _typed_equal(packed[name],before['preferences'][name]):
            raise RuntimeError('Raw preference changed between backup and initial fresh-process readback')
    try:
        if original_policy:
            if preferences.forced(): raise RuntimeError('Preference became forced before mutation')
            # Set this flag BEFORE calling a setter that might partially write.
            result['mutation_attempted'] = True
            preferences.write(scope,False)
            if not preferences.sync(scope): raise RuntimeError('Temporary preference write was rejected')
            actual = preferences.snapshot()
            if not _typed_equal(_pack(actual[scope]),_pack(False)):
                raise RuntimeError('Temporary raw key is not the requested typed boolean false')
            other = next(name for name in SCOPES if name != scope)
            if not _typed_equal(packed[other],_pack(actual[other])):
                raise RuntimeError('An unrelated host scope changed during the fixture')
        temporary = read_policy('temporary')
        result['temporary'] = temporary
        if temporary.get('forced') or _policy_value(temporary):
            raise RuntimeError('Real OS policy did not adopt the temporary condition; blur remains unverified')
        if original_policy and not _typed_equal(temporary['preferences'][scope],_pack(False)):
            raise RuntimeError('Fresh process did not read the temporary raw false key')
        pixels = run_pixels()
        result['pixels'] = pixels
        result['blur_verified'] = bool(pixels.get('ok') and pixels.get('blur_verified'))
        if not result['blur_verified']: raise RuntimeError('Real native blur pixel gate did not pass')
    except Exception as error:
        result['errors'].append(type(error).__name__+': '+str(error))
    finally:
        try:
            if result['mutation_attempted']:
                # None means remove this key, preserving original absence.
                preferences.write(scope,original[scope])
                if not preferences.sync(scope): raise RuntimeError('Original preference restoration synchronize failed')
            restored = {name:_pack(value) for name,value in preferences.snapshot().items()}
            result['restored_preferences'] = restored
            if any(not _typed_equal(packed[name],restored[name]) for name in SCOPES):
                raise RuntimeError('Original raw key existence/type/value was not restored exactly')
            final = read_policy('restored')
            result['restored'] = final
            if final.get('forced') != before.get('forced'):
                raise RuntimeError('Managed policy changed during the isolated fixture')
            if any(not _typed_equal(packed[name],final['preferences'][name]) for name in SCOPES):
                raise RuntimeError('Fresh-process raw preference restoration verification failed')
            if _policy_value(final) != original_policy:
                raise RuntimeError('Real NSWorkspace/native BOOL did not return to its original value')
            result['restoration_verified'] = True
        except Exception as error:
            result['errors'].append('RESTORE '+type(error).__name__+': '+str(error))
    result['ok'] = bool(result['blur_verified'] and result['restoration_verified'] and not result['errors'])
    result['outcome'] = 'verified' if result['ok'] else 'failed'
    return result


def _read_only_report():
    import AppKit,Foundation,objc
    from ci_material_probe import _policy_report
    preferences = _Preferences()
    return dict(policy=_policy_report(AppKit,Foundation,objc),forced=preferences.forced(),
                preferences={scope:_pack(value) for scope,value in preferences.snapshot().items()})


def verify(output):
    _guard()
    output = Path(output).resolve();output.parent.mkdir(parents=True,exist_ok=True)
    base = Path(__file__).resolve().parent
    result = dict(ok=False,blur_verified=False,restoration_verified=False,errors=[])
    def read_policy(stage):
        target = output.with_name(output.stem+'-'+stage+'.json')
        completed = subprocess.run([sys.executable,str(Path(__file__).resolve()),'--read-policy',str(target)],
                                   cwd=base,timeout=8,check=False,capture_output=True,text=True)
        if completed.stderr:
            target.with_suffix('.stderr.txt').write_text(completed.stderr[-16384:],encoding='utf-8')
        if completed.returncode != 0 or not target.is_file():
            raise RuntimeError('Fresh-process policy readback failed at '+stage+
                               '; rc='+str(completed.returncode)+'; '+completed.stderr[-2000:])
        return json.loads(target.read_text(encoding='utf-8'))
    def pixels():
        target = output.with_name(output.stem+'-pixels.json')
        completed = subprocess.run([sys.executable,str(base/'ci_material_probe.py'),str(target)],
                                   cwd=base,timeout=25,check=False,capture_output=True,text=True)
        if completed.stderr:
            target.with_suffix('.stderr.txt').write_text(completed.stderr[-16384:],encoding='utf-8')
        data = json.loads(target.read_text(encoding='utf-8')) if target.is_file() else {}
        data['process_returncode'] = completed.returncode
        if completed.returncode != 0: data['ok'] = False
        return data
    try:
        result = _exercise(_Preferences(),read_policy,pixels,
                           lambda backup:_write_backup(output.with_name(output.stem+'-original-key.json'),backup))
    except Exception as error:
        result['errors'].append(type(error).__name__+': '+str(error))
    finally:
        output.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    if not result['ok']: raise AssertionError(result)
    return result


if __name__ == '__main__':
    _guard()  # Before native imports, reads, files or child processes.
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--output')
    group.add_argument('--read-policy')
    args = parser.parse_args()
    if args.read_policy:
        Path(args.read_policy).write_text(json.dumps(_read_only_report(),ensure_ascii=False,indent=2),encoding='utf-8')
    else:
        print(json.dumps(verify(args.output),ensure_ascii=False))
