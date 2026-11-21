"""Verify the bundled technical fonts and license for both builds."""
import re
from pathlib import Path


def main():
    root = Path(__file__).resolve().parent
    fonts={'JetBrainsMono-Regular.ttf','JetBrainsMono-Medium.ttf'}
    font_dir=root/'assets'/'fonts'
    if {path.name for path in font_dir.iterdir() if path.suffix.lower() in ('.ttf','.otf','.ttc')}!=fonts:
        raise RuntimeError('Unexpected bundled font files')
    license_name='JetBrainsMono-OFL.txt'
    if not (root/'licenses'/license_name).is_file():
        raise RuntimeError('Missing font license: '+license_name)
    for name in ('FlickDock.spec', 'FlickDock-mac.spec'):
        spec = (root / name).read_text(encoding='utf8')
        if set(re.findall(r"'([^']+\.(?:ttf|otf|ttc))'",spec))!=fonts or license_name not in spec:
            raise RuntimeError('Unexpected font bundle in '+name)
    print('Bundled technical fonts and license verified')


if __name__ == '__main__':
    main()
