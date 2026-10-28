import threading
from trace_api_chain.dispatch import ParallelReplay

def test_capture_submission_does_not_wait_for_full_ready_stage():
    release=threading.Event();writer_started=threading.Event();submitted=threading.Event();done=[]
    def commit(item,data):writer_started.set();assert release.wait(10);done.append(data)
    pipeline=ParallelReplay(lambda x:x,commit,lambda *args:None,workers=1,ready_limit=1)
    def capture():
        for i in range(40):pipeline.submit(i)
        submitted.set()
    thread=threading.Thread(target=capture);thread.start()
    try:
        assert writer_started.wait(5);assert submitted.wait(5)
    finally:release.set();thread.join();pipeline.close()
    assert sorted(done)==list(range(40)) and not pipeline.errors

def test_writer_consumes_ready_result_without_waiting_for_slow_earlier_test():
    slow=threading.Event();fast_written=threading.Event();done=[]
    def prepare(item):
        if item=='slow':assert slow.wait(10)
        return item
    def commit(item,data):
        done.append(data)
        if item=='fast':fast_written.set()
    p=ParallelReplay(prepare,commit,lambda *args:None,workers=2,ready_limit=2)
    p.submit('slow');p.submit('fast')
    try:assert fast_written.wait(5)
    finally:slow.set();p.close()
    assert done==['fast','slow']

def test_eight_independent_preparers_overlap_and_errors_do_not_deadlock(tmp_path):
    barrier=threading.Barrier(8);done=[];errors=[]
    def prepare(item):
        folder=tmp_path/str(item);folder.mkdir();(folder/'source.json').write_text(str(item))
        barrier.wait(10);return (folder/'source.json').read_text()
    p=ParallelReplay(prepare,lambda item,data:done.append((item,data)),lambda item,error:errors.append(item))
    for i in range(8):p.submit(i)
    p.close();assert len(done)==8 and not errors
    assert {value for _,value in done}==set(map(str,range(8)))
    def fail(item):
        if item==0:raise ValueError('forced prepare failure')
        return item
    p=ParallelReplay(fail,lambda item,data:done.append((item,data)),lambda item,error:errors.append(item),workers=1,ready_limit=1)
    p.submit(0);p.submit(1);p.close();assert errors==[0] and done[-1]==(1,1)
