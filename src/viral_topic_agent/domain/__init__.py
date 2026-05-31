"""Domain layer.

The immutable core data models and enums shared by every other layer. These are
frozen dataclasses with field-by-field value equality, which underpins the
configuration round-trip integrity guarantee.
"""
