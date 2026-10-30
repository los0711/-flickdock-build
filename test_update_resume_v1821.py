"""Production Qt downloads over real isolated HTTPS, including deliberate cuts."""
import copy,hashlib,os,time,unittest
import threading,itertools
from unittest.mock import patch
from pathlib import Path
os.environ['QT_QPA_PLATFORM']='offscreen'
import test_update_client_v170 as fixture
from PySide6.QtCore import QTimer


class UpdateRecoveryTests(fixture.UpdateTests):
    def task(self,**route):
        self.routes['/setup.exe']=dict(body=self.payload,ranges=True,**route)
        client,state=self.client();client.download(self.manifest)
        return client,state

    def finish(self,state):
        fixture.wait_until(lambda:state['failed'] or state['verified_file'],7000)
        self.assertFalse(state['failed'],state['failed'])
        self.assertEqual(Path(state['verified_file'][0][0]).read_bytes(),self.payload)

    def test_disconnection_automatically_resumes_from_actual_written_bytes(self):
        self.requests.clear();client,state=self.task(drop_once=262144)
        phases=[];client.download_status.connect(lambda phase,data:phases.append((phase,data)))
        self.finish(state)
        requests=[headers for path,headers in self.requests if path=='/setup.exe']
        self.assertGreaterEqual(len(requests),2)
        self.assertTrue(any(int(row.get('Range','bytes=0-').split('=')[1][:-1])>0 for row in requests))
        self.assertIn('reconnecting',[phase for phase,_ in phases]);self.assertIn('verifying',[phase for phase,_ in phases])
        self.assertEqual(state['progress'][-1][0],len(self.payload))

    def test_app_shutdown_preserves_partial_then_next_client_resumes(self):
        client,state=self.task(chunk_delay=.004)
        fixture.wait_until(lambda:client._download_task.received>=131072)
        part=client._download_task.part;client.close();self.assertTrue(part.exists())
        stored=part.stat().st_size;self.assertGreater(stored,0)
        self.requests.clear();second,state2=self.client();second.download(self.manifest);self.finish(state2)
        headers=next(headers for path,headers in self.requests if path=='/setup.exe')
        self.assertEqual(headers.get('Range'),f'bytes={stored}-');self.assertFalse(part.exists())

    def test_controller_shutdown_preserves_partial_through_actual_quit_path(self):
        from update_controller import UpdateController
        client,state=self.client()
        controller=UpdateController('1.9.9',Path(self.folder.name),client=client,platform='win32')
        self.routes['/setup.exe']=dict(body=self.payload,ranges=True,chunk_delay=.004)
        client.download(self.manifest)
        fixture.wait_until(lambda:client._download_task.received>=131072)
        part=client._download_task.part;controller.close()
        self.assertTrue(part.exists());stored=part.stat().st_size;self.requests.clear()
        second,state2=self.client();second.download(self.manifest);self.finish(state2)
        headers=next(headers for path,headers in self.requests if path=='/setup.exe')
        self.assertEqual(headers.get('Range'),f'bytes={stored}-')

    def test_range_unsupported_correctly_restarts_without_duplicate_bytes(self):
        client,state=self.client();folder=client.updates_dir/self.manifest['version'];folder.mkdir(parents=True)
        part=folder/('package-'+self.manifest['installer']['sha256']+'.part');part.write_bytes(self.payload[:131072])
        self.routes['/setup.exe']=dict(body=self.payload)
        client.download(self.manifest);self.finish(state)

    def test_pause_retains_bytes_and_resume_uses_range(self):
        client,state=self.task(chunk_delay=.004)
        fixture.wait_until(lambda:client._download_task.received>=131072)
        task=client._download_task;part=task.part
        self.assertTrue(client.pause());self.assertFalse(client.busy)
        stored=part.stat().st_size;self.requests.clear()
        client.download(self.manifest);self.finish(state)
        headers=next(headers for path,headers in self.requests if path=='/setup.exe')
        self.assertEqual(headers.get('Range'),f'bytes={stored}-')

    def test_controller_paused_download_can_be_cancelled_from_shared_view(self):
        from update_controller import UpdateController
        from update_dialog import UpdateDialog
        client,state=self.client();controller=UpdateController('1.9.9',Path(self.folder.name),client=client,platform='win32')
        view=UpdateDialog('1.9.9');view.bind_controller(controller)
        self.addCleanup(view.deleteLater);self.addCleanup(controller.close)
        controller._manifest=copy.deepcopy(self.manifest);controller._authorized=True
        self.routes['/setup.exe']=dict(body=self.payload,ranges=True,chunk_delay=.004)
        controller._download_current_manifest();fixture.wait_until(lambda:client._download_task.received>=131072)
        part=client._download_task.part;view.secondary_button.click()
        self.assertEqual(controller.state,'paused');self.assertEqual(view.state,'paused');self.assertTrue(part.exists())
        view.secondary_button.click()
        self.assertEqual(controller.state,'cancelled');self.assertEqual(view.state,'cancelled');self.assertFalse(part.exists())
        self.assertTrue(view.primary_button.isEnabled());self.assertTrue(view.secondary_button.isEnabled())

    def test_invalid_content_range_discards_partial_and_never_publishes(self):
        client,state=self.client();folder=client.updates_dir/self.manifest['version'];folder.mkdir(parents=True)
        part=folder/('package-'+self.manifest['installer']['sha256']+'.part');part.write_bytes(self.payload[:131072])
        self.routes['/setup.exe']=dict(body=self.payload,ranges=True,bad_range=True)
        client.download(self.manifest);fixture.wait_until(lambda:state['failed'])
        self.assertFalse(state['verified_file']);self.assertFalse(part.exists());self.assertIn('范围',state['failed'][0][0])

    def test_slow_continuous_download_is_not_a_total_elapsed_timeout(self):
        self.routes['/setup.exe']=dict(body=self.payload,chunk_delay=.008)
        client,state=self.client(timeout=100);beats=[];timer=QTimer();timer.setInterval(10);timer.timeout.connect(lambda:beats.append(time.monotonic()));timer.start()
        phases=[];client.download_status.connect(lambda phase,data:phases.append((phase,data)))
        started=time.monotonic();client.download(self.manifest);self.finish(state);timer.stop()
        self.assertGreater(time.monotonic()-started,.3);self.assertGreater(len(beats),10)
        self.assertTrue(any(data['speed']>0 and data['eta'] is not None for phase,data in phases if phase=='downloading'))

    def test_exhausted_retries_keep_valid_partial_for_manual_resume(self):
        client,state=self.client();folder=client.updates_dir/self.manifest['version'];folder.mkdir(parents=True)
        part=folder/('package-'+self.manifest['installer']['sha256']+'.part');part.write_bytes(self.payload[:131072])
        self.routes['/setup.exe']=dict(status=503,body=b'temporary unavailable')
        client.download(self.manifest);client._download_task.max_retries=1
        fixture.wait_until(lambda:state['failed'],4000);self.assertEqual(part.read_bytes(),self.payload[:131072])
        self.assertFalse(state['verified_file'])
        self.routes['/setup.exe']=dict(body=self.payload,ranges=True)
        state['failed'].clear();client.download(self.manifest);self.finish(state)

    def test_old_cancelled_inspection_cannot_delete_the_new_tasks_partial(self):
        import update_download
        client,state=self.client();folder=client.updates_dir/self.manifest['version'];folder.mkdir(parents=True)
        part=folder/('package-'+self.manifest['installer']['sha256']+'.part');part.write_bytes(self.payload[:131072])
        self.routes['/setup.exe']=dict(body=self.payload,ranges=True)
        original=update_download._inspect;counter=itertools.count();entered=[threading.Event(),threading.Event()];release=[threading.Event(),threading.Event()]
        def inspect(path,limit,cancel):
            index=next(counter);value=original(path,limit,cancel)
            if index<2:entered[index].set();release[index].wait(4)
            return value
        with patch.object(update_download,'_inspect',side_effect=inspect):
            try:
                client.download(self.manifest);old=client._download_task;fixture.wait_until(entered[0].is_set)
                client.cancel();client.download(self.manifest);fixture.wait_until(entered[1].is_set)
                release[0].set();fixture.wait_until(lambda:not old.jobs)
                self.assertTrue(part.exists());self.assertEqual(part.stat().st_size,131072)
                release[1].set();self.finish(state)
            finally:
                for event in release:event.set()

    def test_stalled_stream_speed_decays_to_zero_instead_of_freezing(self):
        from update_download import UpdateDownload
        client,state=self.client();task=UpdateDownload(client.network,self.manifest,Path(self.folder.name),'win32','1.9.9',{'127.0.0.1'})
        rows=[];task.status.connect(lambda phase,value:rows.append(value))
        task.received=1048576;task.samples.extend([(0,0),(1,1048576)])
        with patch('update_download.time.monotonic',return_value=20):task._emit('downloading')
        self.assertEqual(rows[-1]['speed'],0);self.assertIsNone(rows[-1]['eta']);task.deleteLater()


if __name__=='__main__':unittest.main()
