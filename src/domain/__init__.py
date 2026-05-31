"""Domain layer.

The immutable core data models and enums shared by every other layer. These are
frozen dataclasses with field-by-field value equality, which underpins the
configuration round-trip integrity guarantee.

This package also carries the project version, since the layers are top-level
packages under ``src/`` with no single root package.
"""

__version__ = "0.1.0"
