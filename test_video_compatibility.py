"""Real synthetic media checks for the macOS compatibility step."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from video_worker import _mac_compatible_video,_mac_video_streams


class MacVideoCompatibilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        value=os.environ.get('FLICKDOCK_TEST_FFMPEG')
        if not value:raise unittest.SkipTest('Set FLICKDOCK_TEST_FFMPEG to an isolated FFmpeg executable')
        cls.ffmpeg=Path(value)
        if not cls.ffmpeg.is_file():raise unittest.SkipTest('FFmpeg fixture is unavailable')

    def run_ffmpeg(self,*args):
        result=subprocess.run([str(self.ffmpeg),'-hide_banner','-loglevel','error',*map(str,args)],
                              stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=60)
        self.assertEqual(result.returncode,0,result.stderr.decode('utf-8','replace'))
        return result.stdout

    def make(self,path,video,audio,extra=()):
        self.run_ffmpeg('-f','lavfi','-i','testsrc=size=128x72:rate=10','-f','lavfi','-i','sine=frequency=440',
                        '-t','0.6','-c:v',video,'-pix_fmt','yuv420p',*extra,'-c:a',audio,'-y',path)

    def frames(self,path):
        raw=self.run_ffmpeg('-i',path,'-map','0:v:0','-f','framemd5','-')
        return [line.rsplit(b',',1)[-1].strip() for line in raw.splitlines() if line and not line.startswith(b'#')]

    def test_h264_aac_mp4_is_left_untouched(self):
        with tempfile.TemporaryDirectory() as directory:
            source=Path(directory)/'already-compatible.mp4';self.make(source,'libx264','aac')
            before=source.read_bytes()
            with patch('video_worker.emit'):
                self.assertEqual(_mac_compatible_video(source,self.ffmpeg.parent),source)
            self.assertEqual(source.read_bytes(),before)

    def test_hevc_is_retagged_without_changing_decoded_frames(self):
        with tempfile.TemporaryDirectory() as directory:
            source=Path(directory)/'hevc.mp4';self.make(source,'libx265','aac',('-tag:v','hev1','-x265-params','log-level=error'))
            before=self.frames(source)
            with patch('video_worker.emit'):
                output=_mac_compatible_video(source,self.ffmpeg.parent)
            self.assertEqual(self.frames(output),before)
            probe=subprocess.run([str(self.ffmpeg),'-hide_banner','-i',str(output)],capture_output=True).stderr
            self.assertIn(b'hvc1',probe)
            self.assertEqual(_mac_video_streams(output,self.ffmpeg),([('hevc','yuv420p')],['aac']))

    def test_already_compatible_hevc_is_not_remuxed(self):
        with tempfile.TemporaryDirectory() as directory:
            source=Path(directory)/'hevc.mp4';self.make(source,'libx265','aac',('-tag:v','hvc1','-x265-params','log-level=error'))
            before=source.read_bytes()
            self.assertEqual(_mac_video_streams(source,self.ffmpeg,with_details=True)[2],['hvc1'])
            with patch('video_worker.emit'),patch('video_worker.subprocess.Popen',side_effect=AssertionError('Unnecessary remux')):
                with patch('video_worker._mac_video_streams',return_value=([('hevc','yuv420p')],['aac'],['hvc1'],600000)):
                    self.assertEqual(_mac_compatible_video(source,self.ffmpeg.parent),source)
            self.assertEqual(source.read_bytes(),before)

    def test_vp9_opus_is_converted_to_h264_aac(self):
        with tempfile.TemporaryDirectory() as directory:
            source=Path(directory)/'vp9.webm';self.make(source,'libvpx-vp9','libopus')
            progress=[];info={}
            with patch('video_worker.emit',side_effect=lambda kind,**value:progress.append(value)):
                output=_mac_compatible_video(source,self.ffmpeg.parent,info)
            self.assertFalse(source.exists());self.assertEqual(output.suffix,'.mp4')
            self.assertEqual(_mac_video_streams(output,self.ffmpeg),([('h264','yuv420p')],['aac']))
            self.assertEqual(info['video_codec'],'h264')
            self.assertGreater(len(self.frames(output)),0)
            self.assertTrue(progress);self.assertTrue(all(event['phase']=='converting' for event in progress))
            self.assertTrue(any(event['total'] for event in progress))
            self.assertTrue(all(0<=event['bytes']<=event['total'] for event in progress if event['total']))

    @unittest.skipUnless(os.name=='nt','Uses the Windows fixture to simulate an unavailable Mac encoder')
    def test_hardware_failure_falls_back_to_software_without_losing_video(self):
        with tempfile.TemporaryDirectory() as directory:
            source=Path(directory)/'vp9.webm';self.make(source,'libvpx-vp9','libopus');events=[]
            with patch('video_worker._use_mac_hardware',return_value=True), \
                    patch('video_worker.emit',side_effect=lambda kind,**value:events.append(value)):
                output=_mac_compatible_video(source,self.ffmpeg.parent)
            self.assertEqual([event['encoder'] for event in events if event['bytes']==0],['hardware','software'])
            self.assertEqual(_mac_video_streams(output,self.ffmpeg),([('h264','yuv420p')],['aac']))
            self.assertGreater(len(self.frames(output)),0)

    def test_h264_444_is_converted_to_a_compatible_pixel_format(self):
        with tempfile.TemporaryDirectory() as directory:
            source=Path(directory)/'h264-444.mp4';self.make(source,'libx264','aac',('-pix_fmt','yuv444p'))
            self.assertEqual(_mac_video_streams(source,self.ffmpeg)[0],[('h264','yuv444p')])
            with patch('video_worker.emit'):
                output=_mac_compatible_video(source,self.ffmpeg.parent)
            self.assertEqual(_mac_video_streams(output,self.ffmpeg),([('h264','yuv420p')],['aac']))
            self.assertGreater(len(self.frames(output)),0)

    def test_failed_conversion_keeps_original_and_does_not_publish_partial_output(self):
        with tempfile.TemporaryDirectory() as directory:
            source=Path(directory)/'vp9.webm';self.make(source,'libvpx-vp9','libopus')
            original=source.read_bytes()
            with patch('video_worker.emit'),patch('video_worker._mac_video_streams',return_value=([('vp9','yuv420p')],['opus'],[''],600000)), \
                    patch('video_worker.subprocess.Popen',side_effect=OSError('encoder unavailable')):
                with self.assertRaises(OSError):_mac_compatible_video(source,self.ffmpeg.parent)
            self.assertEqual(source.read_bytes(),original)
            self.assertEqual(list(Path(directory).iterdir()),[source])


if __name__=='__main__':unittest.main()
