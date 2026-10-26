"""Fetch the exact unmodified upstream font when absent in a private CI mirror."""
import hashlib
import json
from pathlib import Path
import urllib.request


def main():
    root=Path(__file__).resolve().parent
    manifest=json.loads((root/'font_manifest.json').read_text(encoding='utf8'))
    for record in manifest['fonts']:
        target=root/record['file']
        if not target.is_file():
            url=record['source']
            if not url.startswith('https://raw.githubusercontent.com/openharmony/resources/'):
                raise ValueError('Unapproved CI font origin')
            with urllib.request.urlopen(url,timeout=60) as reply:data=reply.read(record['bytes']+1)
            if len(data)!=record['bytes'] or hashlib.sha256(data).hexdigest()!=record['sha256']:
                raise ValueError('Upstream font differs from reviewed distribution')
            target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(data)
        if target.stat().st_size!=record['bytes'] or hashlib.sha256(target.read_bytes()).hexdigest()!=record['sha256']:
            raise ValueError('CI font validation failed')
    print('Original font verified against exact size/SHA256')


if __name__=='__main__':main()
