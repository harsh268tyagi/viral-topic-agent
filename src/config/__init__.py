"""Configuration and secret-handling layer.

The layered configuration model for the Real Provider Integration: the
``ConfigLoader`` that assembles validated ``Settings`` from precedence-ordered
``ConfigurationSource``s, and the secret-handling primitives (``Secret``,
``CredentialReference``, and the ``redact`` helper) that keep credential values
out of logs, error reasons, and run summaries.
"""
