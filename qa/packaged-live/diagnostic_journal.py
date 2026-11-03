"""Bounded QA-only IO journal; never spend a UIA request budget on logging."""
import json,queue,threading

MAX_PENDING_RECORDS=8192
CLOSE_SECONDS=3.

class JournalUnavailable(RuntimeError):
    def __init__(self,code):self.code=code;super().__init__('QA_DIAGNOSTIC_JOURNAL_'+code)

class DiagnosticJournal:
    def __init__(self,*,capacity=MAX_PENDING_RECORDS,write=None):
        self.queue=queue.Queue(capacity);self.lock=threading.Lock();self.closed=False;self.failure=None
        self.stop=threading.Event()
        self.submitted=0;self.completed=0;self.write=write or self._write
        self.thread=threading.Thread(target=self._run,name='FlickDock-QA-diagnostic-journal',daemon=True);self.thread.start()
    @staticmethod
    def _write(path,row,prefix):
        encoded=json.dumps(row)
        with path.open('a',encoding='utf8') as stream:stream.write(encoded+'\n')
        if prefix is not None:print(prefix+encoded,flush=True)
    def submit(self,path,row,prefix=None):
        # Serialize an owned copy before queueing; later caller changes must not
        # rewrite a past stage. Capacity/error are visible failures, never drops.
        owned=json.loads(json.dumps(row))
        with self.lock:
            if self.closed:raise JournalUnavailable('CLOSED')
            if self.failure is not None:raise JournalUnavailable('WRITE_FAILED')
            try:self.queue.put_nowait((path,owned,prefix))
            except queue.Full:
                self.failure='QUEUE_FULL';raise JournalUnavailable('FULL') from None
            self.submitted+=1
    def _run(self):
        while True:
            try:item=self.queue.get(timeout=.05)
            except queue.Empty:
                if self.stop.is_set():return
                continue
            try:self.write(*item)
            except Exception as error:
                # This is the external diagnostic IO boundary. Keep a fixed
                # type and outstanding count, without exporting exception text.
                with self.lock:self.failure=type(error).__name__
                return
            with self.lock:self.completed+=1
    def close(self,seconds=CLOSE_SECONDS):
        with self.lock:
            if not self.closed:
                self.closed=True;self.stop.set()
        self.thread.join(seconds)
        with self.lock:
            return dict(complete=not self.thread.is_alive() and self.failure is None and self.submitted==self.completed,
                submitted=self.submitted,completed=self.completed,pending=self.submitted-self.completed,
                writer_still_running=self.thread.is_alive(),error_type=self.failure)
