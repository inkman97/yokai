"""Smoke test for the public API surface."""


def test_public_api_imports():
    import yokai

    assert hasattr(yokai, "__version__")
    assert hasattr(yokai, "load_config")
    assert hasattr(yokai, "Story")
    assert hasattr(yokai, "IssueTracker")
    assert hasattr(yokai, "RepoHosting")
    assert hasattr(yokai, "CodingAgent")
    assert hasattr(yokai, "SpecPipelineError")
    assert hasattr(yokai, "configure_logging")


def test_version_is_string():
    import yokai

    assert isinstance(yokai.__version__, str)
    assert len(yokai.__version__) > 0


def test_all_exports_are_resolvable():
    import yokai

    for name in yokai.__all__:
        assert hasattr(yokai, name), f"{name} listed in __all__ but not exported"
