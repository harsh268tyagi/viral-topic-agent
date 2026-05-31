"""Connection layer.

Owns the channel authorization and connection lifecycle: issuing the
authorization request, resolving the grant/deny/timeout decision, persisting
credentials, and retrieving data for connected channels (tagging each data set
with its originating channel id).
"""
