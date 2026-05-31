"""Infrastructure layer.

Cross-cutting primitives and the external-dependency boundary: the injectable
``Clock``, the ``Result[T, E]`` branching type, the ``DataSource`` protocol and
its error model, and the ``ResilientDataSource`` decorator that centralizes
retry, rate-limit backoff, and timeout handling.
"""
