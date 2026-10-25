"""Small real PyTorch capture example; tensors are prepared before test capture."""
import torch

X=torch.ones(2)
Y=torch.ones(2)

def test_add_and_compile_exclusion():
    first=torch.add(X,Y)
    alias=torch.compile
    wrapped=alias(lambda x: torch.mul(x,2),disable=True)
    second=wrapped(X)
    last=torch.add(X,Y)
    assert first.shape==second.shape==last.shape
