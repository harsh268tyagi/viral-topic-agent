# Implementation Plan: Viral Topic Agent

## Overview

This plan converts the Viral Topic Agent design into incremental Python coding tasks. The build order is: project scaffolding and immutable domain models → the `ResilientDataSource` infrastructure (every external call funnels through it) → configuration persistence → the shared baseline computation → each analysis, generation, and delivery component as an isolated pure-logic unit → the `Automation_Scheduler` that wires everything into a scheduled run. Each component is implemented, then validated by Hypothesis property tests (one test per design property) and pytest example/edge-case tests before the next layer builds on it.

Language: Python 3.11+. Testing: pytest + Hypothesis (`@settings(max_examples=100)` minimum). Property test tasks are tagged with `# Feature: viral-topic-agent, Property {n}: {text}` and reference the design property they validate.

## Tasks

- [x] 1. Set up project structure and core domain models
  - [x] 1.1 Scaffold the Python project and testing framework
    - Create the `src/` package layout and `tests/` directory
    - Add `pyproject.toml` with Python 3.11+, `pytest`, and `hypothesis` dependencies
    - Configure pytest discovery and a `conftest.py` placeholder for shared fixtures
    - _Requirements: foundational (no specific acceptance criterion)_

  - [x] 1.2 Implement core enums and immutable domain data models
    - Define enums: `ChannelCategory`, `TimeWindow`, `Confidence`, `VideoFormat`, `DeliveryDestination`, `StepStatus`, `AuthStatus`
    - Define frozen dataclasses: `VideoStats`, `BaselineResult`, `ChannelProfile`, `ViralTemplate`, `ContentIdea`, `DiscoveryResult`, `FilterResult`, `ScoredIdea`, `CompetitorSpike`, `CompetitorReport`, `Outlier`, `OutlierResult`, `TitleConcept`, `ThumbnailConcept`, `ConceptSet`, `PublishRecommendation`, `ScriptBundle`, `KeywordMetric`, `KeywordGap`, `KeywordGapResult`, `FormatResult`, `DigestSection`, `DigestReport`, `DeliveryOutcome`, `Schedule`, `StepResult`, `RunSummary`, `AuthorizedChannel`, `AuthorizationGrant`, `AuthorizationResult`, `Configuration`
    - Use tuple collections so equality is field-by-field (supports round-trip integrity)
    - _Requirements: 2.3, 5.1, 13.1, 15.1, 15.4_

- [x] 2. Implement the resilient data source infrastructure
  - [x] 2.1 Define the Clock, Result type, DataSource protocol, and error hierarchy
    - Implement an injectable `Clock` abstraction (real + fake/test clock) for deterministic time control
    - Implement a `Result[T, E]` type for branching without exceptions
    - Define the `DataSource` Protocol (`get_channel_metadata`, `get_videos`, `get_audience_activity`, `get_keyword_metrics`, `get_template_performance`)
    - Define `DataSourceError` subtypes (`RateLimitError`, `TransientError`, `NonTransientError`, `TimeoutError`), `DataRequest`, and `DataSourceFailure` (carrying `target`, `reason`, `classification`)
    - _Requirements: 16.6_

  - [x] 2.2 Implement RetryPolicy and ResilientDataSource
    - Implement `RetryPolicy` defaults (max 3 attempts, ≥2s backoff, 30s request timeout, 60s default rate-limit pause, 300s max cumulative pause)
    - Implement `ResilientDataSource.call` to classify errors and return `Result[Any, DataSourceFailure]`
    - Rate-limit: pause for reported interval or default 60s; record `rate-limit-timeout` when cumulative pause exceeds 300s
    - Transient (incl. no complete response within 30s): retry up to 3 attempts with ≥2s spacing; record failure only after exhaustion
    - Non-transient: record once with target + reason, no retry; ensure independent calls continue
    - _Requirements: 16.1, 16.2, 16.3, 16.4, 16.5, 16.6, 16.7_

  - [x] 2.3 Write property test for rate-limit pause and cumulative timeout
    - **Property 30: Rate-limited requests pause for the correct interval and time out at the cumulative bound**
    - **Validates: Requirements 16.1, 16.5**

  - [x] 2.4 Write property test for transient retry bound and spacing
    - **Property 31: Transient failures are retried within bound with minimum spacing**
    - **Validates: Requirements 16.2, 16.4**

  - [x] 2.5 Write property test for non-transient single-attempt recording
    - **Property 32: Non-transient failures are recorded once with target and reason**
    - **Validates: Requirements 16.3, 16.6**

  - [x] 2.6 Write property test for independent-request isolation
    - **Property 33: A failed request does not block independent requests**
    - **Validates: Requirements 16.7**

  - [x] 2.7 Write unit tests for resilience edge cases
    - Verify a 30s no-response is classified as transient using the fake clock
    - _Requirements: 16.4_

- [x] 3. Implement configuration persistence
  - [x] 3.1 Implement configuration serialization and ConfigurationStore
    - Implement `serialize_config` / `deserialize_config` (explicit JSON encoders for all `Configuration` fields)
    - Implement `ConfigurationStore.save` (sets `saved` status on success; `configuration-save` error naming the failing setting on write failure, retaining the previous config)
    - Implement `ConfigurationStore.load` (`configuration-invalid` on corrupt data naming the failing setting without overwriting; `configuration-missing` when nothing persisted)
    - _Requirements: 15.1, 15.2, 15.3, 15.5, 15.6, 15.7_

  - [x] 3.2 Write property test for configuration round-trip
    - **Property 29: Configuration serialization round-trips losslessly**
    - **Validates: Requirements 15.1, 15.4**

  - [x] 3.3 Write unit tests for configuration store branches
    - Successful write sets `saved` (15.2); load deserializes persisted config (15.3); corrupt load → `configuration-invalid`, no overwrite (15.5); write failure → `configuration-save`, previous retained (15.6); empty store → `configuration-missing` (15.7)
    - _Requirements: 15.2, 15.3, 15.5, 15.6, 15.7_

- [x] 4. Implement baseline computation and channel analysis
  - [x] 4.1 Implement compute_baseline_view_count
    - Compute the median of the most-recent `min(N, len(videos))` view counts by publish date, with caller-supplied cap N
    - Set confidence: `UNAVAILABLE` for 0 videos, `LOW` for 1–4 videos, `NORMAL` for 5+
    - _Requirements: 2.2, 2.4, 2.7, 6.3, 7.1_

  - [x] 4.2 Write property test for baseline computation
    - **Property 1: Baseline view count is the median of the most-recent capped sample with correct confidence**
    - **Validates: Requirements 2.2, 2.4, 2.7, 6.3, 7.1**

  - [x] 4.3 Implement Channel_Analyzer
    - Retrieve metadata, video list, and per-video view counts through `ResilientDataSource` (2.1)
    - Build `ChannelProfile` with detected category, subscriber count, video count, and baseline (cap N=30) (2.3)
    - Record `partial_failure_reason` while still producing a profile (2.5); return `DataRetrievalError` with channel id + reason only when no data at all (2.6)
    - _Requirements: 2.1, 2.3, 2.5, 2.6_

  - [x] 4.4 Write property test for channel profile completeness
    - **Property 2: Channel profile is complete and consistent with retrieved data**
    - **Validates: Requirements 2.3**

  - [x] 4.5 Write unit tests for analyzer retrieval branches
    - Retrieval of metadata/list/counts (2.1); partial failure records reason (2.5); no data → `DataRetrievalError` (2.6)
    - _Requirements: 2.1, 2.5, 2.6_

- [x] 5. Implement channel authorization and connection
  - [x] 5.1 Implement Connection_Manager
    - Issue the authorization request within 5s of initiation (1.1); on grant store credentials in `Configuration` and mark `connected` (1.2)
    - On storage failure record `credential-storage-failed`, return not-saved error, leave channel not connected (1.8)
    - On denial record `authorization-failed` and never retrieve (1.3); no decision within 300s → `authorization-timeout`, no retrieval (1.7)
    - Retrieve with valid credentials within 30s via `ResilientDataSource` (1.4); expired credentials → `authorization-expired` identifying the channel (1.5)
    - Support up to 50 authorized channels, tagging each retrieved data set with its channel id (1.6); retries exhausted → `data-retrieval-failed` identifying channel, retaining credentials (1.9)
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 1.7, 1.8, 1.9_

  - [x] 5.2 Write property test for channel-id tagging and the 50-channel cap
    - **Property 3: Each owned channel's retrieved data is tagged with its own channel id, capped at 50**
    - **Validates: Requirements 1.6**

  - [x] 5.3 Write property test for failed-retrieval retry and credential retention
    - **Property 4: Failed owned-channel retrieval retries within bound, fails identifiably, and retains credentials**
    - **Validates: Requirements 1.9**

  - [x] 5.4 Write unit tests for authorization branches
    - Grant stores credentials and connects (1.2); denial → `authorization-failed`, no retrieval (1.3); expired → `authorization-expired` (1.5); storage failure → not connected (1.8); decision > 300s → `authorization-timeout` (1.7)
    - _Requirements: 1.2, 1.3, 1.5, 1.7, 1.8_

- [x] 6. Checkpoint - infrastructure and channel layer
  - Ensure all tests pass, ask the user if questions arise.

- [x] 7. Implement trend discovery and category filtering
  - [x] 7.1 Implement Trend_Discovery_Engine
    - For each requested `Time_Window` with data, produce 1–20 `Content_Idea`s; empty result for windows with no data or not requested (3.1, 3.4, 3.5, 3.6)
    - On window timeout/unavailability, return empty for that window plus an error indication identifying it, while other requested windows still produce results (3.7)
    - Associate each idea with 1–5 `Viral_Template`s and record its `Time_Window` plus a rationale referencing at least one observed metric value within that window (3.2, 3.3)
    - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7_

  - [x] 7.2 Write property test for discovery cardinality and per-window isolation
    - **Property 5: Discovery cardinality and per-window isolation**
    - **Validates: Requirements 3.1, 3.4, 3.5, 3.6, 3.7**

  - [x] 7.3 Write property test for idea template/window/rationale validity
    - **Property 6: Every content idea carries valid templates, window, and metric-backed rationale**
    - **Validates: Requirements 3.2, 3.3**

  - [x] 7.4 Implement Category_Filter
    - Selected category → only matching ideas + templates (4.1); no selection but detected category → apply detected (4.2)
    - No matching ideas but matching templates → return templates (4.4); no matches → empty + `no_matches` (4.5)
    - Unsupported selected category → `unsupported-category` error identifying it (4.6); no selection and none detected → `category_unavailable`, no filtering (4.7)
    - _Requirements: 4.1, 4.2, 4.4, 4.5, 4.6, 4.7_

  - [x] 7.5 Write property test for category filtering correctness
    - **Property 7: Category filtering returns only matching items or the correct indicator**
    - **Validates: Requirements 4.1, 4.2, 4.7**

  - [x] 7.6 Write unit and smoke tests for category filter edges
    - Ideas-empty-but-templates-present (4.4); no matches → `no_matches` (4.5); unsupported category → `unsupported-category` (4.6); supported categories are exactly gaming, music, entertainment, sports (4.3)
    - _Requirements: 4.3, 4.4, 4.5, 4.6_

- [x] 8. Implement idea scoring
  - [x] 8.1 Implement compute_idea_score and Idea_Scorer
    - Assign an integer `Idea_Score` in [0, 100] computed from the channel baseline and each associated template's observed performance (5.1, 5.2)
    - Order ideas by descending score, breaking ties by descending associated template observed performance (5.3)
    - Baseline unavailable/zero → use category aggregate and mark `low-confidence` (5.4); both unavailable → withhold score + `insufficient-data` identifying the idea (5.5)
    - _Requirements: 5.1, 5.2, 5.3, 5.4, 5.5_

  - [x] 8.2 Write property test for bounded, deterministic, monotonic scoring
    - **Property 8: Idea score is a bounded, deterministic, monotonic integer**
    - **Validates: Requirements 5.1, 5.2**

  - [x] 8.3 Write property test for scored-idea ordering and set preservation
    - **Property 9: Scored ideas are ordered by score then template performance and preserve the input set**
    - **Validates: Requirements 5.3**

  - [x] 8.4 Write property test for scoring degradation
    - **Property 10: Scoring degrades correctly when the baseline is missing**
    - **Validates: Requirements 5.4, 5.5**

- [x] 9. Implement competitor tracking
  - [x] 9.1 Implement Competitor_Tracker
    - `add_competitor` stores a not-yet-present id in `Configuration` (6.1); reject at 50 with a `limit-reached` indication naming the max (6.8)
    - `monitor` retrieves each competitor's trailing-30-day videos + view counts (6.2), computes that competitor's baseline (6.3), flags any video with view count > 0 and ratio ≥ 3.0 as a spike recording channel id, video id, view count, and spike factor (6.4, 6.5)
    - Competitor with < 5 retrieved videos → `insufficient-data`, skip spike detection, continue others (6.6); unavailable competitor → `unavailable`, continue others (6.7)
    - _Requirements: 6.1, 6.2, 6.3, 6.4, 6.5, 6.6, 6.7, 6.8_

  - [x] 9.2 Write property test for competitor add idempotency
    - **Property 11: Adding a competitor is order-insensitive and idempotent within the limit**
    - **Validates: Requirements 6.1**

  - [x] 9.3 Write property test for competitor spike classification
    - **Property 12: Competitor spike classification and recording**
    - **Validates: Requirements 6.4, 6.5**

  - [x] 9.4 Write property test for competitor monitoring isolation
    - **Property 13: Competitor monitoring isolates insufficient or unavailable channels**
    - **Validates: Requirements 6.6, 6.7**

  - [x] 9.5 Write unit test for the competitor cap
    - Adding at 50 monitored competitors → `limit-reached` (6.8)
    - _Requirements: 6.8_

- [x] 10. Implement outlier detection
  - [x] 10.1 Implement Outlier_Detector
    - Compute baseline from up to the 50 most-recent published videos (7.1)
    - When baseline > 0 and a video view count > 0 and ratio ≥ 5.0, classify as outlier with `outlier_factor = view_count / baseline`, recording video id, view count, factor (7.2, 7.3)
    - < 5 published videos → `insufficient-data`, no outliers (7.4); baseline zero/unavailable → `insufficient-data`, no outliers (7.5)
    - _Requirements: 7.1, 7.2, 7.3, 7.4, 7.5_

  - [x] 10.2 Write property test for outlier classification
    - **Property 14: Outlier classification and recording**
    - **Validates: Requirements 7.2, 7.3**

  - [x] 10.3 Write unit tests for outlier boundaries
    - < 5 videos → `insufficient-data` (7.4); baseline zero/unavailable → `insufficient-data` (7.5)
    - _Requirements: 7.4, 7.5_

- [x] 11. Checkpoint - analysis layer
  - Ensure all tests pass, ask the user if questions arise.

- [x] 12. Implement concept generation
  - [x] 12.1 Implement GenerationProvider interface and in-memory stub
    - Define the `GenerationProvider` interface used by concept and script generation
    - Provide a deterministic in-memory stub for tests
    - _Requirements: 8.1, 10.1_

  - [x] 12.2 Implement Concept_Generator
    - Produce ≥ 3 distinct title concepts each 1–100 chars (8.1, 8.3); ≥ 1 thumbnail concept with a visual description and a text overlay ≤ 30 chars (8.2)
    - Concepts match the idea's category when present (8.4); on inability to produce the required set, return no partial concepts and an error identifying the idea (8.5)
    - _Requirements: 8.1, 8.2, 8.3, 8.4, 8.5_

  - [x] 12.3 Write property test for concept set structural constraints
    - **Property 15: Generated concept sets satisfy all structural constraints**
    - **Validates: Requirements 8.1, 8.2, 8.3, 8.4**

  - [x] 12.4 Write unit test for concept generation failure
    - Cannot produce required set → no partial output + error identifying the idea (8.5)
    - _Requirements: 8.5_

- [x] 13. Implement SEO keyword-gap analysis
  - [x] 13.1 Implement classify_keyword_gaps and SEO_Analyzer
    - Retrieve up to 1,000 candidate keywords through `ResilientDataSource` (11.1)
    - With ≥ 4 analyzed keywords, classify a keyword as a gap when demand ≥ 50th percentile and competition ≤ 50th percentile (11.2)
    - Order gaps by descending demand, ties broken by ascending competition (11.3); no gaps → empty + `no-gap` (11.4); source error → no result, retain previous results, error indication (11.5); < 4 candidates → empty + `insufficient-data` (11.6)
    - _Requirements: 11.1, 11.2, 11.3, 11.4, 11.5, 11.6_

  - [x] 13.2 Write property test for keyword-gap percentile classification
    - **Property 20: Keyword-gap classification follows the percentile rule**
    - **Validates: Requirements 11.2**

  - [x] 13.3 Write property test for keyword-gap ordering
    - **Property 21: Keyword gaps are ordered by demand then competition**
    - **Validates: Requirements 11.3**

  - [x] 13.4 Write unit tests for SEO analyzer boundaries
    - No gaps → `no-gap` (11.4); source error retains previous results (11.5); < 4 candidates → `insufficient-data` (11.6)
    - _Requirements: 11.4, 11.5, 11.6_

- [x] 14. Implement script generation
  - [x] 14.1 Implement Script_Generator
    - Produce outline, script draft, SEO tags, and description (10.1)
    - SEO tags include every keyword supplied by `SEO_Analyzer` and total 5–30 (10.2); description 100–5000 chars (10.3)
    - No keywords supplied → produce outline/script/description and set `seo_tags_unavailable` (10.4); on failure → error identifying the failed item and retain the selected idea for retry (10.5)
    - _Requirements: 10.1, 10.2, 10.3, 10.4, 10.5_

  - [x] 14.2 Write property test for SEO tag superset and bounds
    - **Property 18: SEO tags include every analyzer keyword and stay within bounds**
    - **Validates: Requirements 10.2**

  - [x] 14.3 Write property test for description length bounds
    - **Property 19: Generated description length is within bounds**
    - **Validates: Requirements 10.3**

  - [x] 14.4 Write unit tests for script generation branches
    - Empty keywords → artifacts produced + `seo_tags_unavailable` (10.4); failed item → error + idea retained (10.5)
    - _Requirements: 10.4, 10.5_

- [x] 15. Implement format recommendation
  - [x] 15.1 Implement Format_Recommender

    - With ≥ 5 short-format and ≥ 5 long-format template videos, recommend exactly one format (12.1), choosing the higher observed average view count (12.2), defaulting to Short on a tie (12.3), with a rationale citing both averages (12.4)
    - Fewer than 5 in either format → withhold + `insufficient-performance-data` (12.5)
    - _Requirements: 12.1, 12.2, 12.3, 12.4, 12.5_

  - [x] 15.2 Write property test for format selection and tie-break
    - **Property 22: Format recommendation selects the higher average with the Short tie-break**
    - **Validates: Requirements 12.1, 12.2, 12.3, 12.4**

  - [x] 15.3 Write unit test for insufficient format data
    - One format with < 5 videos → withhold + `insufficient-performance-data` (12.5)
    - _Requirements: 12.5_

- [x] 16. Implement publish-time prediction
  - [x] 16.1 Implement Publish_Time_Predictor
    - Retrieve ≥ 7 days of audience activity through `ResilientDataSource` (9.1)
    - Recommend exactly one day-of-week and one contiguous window 1–3 hours long, in the Creator's time zone (9.2) or UTC if none configured (9.3)
    - Retrieval failure → retry up to 3 total attempts, else `audience-data-retrieval` error with channel id (9.4); owned-channel activity unavailable → derive from category aggregate, mark `low-confidence` (9.5); both unavailable → `no-data` error, no recommendation (9.6)
    - _Requirements: 9.1, 9.2, 9.3, 9.4, 9.5, 9.6_

  - [x] 16.2 Write property test for recommendation shape and time-zone selection
    - **Property 16: Publish-time recommendation shape and time-zone selection**
    - **Validates: Requirements 9.2, 9.3**

  - [x] 16.3 Write property test for low-confidence category fallback
    - **Property 17: Publish-time recommendation degrades to category aggregate with low confidence**
    - **Validates: Requirements 9.5**

  - [x] 16.4 Write unit tests for publish-time edges
    - Retrieval exhausts 3 attempts → `audience-data-retrieval` (9.4); both unavailable → `no-data` (9.6)
    - _Requirements: 9.4, 9.6_

- [x] 17. Checkpoint - generation layer
  - Ensure all tests pass, ask the user if questions arise.

- [x] 18. Implement digest compilation and delivery
  - [x] 18.1 Implement Deliverer interface and per-destination stubs
    - Define the `Deliverer` interface and in-memory stubs for email, Slack, and Notion supporting injectable success/failure
    - _Requirements: 13.5_

  - [x] 18.2 Implement Digest_Service
    - Compile scored ideas, competitor spikes, and outliers into one report with exactly three distinct typed sections; a zero-item section carries a `no-items` indicator (13.1, 13.2)
    - Deliver to each configured destination independently with per-destination retry up to 3 total attempts, recording `delivery-failed` per destination when all fail (13.3, 13.6, 13.7)
    - No destination configured → no delivery + `no-destination-configured` status (13.4)
    - _Requirements: 13.1, 13.2, 13.3, 13.4, 13.6, 13.7_

  - [x] 18.3 Write property test for report sections and no-items indicators
    - **Property 23: Digest report has three typed sections with correct no-items indicators**
    - **Validates: Requirements 13.1, 13.2**

  - [x] 18.4 Write property test for independent, retry-bounded delivery
    - **Property 24: Delivery is per-destination, independent, and retry-bounded**
    - **Validates: Requirements 13.3, 13.6, 13.7**

  - [x] 18.5 Write smoke/unit tests for delivery destinations
    - Supported destinations are exactly email, Slack, Notion (13.5); no destination configured → `no-destination-configured` (13.4)
    - _Requirements: 13.4, 13.5_

- [x] 19. Implement automation scheduling and end-to-end wiring
  - [x] 19.1 Implement Automation_Scheduler set_schedule and run wiring
    - `set_schedule` stores a valid schedule (interval + run time) in `Configuration` (14.1); reject a schedule missing interval or run time without storing, naming the missing field (14.2)
    - `run` loads `Configuration` and executes the steps in the mandated order: channel analysis, trend discovery, category filtering, idea scoring, competitor tracking, outlier detection, digest delivery (14.3)
    - Overlapping trigger while a run is in progress → not started concurrently, recorded as skipped (14.4); a failed step → record failure, skip only steps whose input depends on its output, continue independents (14.5)
    - Emit a `RunSummary` listing each step's status (succeeded/failed/skipped) with start and completion times (14.6); with no schedule configured, run only on manual trigger (14.7)
    - Wire `Channel_Analyzer`, `Trend_Discovery_Engine`, `Category_Filter`, `Idea_Scorer`, `Competitor_Tracker`, `Outlier_Detector`, and `Digest_Service` into the run pipeline
    - _Requirements: 14.1, 14.2, 14.3, 14.4, 14.5, 14.6, 14.7_

  - [x] 19.2 Write property test for invalid schedule rejection
    - **Property 25: Invalid schedules are rejected and name the missing field**
    - **Validates: Requirements 14.2**

  - [x] 19.3 Write property test for mandated step order
    - **Property 26: A run executes steps in the mandated order**
    - **Validates: Requirements 14.3**

  - [x] 19.4 Write property test for failed-step dependency skipping
    - **Property 27: A failed step skips only its dependents and continues independents**
    - **Validates: Requirements 14.5**

  - [x] 19.5 Write property test for run summary completeness
    - **Property 28: Run summary is complete and time-consistent**
    - **Validates: Requirements 14.6**

  - [x] 19.6 Write unit tests for scheduler branches
    - Valid schedule stored (14.1); overlap → recorded skipped (14.4); no schedule → manual-only (14.7)
    - _Requirements: 14.1, 14.4, 14.7_

- [x] 20. End-to-end integration and latency verification
  - [x] 20.1 Write integration tests for latency budgets and external wiring
    - Authorization request issued within 5s of initiation (1.1) and data retrieval within 30s with valid credentials (1.4); trend discovery responds within 10s (3.8); SEO retrieval of up to 1,000 keywords within 10s (11.1); script generation produces all artifacts within 60s (10.1) — each against stub provider/data source
    - _Requirements: 1.1, 1.4, 3.8, 10.1, 11.1_

  - [x] 20.2 Write end-to-end happy-path integration test
    - Exercise a single scheduled run through the full step order producing a delivered digest
    - _Requirements: 14.3_

- [x] 21. Final checkpoint - full suite
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional test sub-tasks and can be skipped for a faster MVP; core implementation tasks are never optional.
- Each task references specific granular acceptance criteria for traceability.
- Property tests (Properties 1–33) validate universal correctness properties using Hypothesis with a minimum of 100 examples; each property has exactly one property-based test placed close to the implementation it verifies.
- Unit, smoke, and integration tests cover concrete branches, supported-set checks, latency budgets, and external wiring that property testing is not suited to.
- All external `Data_Source` access goes through `ResilientDataSource` (Epic 2), so it is built before any component that retrieves data.
- Checkpoints ensure incremental validation at the end of the infrastructure/channel, analysis, generation, and full-system layers.

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1"] },
    { "id": 1, "tasks": ["1.2"] },
    { "id": 2, "tasks": ["2.1", "3.1", "4.1", "7.4", "8.1", "12.1", "15.1", "18.1"] },
    { "id": 3, "tasks": ["2.2", "3.2", "3.3", "4.2", "7.5", "7.6", "8.2", "8.3", "8.4", "10.1", "12.2", "14.1", "15.2", "15.3", "18.2"] },
    { "id": 4, "tasks": ["2.3", "2.4", "2.5", "2.6", "2.7", "4.3", "5.1", "7.1", "9.1", "10.2", "10.3", "13.1", "16.1", "12.3", "12.4", "14.2", "14.3", "14.4", "18.3", "18.4", "18.5"] },
    { "id": 5, "tasks": ["4.4", "4.5", "5.2", "5.3", "5.4", "7.2", "7.3", "9.2", "9.3", "9.4", "9.5", "13.2", "13.3", "13.4", "16.2", "16.3", "16.4", "19.1"] },
    { "id": 6, "tasks": ["19.2", "19.3", "19.4", "19.5", "19.6", "20.1", "20.2"] }
  ]
}
```
