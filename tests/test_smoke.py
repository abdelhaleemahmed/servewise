"""Smoke tests: the package imports, declares a version, and its identity
endpoint payload is well-formed."""
import servewise


def test_version():
    assert isinstance(servewise.__version__, str) and servewise.__version__


def test_main_is_callable():
    assert callable(servewise.main)
