"""Scaffold smoke tests.

Verifies the project package is importable and the testing framework
(pytest + Hypothesis) is wired up correctly. These are placeholders that the
real component tests will build on in later tasks.
"""

from hypothesis import given, settings
from hypothesis import strategies as st

import viral_topic_agent


def test_package_importable_and_versioned():
    """The package imports and exposes a version string."""
    assert isinstance(viral_topic_agent.__version__, str)
    assert viral_topic_agent.__version__


@settings(max_examples=100)
@given(st.integers())
def test_hypothesis_is_available(value):
    """Hypothesis is installed and can drive a property test."""
    assert value + 0 == value
