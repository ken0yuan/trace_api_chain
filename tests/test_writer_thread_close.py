import threading
from trace_api_chain.dispatch import ParallelReplay


def test_connection_finalizer_runs_once_on_the_single_writer_thread():
    calls = []
    def commit(item, prepared):
        calls.append(('commit', item, threading.get_ident()))
    def close():
        calls.append(('close', None, threading.get_ident()))
    main = threading.get_ident()
    pipeline = ParallelReplay(lambda x: x, commit,
                              lambda *args: (_ for _ in ()).throw(AssertionError(args)),
                              workers=2, ready_limit=2, on_writer_close=close)
    pipeline.submit('first')
    pipeline.submit('second')
    pipeline.close()
    assert [row[0] for row in calls] == ['commit', 'commit', 'close']
    assert len({row[2] for row in calls}) == 1
    assert calls[-1][2] != main
