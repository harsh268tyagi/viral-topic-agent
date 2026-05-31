"""Delivery layer.

Compiles analysis results into a digest report and delivers it to each
configured destination (email, Slack, Notion) independently with bounded retry.

The deliverer boundary, its stubs, and the digest service are re-exported here
for stable import paths.
"""

from viral_topic_agent.delivery.deliverer import (
    Deliverer,
    DeliveryError,
    EmailDeliverer,
    InMemoryDeliverer,
    NotionDeliverer,
    SlackDeliverer,
)
from viral_topic_agent.delivery.digest_service import (
    MAX_DELIVERY_ATTEMPTS,
    SECTION_COMPETITOR_SPIKES,
    SECTION_OUTLIERS,
    SECTION_SCORED_IDEAS,
    STATUS_DELIVERED,
    STATUS_DELIVERY_FAILED,
    STATUS_NO_DESTINATION_CONFIGURED,
    DeliveryResult,
    DigestService,
)

__all__ = [
    "Deliverer",
    "DeliveryError",
    "EmailDeliverer",
    "InMemoryDeliverer",
    "NotionDeliverer",
    "SlackDeliverer",
    "MAX_DELIVERY_ATTEMPTS",
    "SECTION_COMPETITOR_SPIKES",
    "SECTION_OUTLIERS",
    "SECTION_SCORED_IDEAS",
    "STATUS_DELIVERED",
    "STATUS_DELIVERY_FAILED",
    "STATUS_NO_DESTINATION_CONFIGURED",
    "DeliveryResult",
    "DigestService",
]
