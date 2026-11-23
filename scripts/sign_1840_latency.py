"""Sign the already verified FlickDock 1.8.40 public installers."""

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
from pathlib import Path

from release_history import manifest_notes, release_entries
from release_macos import verify_public_dmg, verify_public_zip
from release_manifest import verify_public_file
from release_signing import publisher_signer
from update_client import MAX_MANIFEST_BYTES, validate_manifest
from update_signatures import sign_manifest, verify_manifest_signature


VERSION = '1.8.40'
HOST = 'download.flickdock.cc.cd'
NOTES = '恢复文件列表与卡片的转场和按压回弹，改善总览滚动与实时翻译响应。Mac 恢复辅助功能引导中的旧条目移除步骤，并修复滚动后点击剪贴板历史卡片时可能丢失点击的问题。'
WINDOWS = ('FlickDock-Setup-1.8.40-latency.exe', 70603346, 'e0e0ffde9fa5bcb7791fee9c17756bc81344f0357d4d155c3de407382251ebce')
ZIP = ('FlickDock-1.8.40-macOS-arm64-latency.zip', 82370133, '8d1a553bc4ac2e5d7eacbe8cabff35c73e9b14880f3da1e7c2d52c19549aca37')
DMG = ('FlickDock-1.8.40-macOS-arm64-latency.dmg', 97330269, '85d4f471f01ec9a5b44f14ff7364ee7f54ba2ab4d9f9142e7dc80a835934ed3a')
FEEDS = {
    'latest.json': (WINDOWS, 'win32'),
    'macos-arm64-auto-v2.json': (ZIP, 'darwin'),
    'macos-arm64-auto.json': (DMG, 'darwin'),
    'macos-arm64.json': (DMG, 'darwin'),
}


def installer(artifact):
    name, size, digest = artifact
    return dict(filename=name, url=f'https://{HOST}/v{VERSION}/{name}', size=size, sha256=digest)


def validate(value, platform, *, signed):
    if signed:
        verify_manifest_signature(value, required=True)
    validate_manifest(value, platform=platform, machine='arm64')


def write_json(path, value):
    content = (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
    if len(content) > MAX_MANIFEST_BYTES:
        raise ValueError(f'{path.name} exceeds the desktop updater size limit')
    path.write_bytes(content)


def prepare():
    with ThreadPoolExecutor(max_workers=3) as pool:
        checks = []
        for artifact, verify in ((WINDOWS, verify_public_file), (ZIP, verify_public_zip), (DMG, verify_public_dmg)):
            info = installer(artifact)
            checks.append(pool.submit(verify, info['url'], info['sha256'], info['size'], {HOST}))
        for check in checks:
            print(check.result())

    directory = Path('prepared-updates')
    directory.mkdir(exist_ok=True)
    published_at = datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')
    for name, (artifact, platform) in FEEDS.items():
        previous = json.loads((Path('updates') / name).read_text(encoding='utf-8'))
        validate(previous, platform, signed=True)
        if previous['version'] != VERSION or previous['installer']['sha256'] == artifact[2]:
            raise ValueError(f'Unexpected prior version in {name}')
        value = dict(schema_version=1, version=VERSION, published_at=published_at,
                     notes=manifest_notes(VERSION, NOTES), releases=release_entries(VERSION, NOTES),
                     installer=installer(artifact))
        if platform == 'darwin':
            value.update(platform=platform, architecture='arm64')
        validate(value, platform, signed=False)
        write_json(directory / name, value)


def sign():
    signer = publisher_signer()
    directory = Path('signed-updates')
    directory.mkdir(exist_ok=True)
    for name, (artifact, platform) in FEEDS.items():
        value = json.loads((Path('prepared-updates') / name).read_text(encoding='utf-8'))
        if value['version'] != VERSION or value['installer'] != installer(artifact):
            raise ValueError(f'Unexpected installer in {name}')
        signed = sign_manifest(value, signer)
        validate(signed, platform, signed=True)
        write_json(directory / name, signed)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=('prepare', 'sign'))
    {'prepare': prepare, 'sign': sign}[parser.parse_args().action]()
