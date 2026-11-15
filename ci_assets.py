"""Confirm that CI builds use OS-provided fonts without downloading a bundle."""
from pathlib import Path


def main():
    root = Path(__file__).resolve().parent
    for name in ('FlickDock.spec', 'FlickDock-mac.spec'):
        spec = (root / name).read_text(encoding='utf8')
        if 'OPPOSans4.ttf' in spec or 'Inter-Regular.ttf' in spec:
            raise RuntimeError('The build unexpectedly includes a retired font bundle')
    print('System font configuration verified; no font download required')


if __name__ == '__main__':
    main()
