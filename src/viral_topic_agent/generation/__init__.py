"""Generation layer.

Produces creative assets (title/thumbnail concepts and full scripts) via the
``GenerationProvider`` interface, so the concrete backend (an LLM or hosted
service) can change without touching domain logic.

The provider interface and its in-memory stub are re-exported here so callers
can ``from viral_topic_agent.generation import GenerationProvider`` regardless
of the module split.
"""

from viral_topic_agent.generation.provider import (
    GENERATION_OPERATIONS,
    OP_DESCRIPTION,
    OP_OUTLINE,
    OP_SCRIPT,
    OP_THUMBNAILS,
    OP_TITLES,
    GenerationError,
    GenerationProvider,
    InMemoryGenerationProvider,
    ThumbnailDraft,
)
from viral_topic_agent.generation.concept_generator import (
    MAX_OVERLAY_CHARS,
    MAX_TITLE_CHARS,
    MIN_THUMBNAILS,
    MIN_TITLES,
    REASON_GENERATION_FAILED,
    REASON_INSUFFICIENT_TITLES,
    REASON_NO_THUMBNAIL,
    THUMBNAIL_REQUEST_COUNT,
    TITLE_REQUEST_COUNT,
    ConceptError,
    ConceptGenerator,
)
from viral_topic_agent.generation.script_generator import (
    MAX_DESCRIPTION_CHARS,
    MAX_SEO_TAGS,
    MIN_DESCRIPTION_CHARS,
    MIN_SEO_TAGS,
    ScriptError,
    ScriptGenerator,
)

__all__ = [
    "GENERATION_OPERATIONS",
    "OP_DESCRIPTION",
    "OP_OUTLINE",
    "OP_SCRIPT",
    "OP_THUMBNAILS",
    "OP_TITLES",
    "GenerationError",
    "GenerationProvider",
    "InMemoryGenerationProvider",
    "ThumbnailDraft",
    "MAX_OVERLAY_CHARS",
    "MAX_TITLE_CHARS",
    "MIN_THUMBNAILS",
    "MIN_TITLES",
    "REASON_GENERATION_FAILED",
    "REASON_INSUFFICIENT_TITLES",
    "REASON_NO_THUMBNAIL",
    "THUMBNAIL_REQUEST_COUNT",
    "TITLE_REQUEST_COUNT",
    "ConceptError",
    "ConceptGenerator",
    "MAX_DESCRIPTION_CHARS",
    "MAX_SEO_TAGS",
    "MIN_DESCRIPTION_CHARS",
    "MIN_SEO_TAGS",
    "ScriptError",
    "ScriptGenerator",
]
