"""Prepare the pinned native executable for source and CI verification only."""
from pathlib import Path
import shutil
import sys
from video_runtime import ensure_video_runtime

def prepare():
    runtime=ensure_video_runtime();name='ffmpeg.exe' if sys.platform=='win32' else 'ffmpeg'
    target=Path(__file__).parent/'assets/video-tools';target.mkdir(parents=True,exist_ok=True)
    shutil.copy2(runtime/name,target/name)
    print('Verified optional FFmpeg runtime prepared for native tests')

if __name__=='__main__':prepare()
