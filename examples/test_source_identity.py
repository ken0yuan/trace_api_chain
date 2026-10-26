import source_identity_backend as backend


def test_source_identity():
    # Labels can change without changing the lexical source identity.
    backend.Backend.__qualname__ = "RuntimeBackend"
    assert backend.api("cuda")
