import json
import pytest
import torch
import scenario_harness as h

torch.set_num_threads(1)

@pytest.fixture
def selected(request):
    # Execute the same method once before tracing to warm all paths it reaches.
    # Known failing methods still execute again under capture; failure is not hidden.
    warmup={'id':request.param}
    try:
        with h.prepared_case(request.param) as (row, method):
            method()
            h.synchronize(row)
        warmup['status']='passed'
    except Exception as error:
        warmup.update(status='failed',reason=str(error)[:3000])
    out=h.ROOT/'warmup-evidence';out.mkdir(exist_ok=True)
    (out/(request.param+'.json')).write_text(json.dumps(warmup,indent=2))
    with h.prepared_case(request.param) as value:
        yield value

@pytest.mark.parametrize('selected',list(h.CASES),indirect=True)
def test_selected(selected):
    row,method=selected
    method()
    h.synchronize(row)
