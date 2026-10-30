"""Same-input memory/IO benchmark; temporary images only, no native app state."""
import argparse,ast,gc,hashlib,json,os,subprocess,tempfile,time,tracemalloc,types
from pathlib import Path
from types import SimpleNamespace
os.environ['QT_QPA_PLATFORM']='offscreen'
from PySide6.QtCore import Qt
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication
from performance_metrics import process_sample


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--baseline',action='store_true')
    parser.add_argument('--output',type=Path,required=True);args=parser.parse_args()
    def source(name):
        return (subprocess.check_output(['git','show','HEAD:'+name]).decode('utf8') if args.baseline
                else Path(name).read_text(encoding='utf8'))
    app=QApplication([]);decode_source=source('async_images.py');namespace={}
    exec(compile(decode_source,'async_images.py','exec'),namespace);decode=namespace['_decode']
    tree=ast.parse(source('flickdock.py'))
    method=next(node for item in tree.body if isinstance(item,ast.ClassDef) and item.name=='PreviewPane'
                for node in item.body if isinstance(node,ast.FunctionDef) and node.name=='_poster_ready')
    globals_=dict(Qt=Qt,QImage=QImage);exec(compile(ast.Module(body=[method],type_ignores=[]),'poster_ready.py','exec'),globals_)
    poster_ready=globals_['_poster_ready']
    result=dict(mode='baseline_HEAD_1.8.21' if args.baseline else 'working_tree',
        conditions=dict(qt_platform=app.platformName(),poster_count=8,poster_resolution=[3840,2160],
                        image_target=128,encoded_bytes=16*1024*1024),
        source_sha256=hashlib.sha256((decode_source+ast.unparse(method)).encode()).hexdigest())
    with tempfile.TemporaryDirectory(prefix='flickdock-resources-') as directory:
        path=Path(directory)/'large.jpg';image=QImage(3000,2000,QImage.Format_RGB32);image.fill(Qt.blue)
        assert image.save(str(path),'JPEG');image=QImage()
        with path.open('ab') as stream:stream.write(b'\0'*(16*1024*1024-path.stat().st_size))
        result['before_decode']=process_sample();tracemalloc.start();started=time.perf_counter()
        import threading
        decoded=decode(str(path),128,threading.Event())
        result['decode_ms']=(time.perf_counter()-started)*1000
        result['python_decode_current_bytes'],result['python_decode_peak_bytes']=tracemalloc.get_traced_memory()
        tracemalloc.stop();result['after_decode']=process_sample()
        result['decoded_dimensions']=[decoded.width(),decoded.height()];decoded=QImage();gc.collect()
        cache={};fake=SimpleNamespace(shelf=SimpleNamespace(preview_cache=cache,
            zones={'permanent':SimpleNamespace(refresh=lambda:None)}),popup=SimpleNamespace(isVisible=lambda:False))
        poster=QImage(3840,2160,QImage.Format_RGBA8888);poster.fill(Qt.blue)
        result['before_posters']=process_sample();started=time.perf_counter()
        for index in range(8):poster_ready(fake,str(index),poster)
        result['poster_store_ms']=(time.perf_counter()-started)*1000
        result['poster_cache_bytes']=sum(image.sizeInBytes() for image in cache.values())
        result['poster_cache_entries']=len(cache);result['after_posters']=process_sample()
        cache.clear();poster=QImage();gc.collect();result['after_release']=process_sample()
    args.output.parent.mkdir(parents=True,exist_ok=True);args.output.write_text(json.dumps(result,indent=2),encoding='utf8')
    print(json.dumps({key:result[key] for key in ('mode','decode_ms','python_decode_peak_bytes','poster_cache_bytes','poster_store_ms')}))


if __name__=='__main__':main()
