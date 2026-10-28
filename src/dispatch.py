"""Capture producers never wait for offline normalization or database writes."""
from concurrent.futures import ThreadPoolExecutor
import queue
import threading

class ParallelReplay:
    def __init__(self,prepare,commit,on_error,workers=8,ready_limit=16):
        if workers<1 or ready_limit<workers:raise ValueError('invalid_parallel_stage_limits')
        self.prepare=prepare;self.commit=commit;self.on_error=on_error
        self.condition=threading.Condition();self.pending=0;self.closed=False;self.errors=[]
        self.admission=threading.Semaphore(ready_limit);self.ready=queue.Queue()
        self.pool=ThreadPoolExecutor(max_workers=workers,thread_name_prefix='offline-prepare')
        self.writer=threading.Thread(target=self._write,name='sqlite-writer');self.writer.start()

    def submit(self,item):
        with self.condition:
            if self.closed:raise RuntimeError('parallel_replay_closed')
            self.pending+=1
            try:future=self.pool.submit(self._prepare,item)
            except BaseException:
                self.pending-=1;self.condition.notify_all();raise
        future.add_done_callback(lambda f:self.ready.put((item,f)))
        return future

    def _prepare(self,item):
        # This limit bounds derived artifacts, never capture producers.
        self.admission.acquire()
        return self.prepare(item)

    def _write(self):
        while True:
            entry=self.ready.get()
            if entry is None:return
            item,future=entry
            try:self.commit(item,future.result())
            except BaseException as error:
                self.errors.append((item,error))
                try:self.on_error(item,error)
                except BaseException as nested:self.errors.append((item,nested))
            finally:
                self.admission.release()
                with self.condition:self.pending-=1;self.condition.notify_all()

    def join(self):
        with self.condition:
            while self.pending:self.condition.wait()

    def close(self):
        with self.condition:self.closed=True
        self.join();self.pool.shutdown(wait=True);self.ready.put(None);self.writer.join()
