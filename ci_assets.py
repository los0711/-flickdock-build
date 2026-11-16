"""Confirm bundled Latin faces and their licenses are present in both builds."""
from pathlib import Path


def main():
    root = Path(__file__).resolve().parent
    fonts=('Inter-Regular.ttf','Inter-Medium.ttf','Inter-SemiBold.ttf',
           'JetBrainsMono-Regular.ttf','JetBrainsMono-Medium.ttf')
    for name in fonts:
        if not (root/'assets'/'fonts'/name).is_file():
            raise RuntimeError('Missing bundled font: '+name)
    for name in ('Inter-OFL.txt','JetBrainsMono-OFL.txt'):
        if not (root/'licenses'/name).is_file():
            raise RuntimeError('Missing font license: '+name)
    for name in ('FlickDock.spec', 'FlickDock-mac.spec'):
        spec = (root / name).read_text(encoding='utf8')
        if 'OPPOSans4.ttf' in spec or any(font not in spec for font in fonts):
            raise RuntimeError('Unexpected font bundle in '+name)
    print('Bundled Latin fonts and system Chinese font configuration verified')


if __name__ == '__main__':
    main()
