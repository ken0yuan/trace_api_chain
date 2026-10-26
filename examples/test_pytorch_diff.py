"""Real PyTorch API calls for upstream PR #201098, without executing compile.

These tests exercise public flex_attention validation with actual CPU/CUDA
tensors. The expected rank error occurs after the two changed helper functions.
No PyTorch function is replaced, patched, or called through a fake API.
"""
import pytest
import torch
from torch.nn.attention.flex_attention import flex_attention


@pytest.fixture(params=['cpu','cuda'])
def inputs(request):
    if request.param=='cuda' and not torch.cuda.is_available():
        pytest.skip('CUDA is unavailable')
    # Allocate outside the selected test body: evidence concerns the API call.
    return tuple(torch.ones((1,4,8),device=request.param) for _ in range(3))


def test_flex_attention_validation(inputs):
    with pytest.raises(NotImplementedError,match='must be 4D tensors'):
        flex_attention(*inputs)
