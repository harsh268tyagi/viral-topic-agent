# Viral Topic Agent — Knowledge Base

This document is the engineering reference for the Viral Topic Agent: what the
system does, how it is structured, how data flows through it, how it handles
failure, and the correctness properties it is verified against. It complements
the formal spec under `.kiro/specs/viral-topic-agent/` (requirements, design,
tasks) and the inline docstrings in each module.

---

## 1. Overview

The Viral Topic Agent is an automated pipeline that helps a YouTube creator grow
a channel. It:

1. Connects and analyzes the creator's owned channel(s).
2. Discovers trending and historically viral content ideas and templates across
   weekly, monthly, and all-time windows.
3. Filters ideas by channel category and scores them by predicted view
   potential for the specific channel.
4. Tracks competitor channels and detects performance spikes.
5. Detects outlier videos that far exceed a channel's normal performance.
6. Generates production-ready assets: title/thumbnail concepts, scripts, SEO
   tags, descriptions, plus publish-time and format recommendations.
7. Compiles a digest and delivers it to email, Slack, and/or Notion.
8. Runs the whole flow on a recurring schedule.

The codebase is **Python 3.11+** with **no runtime third-party dependencies**.
Testing uses `pytest` and `hypothesis`.

---

## 2. Architecture

The system is organized into five layers. Domain logic depends only inward
(toward more abstract layers); infrastructure concerns sit at the edges.

```
┌─────────────────────────────────────────────────────────────┐
│ Orchestration                                                 │
│   AutomationScheduler                                         │
├─────────────────────────────────────────────────────────────┤
│ Analysis (pure transformations over retrieved data)          │
│   ChannelAnalyzer · TrendDiscoveryEngine · CategoryFilter    │
│   IdeaScorer · CompetitorTracker · OutlierDetector           │
│   SEOAnalyzer · FormatRecommender · PublishTimePredictor     │
├─────────────────────────────────────────────────────────────┤
│ Generation (creative assets via a provider interface)        │
│   ConceptGenerator · ScriptGenerator                         │
├─────────────────────────────────────────────────────────────┤
│ Delivery                                                      │
│   DigestService                                              │
├─────────────────────────────────────────────────────────────┤
│ Infrastructure (edges: network, persistence, generation)     │
│   ResilientDataSource → DataSource                           │
│   ConfigurationStore → Persistent Storage                    │
│   GenerationProvider (LLM / hosted service)                  │
│   Clock · Result                                             │
└─────────────────────────────────────────────────────────────┘
```

### Layer responsibilities

- **Orchestration** (`AutomationScheduler`) owns the schedule, prevents
  overlapping runs, runs steps in the mandated order, short-circuits dependent
  steps on failure, and builds a run summary.
- **Analysis** components are pure transformations: each takes already-fetched
  data plus parameters and returns a structured result. These are the primary
  targets for property-based testing.
- **Generation** produces creative assets via an external provider, wrapped
  behind an interface so tests can inject deterministic stubs.
- **Delivery** (`DigestService`) compiles results into a report and delivers to
  each destination independently with per-destination retry.
- **Infrastructure**:
  - `ResilientDataSource` decorates the raw `DataSource` and centralizes retry,
    rate-limit backoff, timeout handling, and transient/non-transient
    classification. **All** analysis components reach the data source only
    through this decorator.
  - `ConfigurationStore` serializes/deserializes `Configuration` with
    round-trip integrity.
  - `Clock` abstracts time so retry spacing, timeouts, and scheduling are
    deterministic and instant in tests.
  - `Result[T, E]` lets external calls branch on success/failure as data rather
    than raising exceptions.

### Package layout

Each architectural layer is a top-level Python package directly under `src/`
(there is no wrapping package):

| Layer | Package | Modules |
|-------|---------|---------|
| Domain | `domain/` | `models` (also carries `__version__`) |
| Infrastructure | `infrastructure/` | `clock`, `result`, `datasource`, `resilient_data_source` |
| Persistence | `persistence/` | `config_store` |
| Connection | `connection/` | `connection_manager` |
| Analysis | `analysis/` | `baseline`, `channel_analyzer`, `trend_discovery`, `category_filter`, `scoring`, `competitor_tracker`, `outlier_detector`, `seo_analyzer`, `publish_time_predictor`, `format_recommender` |
| Generation | `generation/` | `provider`, `concept_generator`, `script_generator` |
| Delivery | `delivery/` | `deliverer`, `digest_service` |
| Orchestration | `orchestration/` | `automation_scheduler` |

Imports use the layer package directly, e.g. `from domain.models import
Configuration`, `from infrastructure.clock import RealClock`. The `generation`
and `delivery` packages re-export their public symbols from `__init__.py`, so
callers can import either from the package (`from generation import
ScriptGenerator`) or from the specific module (`from generation.script_generator
import ScriptGenerator`). The project version is `domain.__version__`.

---

## 3. Design Principles

1. **Resilience first.** Every interaction with the external `DataSource` flows
   through `ResilientDataSource`. A failure of one request never silently
   corrupts unrelated results.
2. **Component isolation.** Each analytical component is a pure-logic unit.
   Side effects live at the edges. This makes the bulk of the system
   property-testable.
3. **Graceful degradation.** Partial data produces partial results with explicit
   status markers (`low-confidence`, `insufficient-data`, `unavailable`,
   `no-matches`, `no-gap`, etc.) rather than hard failures — except where a
   requirement mandates withholding output.
4. **Deterministic ordering and bounds.** Ranked outputs (scored ideas, keyword
   gaps) have a well-defined total ordering with explicit tie-breaks, and all
   counts/lengths obey documented bounds.
5. **Round-trip integrity.** Configuration serialization is lossless and
   field-by-field reversible.
6. **Status as data, not exceptions.** Degraded states are explicit fields on
   result objects, so downstream components and the digest can render them.

---

## 4. Core Infrastructure

### Clock (`infrastructure/clock.py`)

An injectable source of monotonic time plus a `sleep`.

- `RealClock` delegates to the system monotonic clock and real sleeping.
- `FakeClock` keeps a virtual "now" that only advances on `sleep`/`advance`, so
  tests drive retry counts, backoff spacing, and timeouts instantly and
  deterministically. Tracks `total_slept` for cumulative-pause assertions.

### Result (`infrastructure/result.py`)

A minimal `Result[T, E]` with two frozen variants, `Ok` and `Err`. API:
`is_ok`/`is_err`, `value`/`error`, `unwrap`/`unwrap_err`/`unwrap_or`,
`map`/`map_err`. Both variants are frozen (value equality, hashable).

### DataSource (`infrastructure/datasource.py`)

The single external dependency for YouTube data, defined as a `Protocol`:

```python
class DataSource(Protocol):
    def get_channel_metadata(self, channel_id) -> ChannelMetadata: ...
    def get_videos(self, channel_id, published_within_days=None) -> list[VideoStats]: ...
    def get_audience_activity(self, channel_id, days) -> AudienceActivity: ...
    def get_keyword_metrics(self, category, max_keywords) -> list[KeywordMetric]: ...
    def get_template_performance(self, category) -> list[TemplatePerformance]: ...
```

Supporting types:

- **Error hierarchy**: `DataSourceError` → `RateLimitError`, `TransientError`,
  `NonTransientError`, `TimeoutError` (a `TransientError` subtype).
- **`DataRequest`**: a reified, replayable description of a single call
  (operation + target + params), so the resilience layer can retry it and name
  its `target` in failures.
- **`DataSourceFailure`**: a recorded failure carrying `target`, `reason`,
  `classification`, and `attempts`.
- **`FailureClassification`**: `RATE_LIMITED`, `TRANSIENT`, `NON_TRANSIENT`,
  `RATE_LIMIT_TIMEOUT`.

### ResilientDataSource (`infrastructure/resilient_data_source.py`)

Decorates a `DataSource` and returns `Result[Any, DataSourceFailure]`.

`RetryPolicy` defaults:

| Setting                          | Default | Meaning                                  |
|----------------------------------|---------|------------------------------------------|
| `max_attempts`                   | 3       | total attempts including the first       |
| `min_backoff_seconds`            | 2.0     | minimum wait between retries             |
| `request_timeout_seconds`        | 30.0    | a slower response is treated as transient|
| `default_rate_limit_pause_seconds`| 60.0   | pause when the provider reports none     |
| `max_total_pause_seconds`        | 300.0   | cumulative rate-limit pause bound        |

Behavior:

- **Rate limit** — pause for the provider-reported interval (or 60 s default);
  if cumulative pause would exceed 300 s, record a `rate-limit-timeout` failure.
- **Transient** (network timeout, connection reset, temporary unavailability, or
  no complete response within 30 s) — retry up to `max_attempts` with ≥ 2 s
  spacing; record the failure only after the bound is exhausted.
- **Non-transient** (auth rejection, invalid request, target-not-found) —
  recorded immediately, no retry, carrying target + reason.
- **Independence** — each `call` is independent; a failed request never blocks
  unrelated ones.

### ConfigurationStore (`persistence/config_store.py`)

Explicit per-setting JSON encoders/decoders for every `Configuration` field, so
the lossless round-trip is fully under our control and a failure can name the
offending setting.

- `save` → status `saved` on success; on serialize/write failure returns
  `configuration-save` naming the failing setting and retains the previously
  persisted config unchanged.
- `load` → `Ok(Configuration)` on success; `configuration-invalid` (naming the
  failing setting, no overwrite) on corrupt data; `configuration-missing` when
  nothing is persisted.

Storage is pluggable via a `StorageBackend` protocol (`InMemoryStorageBackend`,
`FileStorageBackend` with atomic replace), so write failures and corruption are
injectable in tests.

---

## 5. Component Reference

| Component | Module | Responsibility | Requirements |
|-----------|--------|----------------|--------------|
| `ConnectionManager` | `connection/connection_manager.py` | Channel authorization lifecycle (request → decision → retrieval); credential storage; 50-channel cap; data tagging | 1 |
| `compute_baseline_view_count` | `analysis/baseline.py` | Median of most-recent capped sample with confidence marker (shared by analyzer / competitor / outlier) | 2, 6, 7 |
| `ChannelAnalyzer` | `analysis/channel_analyzer.py` | Builds a `ChannelProfile` (category, subs, video count, baseline) | 2 |
| `TrendDiscoveryEngine` | `analysis/trend_discovery.py` | 1–20 ideas per window, templates + metric-backed rationale; per-window isolation | 3 |
| `CategoryFilter` | `analysis/category_filter.py` | Restrict ideas/templates to a category; status indicators | 4 |
| `IdeaScorer` / `compute_idea_score` | `analysis/scoring.py` | Bounded 0–100 score; ranking; degradation to category aggregate | 5 |
| `CompetitorTracker` | `analysis/competitor_tracker.py` | Add competitors (cap 50); monitor; flag ≥ 3× spikes | 6 |
| `OutlierDetector` | `analysis/outlier_detector.py` | Flag own videos ≥ 5× baseline | 7 |
| `ConceptGenerator` | `generation/concept_generator.py` | ≥ 3 distinct titles (1–100 chars), ≥ 1 thumbnail (overlay ≤ 30 chars) | 8 |
| `PublishTimePredictor` | `analysis/publish_time_predictor.py` | Best day + 1–3 hour window; tz handling; degradation | 9 |
| `ScriptGenerator` | `generation/script_generator.py` | Outline, script, SEO tags (5–30, superset of analyzer keywords), description (100–5000 chars) | 10 |
| `SEOAnalyzer` / `classify_keyword_gaps` | `analysis/seo_analyzer.py` | Up to 1,000 keywords; percentile gap classification + ordering | 11 |
| `FormatRecommender` | `analysis/format_recommender.py` | Short vs long-form by higher average; Short tie-break | 12 |
| `DigestService` | `delivery/digest_service.py` | Compile 3-section report; per-destination delivery with retry | 13 |
| `Deliverer` (+ stubs) | `delivery/deliverer.py` | One-attempt delivery boundary; email/Slack/Notion stubs | 13 |
| `AutomationScheduler` | `orchestration/automation_scheduler.py` | Schedule storage; ordered run; overlap prevention; failure/skip; run summary | 14 |
| `GenerationProvider` (+ stub) | `generation/provider.py` | Creative-generation backend abstraction (titles, thumbnails, script artifacts) | 8, 10 |
| Domain models & enums | `domain/models.py` | Frozen dataclasses + enums shared by every layer | all |
| `Clock`, `Result`, `DataSource`, `ResilientDataSource` | `infrastructure/` | Time, branching, external boundary, resilience | 16 |
| `serialize_config` / `ConfigurationStore` | `persistence/config_store.py` | Lossless configuration round-trip + storage | 15 |

### Key thresholds and bounds

| Concept | Value |
|---------|-------|
| Owned-channel baseline sample cap | 30 most-recent videos |
| Outlier baseline sample cap | 50 most-recent videos |
| Baseline confidence | 0 videos → `UNAVAILABLE`; 1–4 → `LOW`; 5+ → `NORMAL` |
| Competitor spike threshold | view count > 0 **and** ratio ≥ 3.0 |
| Outlier threshold | view count > 0 **and** ratio ≥ 5.0 |
| Idea score range | integer 0–100 |
| Max owned channels | 50 |
| Max monitored competitors | 50 |
| Ideas per window | 1–20 |
| Templates per idea | 1–5 |
| Title concepts | ≥ 3 distinct, each 1–100 chars |
| Thumbnail overlay | ≤ 30 chars |
| SEO tags | 5–30 total, superset of analyzer keywords |
| Description length | 100–5000 chars |
| Publish window | 1–3 contiguous hours |
| Keyword candidates | up to 1,000 |
| Keyword-gap rule | demand ≥ median **and** competition ≤ median (both inclusive) |
| Delivery attempts per destination | up to 3 total |
| Supported categories | gaming, music, entertainment, sports |
| Supported destinations | email, Slack, Notion |

---

## 6. Data Flow — Scheduled Run

`AutomationScheduler.run` executes seven steps in the mandated order:

```
channel_analysis → trend_discovery → category_filter → idea_scoring
  → competitor_tracking → outlier_detection → digest_delivery
```

### Step dependency graph

The skip-on-failure rule is driven by an explicit DAG mapping each step to the
steps whose output is its direct input:

| Step | Directly depends on |
|------|---------------------|
| `channel_analysis` | (independent) |
| `trend_discovery` | (independent) |
| `category_filter` | `trend_discovery` |
| `idea_scoring` | `category_filter`, `channel_analysis` |
| `competitor_tracking` | (independent) |
| `outlier_detection` | (independent) |
| `digest_delivery` | `idea_scoring`, `competitor_tracking`, `outlier_detection` |

`STEP_ORDER` is a topological order of this DAG, so a single forward pass both
runs independent steps and skips the transitive dependents of any failed or
skipped step.

### Run semantics

- **Overlap** — a trigger arriving while a run is in progress is recorded as
  skipped (every step `SKIPPED`, `overlap_skipped=True`) and does not run
  concurrently.
- **Failure** — a failed step is recorded `FAILED`; its transitive dependents
  are `SKIPPED`; independent steps still run.
- **Manual vs scheduled** — with no schedule configured, the run executes only
  on a manual trigger.
- **Run summary** — every step is listed with `SUCCEEDED` / `FAILED` /
  `SKIPPED`, plus start and completion timestamps (`start ≤ completion`).

---

## 7. Error Handling Model

Failures fall into a small number of explicit shapes:

- **Data-source failures** are classified and surfaced as `DataSourceFailure`
  inside an `Err` (rate-limit-timeout, transient-exhausted, non-transient). They
  never raise out of `ResilientDataSource`.
- **Degraded results** are returned as ordinary result objects with status
  flags rather than errors:
  - `Confidence.LOW` / `Confidence.UNAVAILABLE` on baselines and scores.
  - `insufficient_data`, `no_matches`, `category_unavailable`, `no_gap`,
    `seo_tags_unavailable`, `insufficient_performance_data`,
    `no_destination_configured`.
- **Withheld output** is used where a requirement mandates it (e.g. idea scoring
  with no usable basis withholds the score and flags `insufficient-data`
  identifying the idea; concept/script generation returns no partial output and
  an error naming the failed item/idea).
- **Identifiable errors** always name the affected entity — channel id, idea id,
  competitor id, failing setting, or destination — so a digest or run summary
  can render the failure without re-deriving it.

---

## 8. Correctness Properties

The system is verified against 33 universal correctness properties using
property-based testing (Hypothesis, ≥ 100 examples each; one property-based test
per property). Each test is tagged
`# Feature: viral-topic-agent, Property N: <text>`.

| # | Property | Validates |
|---|----------|-----------|
| 1 | Baseline is the median of the most-recent capped sample with correct confidence | 2.2, 2.4, 2.7, 6.3, 7.1 |
| 2 | Channel profile is complete and consistent with retrieved data | 2.3 |
| 3 | Each owned channel's retrieved data is tagged with its own id, capped at 50 | 1.6 |
| 4 | Failed owned-channel retrieval retries within bound, fails identifiably, retains credentials | 1.9 |
| 5 | Discovery cardinality and per-window isolation | 3.1, 3.4, 3.5, 3.6, 3.7 |
| 6 | Every content idea carries valid templates, window, and metric-backed rationale | 3.2, 3.3 |
| 7 | Category filtering returns only matching items or the correct indicator | 4.1, 4.2, 4.7 |
| 8 | Idea score is a bounded, deterministic, monotonic integer | 5.1, 5.2 |
| 9 | Scored ideas are ordered by score then template performance and preserve the input set | 5.3 |
| 10 | Scoring degrades correctly when the baseline is missing | 5.4, 5.5 |
| 11 | Adding a competitor is order-insensitive and idempotent within the limit | 6.1 |
| 12 | Competitor spike classification and recording | 6.4, 6.5 |
| 13 | Competitor monitoring isolates insufficient or unavailable channels | 6.6, 6.7 |
| 14 | Outlier classification and recording | 7.2, 7.3 |
| 15 | Generated concept sets satisfy all structural constraints | 8.1, 8.2, 8.3, 8.4 |
| 16 | Publish-time recommendation shape and time-zone selection | 9.2, 9.3 |
| 17 | Publish-time recommendation degrades to category aggregate with low confidence | 9.5 |
| 18 | SEO tags include every analyzer keyword and stay within bounds | 10.2 |
| 19 | Generated description length is within bounds | 10.3 |
| 20 | Keyword-gap classification follows the percentile rule | 11.2 |
| 21 | Keyword gaps are ordered by demand then competition | 11.3 |
| 22 | Format recommendation selects the higher average with the Short tie-break | 12.1–12.4 |
| 23 | Digest report has three typed sections with correct no-items indicators | 13.1, 13.2 |
| 24 | Delivery is per-destination, independent, and retry-bounded | 13.3, 13.6, 13.7 |
| 25 | Invalid schedules are rejected and name the missing field | 14.2 |
| 26 | A run executes steps in the mandated order | 14.3 |
| 27 | A failed step skips only its dependents and continues independents | 14.5 |
| 28 | Run summary is complete and time-consistent | 14.6 |
| 29 | Configuration serialization round-trips losslessly | 15.1, 15.4 |
| 30 | Rate-limited requests pause for the correct interval and time out at the cumulative bound | 16.1, 16.5 |
| 31 | Transient failures are retried within bound with minimum spacing | 16.2, 16.4 |
| 32 | Non-transient failures are recorded once with target and reason | 16.3, 16.6 |
| 33 | A failed request does not block independent requests | 16.7 |

Beyond these, unit/example tests cover concrete branches, supported-set checks,
and error paths; integration tests verify latency budgets (auth ≤ 5 s, retrieval
≤ 30 s, trend discovery ≤ 10 s, SEO ≤ 10 s, script generation ≤ 60 s) and the
end-to-end scheduled run.

---

## 9. Testing Strategy

- **Property-based (Hypothesis)** — universal invariants over generated inputs,
  one test per property, each comparing observed behavior against an independent
  reference model.
- **Unit / example** — concrete branches, boundaries, and supported-set checks
  that property testing is not suited to.
- **Integration** — latency budgets and the full scheduled run against fast
  stubs.

All time-dependent behavior is driven by an injected `Clock` (`FakeClock` /
`TickingClock` in tests), so retries, backoff, timeouts, and timestamps are
exact and instant.

Run everything:

```bash
pytest
```

---

## 10. Extending the System

- **Swap the data provider** — implement the `DataSource` protocol (e.g. a real
  YouTube Data API client) and wrap it in `ResilientDataSource`. No domain code
  changes.
- **Swap the generation backend** — implement `GenerationProvider` (e.g. an LLM
  client). `ConceptGenerator` and `ScriptGenerator` consume it unchanged.
- **Add a delivery destination** — extend `DeliveryDestination`, add a
  `Deliverer` implementation, and update `DigestService.SUPPORTED`. (Note the
  supported-set tests pin the current three destinations, so they update too.)
- **Change persistence** — implement a `StorageBackend` for `ConfigurationStore`
  (e.g. a database-backed store).

Keep new analysis logic pure (consume retrieved data, return a result object
with explicit status markers) so it remains property-testable.

---

## 11. References

- `.kiro/specs/viral-topic-agent/requirements.md` — functional & quality
  requirements (EARS format).
- `.kiro/specs/viral-topic-agent/design.md` — architecture, interfaces, data
  models, correctness properties, error handling.
- `.kiro/specs/viral-topic-agent/tasks.md` — the implementation plan.
- Module docstrings — each `src/viral_topic_agent/*.py` file documents its
  behavior and requirement traceability inline.
