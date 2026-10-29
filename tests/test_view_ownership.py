from trace_api_chain.object_capture import Recorder


class NoScan(dict):
    def __iter__(self):
        raise AssertionError('retirement scanned unrelated live frames')


def test_return_retires_exact_owner_without_scanning_other_frames():
    recorder=Recorder.__new__(Recorder)
    recorder.views=NoScan();recorder.view_keys={}
    for uid in range(1000):
        for scope in ('frame','return','native','global:registry'):
            recorder.remember_view((uid,scope),(uid,scope))
    recorder.remember_view((7,'frame'),('latest',))
    recorder.retire_views(7)
    assert len(recorder.views)==3996
    assert 7 not in recorder.view_keys
    assert all((uid,scope) in recorder.views for uid in (0,6,8,999)
               for scope in ('frame','return','native','global:registry'))
    recorder.retire_views(7)
    assert len(recorder.views)==3996
    for uid in range(1000):recorder.retire_views(uid)
    assert recorder.views=={} and recorder.view_keys=={}
