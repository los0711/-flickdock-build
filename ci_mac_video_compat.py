"""Check synthetic compatibility outputs with macOS's native video decoder."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest.mock import patch

from video_worker import _mac_compatible_video

if sys.platform!='darwin':raise SystemExit('This check requires macOS.')
ffmpeg=Path(__file__).resolve().parent/'assets/video-tools/ffmpeg'
assert ffmpeg.is_file()
swift=r'''
import Foundation
import AVFoundation
import CoreVideo
let asset = AVURLAsset(url: URL(fileURLWithPath: CommandLine.arguments[1]))
guard let video = asset.tracks(withMediaType: .video).first else { fatalError("Missing video track") }
let reader = try AVAssetReader(asset: asset)
let output = AVAssetReaderTrackOutput(track: video, outputSettings: [
    kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32BGRA
])
guard reader.canAdd(output) else { fatalError("Cannot add video decoder") }
reader.add(output)
guard reader.startReading(), output.copyNextSampleBuffer() != nil else {
    fatalError("Could not decode video frame: \(String(describing: reader.error))")
}
print("native-video-frame-ok")
'''
with tempfile.TemporaryDirectory(prefix='flickdock-mac-video-compat-') as directory:
    root=Path(directory);script=root/'decode.swift';script.write_text(swift,encoding='utf8')
    for name,encoder,extension,audio,extra in (
        ('hevc','libx265','.mp4','aac',['-pix_fmt','yuv420p','-tag:v','hev1','-x265-params','log-level=error']),
        ('vp9','libvpx-vp9','.webm','libopus',['-pix_fmt','yuv420p']),
    ):
        source=root/(name+extension)
        subprocess.run([str(ffmpeg),'-hide_banner','-loglevel','error','-f','lavfi','-i','testsrc=size=128x72:rate=10',
                        '-f','lavfi','-i','sine=frequency=440','-t','.6','-c:v',encoder,*extra,'-c:a',audio,
                        '-y',str(source)],check=True,timeout=60)
        with patch('video_worker.emit'):
            output=_mac_compatible_video(source,ffmpeg.parent)
        result=subprocess.run(['swift',str(script),str(output)],capture_output=True,text=True,timeout=90)
        if result.returncode or 'native-video-frame-ok' not in result.stdout:
            raise RuntimeError(name+' native decoding failed: '+result.stderr[-4000:])
        print(name+': native video frame decoded',flush=True)
