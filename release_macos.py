"""Produce a macOS update index after native QA and anonymous package verification.

This operator tool never uploads source, launches an app, or installs an update.
The existing Windows PE release producer remains separate.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import plistlib
import stat
import struct
import sys
import zipfile

from release_manifest import (ReleaseError, _json, _public_response, _write_atomic,
                              sha256_file, validate_url, version_tuple)
from update_client import validate_manifest

ARCHITECTURES = ('arm64', 'x86_64')
BUNDLE_ID = 'com.flickdock.app'


def _regular(path: Path, label: str) -> Path:
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise ReleaseError(f'{label} must be a regular local file')
    return path


def _validate_bundle(archive: Path, version: str, architecture: str) -> None:
    """Inspect metadata and the native header without extracting or executing."""
    try:
        with zipfile.ZipFile(archive) as package:
            def member(name, maximum):
                matches = [item for item in package.infolist() if item.filename == name]
                if len(matches) != 1 or matches[0].file_size > maximum:
                    raise ReleaseError('Mac ZIP has missing, duplicate or oversized application metadata')
                info = matches[0]
                if info.flag_bits & 1 or stat.S_ISLNK(info.external_attr >> 16):
                    raise ReleaseError('Mac ZIP application metadata cannot be encrypted or a link')
                return info
            info = plistlib.loads(package.read(member('FlickDock.app/Contents/Info.plist', 1024 * 1024)))
            if (not isinstance(info, dict) or info.get('CFBundleIdentifier') != BUNDLE_ID
                    or info.get('CFBundleExecutable') != 'FlickDock' or info.get('CFBundlePackageType') != 'APPL'
                    or info.get('CFBundleShortVersionString') != version or info.get('CFBundleVersion') != version):
                raise ReleaseError('Mac ZIP application identity/version does not match this release')
            binary = member('FlickDock.app/Contents/MacOS/FlickDock', 512 * 1024 * 1024)
            with package.open(binary) as executable:
                header = executable.read(32)
            if len(header) < 32 or header[:4] not in (b'\xcf\xfa\xed\xfe', b'\xfe\xed\xfa\xcf'):
                raise ReleaseError('Mac ZIP must contain a native 64-bit Mach-O executable')
            endian = '<' if header[:4] == b'\xcf\xfa\xed\xfe' else '>'
            cpu = struct.unpack(endian + 'I', header[4:8])[0]
            if cpu != {'arm64': 0x0100000c, 'x86_64': 0x01000007}[architecture]:
                raise ReleaseError('Mac ZIP native executable architecture does not match this channel')
    except (OSError, ValueError, KeyError, zipfile.BadZipFile, RuntimeError) as error:
        if isinstance(error, ReleaseError):
            raise
        raise ReleaseError('Cannot inspect the built Mac application ZIP') from error


def validate_reports(build_report: Path, cloud_report: Path, archive: Path,
                     version: str, architecture: str, size: int, digest: str) -> None:
    build, cloud = _json(build_report), _json(cloud_report)
    if (not isinstance(build, dict) or build.get('ok') is not True or build.get('version') != version
            or build.get('architecture') != architecture
            or not str(build.get('host', '')).lower().startswith(('macos-', 'darwin-'))):
        raise ReleaseError('A passing native Mac build report for this version and architecture is required')
    frozen = build.get('frozen')
    if (not isinstance(frozen, dict) or frozen.get('ok') is not True or frozen.get('version') != version
            or frozen.get('platform') != 'darwin' or frozen.get('bundle_id') != BUNDLE_ID
            or frozen.get('qt_platform') != 'offscreen'):
        raise ReleaseError('Native Mac build report must include the passing packaged application check')
    artifacts = build.get('artifacts')
    selected = [row for row in artifacts if isinstance(row, dict) and row.get('name') == archive.name] if isinstance(artifacts, list) else []
    if (len(selected) != 1 or type(selected[0].get('bytes')) is not int or selected[0]['bytes'] != size
            or str(selected[0].get('sha256', '')).lower() != digest):
        raise ReleaseError('Native build artifact list does not attest these exact Mac package bytes')
    if archive.suffix.lower() == '.dmg' and build.get('dmg_verified') is not True:
        raise ReleaseError('Native Mac build must verify the mounted DMG payload')
    if (not isinstance(cloud, dict) or cloud.get('ok') is not True or cloud.get('version') != version
            or cloud.get('frozen') is not True or cloud.get('mac_application_tested') is not True):
        raise ReleaseError('A passing actual frozen macOS cloud-sync report is required')


def verify_public_zip(url: str, digest: str, size: int, allowed_hosts: set[str], *, opener=None) -> dict:
    response, final_url = _public_response(url, allowed_hosts, opener)
    checksum = hashlib.sha256(); received = 0; prefix = bytearray()
    try:
        content_type = response.headers.get('Content-Type', '').lower().split(';', 1)[0]
        if content_type.startswith('text/') or content_type in ('application/json', 'application/xhtml+xml'):
            raise ReleaseError('Public Mac download is an HTML/login/text response')
        declared = response.headers.get('Content-Length')
        if declared is not None and (not declared.isdecimal() or int(declared) != size):
            raise ReleaseError('Public Mac ZIP Content-Length differs from the verified artifact')
        while chunk := response.read(1024 * 1024):
            if len(prefix) < 4:
                prefix.extend(chunk[:4-len(prefix)])
            received += len(chunk)
            if received > size:
                raise ReleaseError('Public Mac ZIP is larger than the verified artifact')
            checksum.update(chunk)
    finally:
        response.close()
    if bytes(prefix) != b'PK\x03\x04':
        raise ReleaseError('Public Mac download does not have a ZIP header')
    if received != size or checksum.hexdigest() != digest:
        raise ReleaseError('Public Mac ZIP size/SHA256 differs from the verified local artifact')
    return dict(url=url, final_url=final_url, size=received, sha256=checksum.hexdigest())


def verify_public_dmg(url: str, digest: str, size: int, allowed_hosts: set[str], *, opener=None) -> dict:
    response, final_url = _public_response(url, allowed_hosts, opener)
    checksum = hashlib.sha256(); received = 0; trailer = b''
    try:
        content_type = response.headers.get('Content-Type', '').lower().split(';', 1)[0]
        if content_type.startswith('text/') or content_type in ('application/json', 'application/xhtml+xml'):
            raise ReleaseError('Public Mac DMG download is an HTML/login/text response')
        declared = response.headers.get('Content-Length')
        if declared is not None and (not declared.isdecimal() or int(declared) != size):
            raise ReleaseError('Public Mac DMG Content-Length differs from the verified artifact')
        while chunk := response.read(1024 * 1024):
            received += len(chunk)
            if received > size:
                raise ReleaseError('Public Mac DMG is larger than the verified artifact')
            checksum.update(chunk)
            trailer = (trailer + chunk)[-512:]
    finally:
        response.close()
    if len(trailer) != 512 or trailer[:4] != b'koly':
        raise ReleaseError('Public Mac download does not have a DMG trailer')
    if received != size or checksum.hexdigest() != digest:
        raise ReleaseError('Public Mac DMG size/SHA256 differs from the verified local artifact')
    return dict(url=url, final_url=final_url, size=received, sha256=checksum.hexdigest())


def _baseline(value, architecture, approved):
    try:
        validate_manifest(value, approved, platform='darwin', machine=architecture)
    except (TypeError, ValueError) as error:
        raise ReleaseError('Previous Mac update index is invalid') from error
    if value.get('architecture') != architecture:
        raise ReleaseError('Previous Mac index belongs to another architecture')
    validate_url(value['installer']['url'])
    return value


def create_mac_release(*, archive: Path, build_report: Path, cloud_report: Path, version: str,
                       architecture: str, notes: str, download_url: str, output: Path,
                       previous: dict | None = None, initial_release=False,
                       allowed_hosts: set[str] | None = None, opener=None) -> dict:
    version_tuple(version)
    if architecture not in ARCHITECTURES:
        raise ReleaseError('Choose a native arm64 or x86_64 release channel')
    archive = _regular(archive, 'Mac package'); build_report = _regular(build_report, 'Mac build report')
    cloud_report = _regular(cloud_report, 'Mac cloud report'); output = Path(output)
    if archive.name not in (f'FlickDock-{version}-macOS-{architecture}.zip',
                            f'FlickDock-{version}-macOS-{architecture}.dmg'):
        raise ReleaseError('Mac package filename must identify this exact release and architecture')
    if output.name != f'macos-{architecture}.json' or output.is_symlink():
        raise ReleaseError('Output must be the matching macos-architecture.json channel index')
    if not isinstance(notes, str) or not notes.strip() or len(notes) > 32768:
        raise ReleaseError('Nonempty Mac release notes of at most 32768 characters are required')
    host = validate_url(download_url)
    approved = {host} if allowed_hosts is None else {str(entry).lower().rstrip('.') for entry in allowed_hosts}
    baselines = []
    if previous is not None:
        baselines.append(_baseline(previous, architecture, approved))
    old_index = output.read_bytes() if output.exists() else None
    if old_index is not None:
        baselines.append(_baseline(_json(output), architecture, approved))
    if not baselines and not initial_release:
        raise ReleaseError('Previous Mac index is required; first publication needs --initial-release')
    if any(version_tuple(version) < version_tuple(value['version']) for value in baselines):
        raise ReleaseError('Mac release version downgrade is forbidden')
    size = archive.stat().st_size; digest = sha256_file(archive)
    report_hashes = (sha256_file(build_report), sha256_file(cloud_report))
    validate_reports(build_report, cloud_report, archive, version, architecture, size, digest)
    if archive.suffix.lower() == '.zip':
        _validate_bundle(archive, version, architecture)
    else:
        with archive.open('rb') as image:
            image.seek(-512, 2)
            if image.read(4) != b'koly':
                raise ReleaseError('Mac DMG trailer is invalid')
    manifest = dict(schema_version=1, version=version, platform='darwin', architecture=architecture,
                    published_at=datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z'),
                    notes=notes.strip(), installer=dict(filename=archive.name, url=download_url, size=size, sha256=digest))
    try:
        validate_manifest(manifest, approved, platform='darwin', machine=architecture)
    except (TypeError, ValueError) as error:
        raise ReleaseError('Mac release manifest is not accepted by the shipped updater') from error
    for baseline in baselines:
        if baseline['version'] != version:
            continue
        if (any(baseline['installer'].get(key) != manifest['installer'][key] for key in ('url', 'size', 'sha256', 'filename'))
                or baseline['notes'] != manifest['notes']):
            raise ReleaseError('An existing Mac version cannot be repackaged or moved; publish a newer version')
        manifest = baseline
    (verify_public_dmg if archive.suffix.lower() == '.dmg' else verify_public_zip)(
        download_url, digest, size, approved, opener=opener)
    if archive.stat().st_size != size or sha256_file(archive) != digest:
        raise ReleaseError('Local Mac ZIP changed during anonymous verification')
    if (sha256_file(build_report), sha256_file(cloud_report)) != report_hashes:
        raise ReleaseError('Mac verification reports changed during anonymous verification')
    if (output.read_bytes() if output.exists() else None) != old_index:
        raise ReleaseError('Mac channel index changed during verification; retry against its latest baseline')
    # This is the only write: the per-architecture index is the commit point.
    _write_atomic(output, (json.dumps(manifest, ensure_ascii=False, indent=2) + '\n').encode('utf8'))
    return manifest


def main(arguments=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive', type=Path, required=True)
    parser.add_argument('--build-report', type=Path, required=True)
    parser.add_argument('--cloud-report', type=Path, required=True)
    parser.add_argument('--version', required=True)
    parser.add_argument('--architecture', choices=ARCHITECTURES, required=True)
    parser.add_argument('--notes', required=True)
    parser.add_argument('--download-url', required=True)
    parser.add_argument('--output-dir', type=Path, default=Path('updates'))
    parser.add_argument('--previous-manifest', type=Path)
    parser.add_argument('--initial-release', action='store_true')
    parser.add_argument('--allow-host', action='append', default=[])
    options = parser.parse_args(arguments)
    try:
        output = options.output_dir / f'macos-{options.architecture}.json'
        create_mac_release(archive=options.archive, build_report=options.build_report,
            cloud_report=options.cloud_report, version=options.version, architecture=options.architecture,
            notes=options.notes, download_url=options.download_url, output=output,
            previous=_json(options.previous_manifest) if options.previous_manifest else None,
            initial_release=options.initial_release, allowed_hosts=set(options.allow_host) or None)
    except (ReleaseError, OSError) as error:
        print(str(error), file=sys.stderr); return 1
    print(json.dumps(dict(version=options.version, architecture=options.architecture, manifest=str(output))))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
