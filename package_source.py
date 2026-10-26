"""Create the private source archive from the reviewed release allowlist."""
from pathlib import Path
from app_version import APP_VERSION
from release_manifest import collect_source_files, source_archive

VERSION = APP_VERSION
work = Path(__file__).resolve().parent
output = work.parent / 'outputs' / f'FlickDock-{VERSION}-完整源码.zip'


def collect_sources():
    return [(work / name, f'FlickDock-{VERSION}/{name}')
            for name, _ in collect_source_files(work) if (work / name).is_file()]


def package_source():
    data, _ = source_archive(work, VERSION)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(data)
    return output


if __name__ == '__main__':
    result = package_source()
    print(result.name, result.stat().st_size)
