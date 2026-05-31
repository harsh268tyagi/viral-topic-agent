"""Scaffold smoke tests.

Verifies the project package is importable and the testing framework
(pytest + Hypothesis) is wired up correctly. These are placeholders that the
real component tests will build on in later tasks.
"""

from hypothesis import given, settings
from hypothesis import strategies as st

import domain


def test_package_importable_and_versioned():
    """The package imports and exposes a version string."""
    assert isinstance(domain.__version__, str)
    assert domain.__version__


@settings(max_examples=100)
@given(st.integers())
def test_hypothesis_is_available(value):
    """Hypothesis is installed and can drive a property test."""
    assert value + 0 == value
