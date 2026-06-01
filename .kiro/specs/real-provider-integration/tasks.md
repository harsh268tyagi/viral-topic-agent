# Implementation Plan: Real Provider Integration

## Overview

This plan replaces the in-memory stubs (`DummyDataSource`, `InMemoryGenerationProvider`, and the in-memory deliverers) with real edge components that implement the existing `DataSource`, `GenerationProvider`, and `Deliverer` protocols unchanged. Implementation is in **Python 3.11+** to match the existing codebase, using `pytest` for example/integration tests and **Hypothesis** for the 18 correctness properties from the design.

Work proceeds bottom-up: first the injected ports (`HttpTransport`, `SmtpTransport`, `LLMClient`) and the secret-handling foundation, then layered configuration, then the YouTube data source (error mapping → auth → public data → analytics/keyword/template), then the LLM provider and deliverers, and finally the `CompositionRoot` that wires everything together and the dependency-policy/regression checks. Each component accepts injected transports/clients and a `Clock`, so every branch is exercised against fakes with no real network access.

All new third-party imports stay confined to the `infrastructure/`, `generation/`, and `delivery/` edges; `domain/` and `analysis/` gain no new imports.

## Tasks

- [x] 1. Establish injected ports and the secret-handling foundation
  - [x] 1.1 Implement the HttpTransport port and urllib-backed transport
    - Create `src/infrastructure/http_transport.py` defining `HttpResponse` (frozen dataclass with `status`, case-insensitive `headers`, `body`), the `HttpTransport` `Protocol` (single `request(...)` method), and the `HttpTransportError` / `HttpTimeoutError` signals
    - Implement `UrllibHttpTransport` over stdlib `urllib.request` performing exactly one request with no retry/backoff
    - Add a `FakeHttpTransport` test double (e.g. in `tests/edge_fakes.py`) that returns scripted `HttpResponse`s or raises `HttpTransportError` / `HttpTimeoutError`
    - _Requirements: 16.3, 16.5, 15.3_

  - [x] 1.2 Implement the SmtpTransport port
    - Create `src/delivery/smtp_transport.py` defining the `SmtpTransport` `Protocol` and a stdlib `smtplib`-backed implementation that sends one message and raises on failure
    - Add a `FakeSmtpTransport` test double that records sent messages or raises to drive the failure path
    - _Requirements: 16.3, 15.3_

  - [x] 1.3 Implement the LLMClient port
    - Create `src/generation/llm_client.py` defining the `LLMClient` `Protocol` (`complete(prompt, *, n, timeout_seconds) -> tuple[str, ...]`)
    - Provide a urllib/stdlib-backed `HttpLLMClient` over the `HttpTransport` port and a spy/fake `LLMClient` test double for tests
    - _Requirements: 16.3_

  - [x] 1.4 Implement Secret, CredentialReference, and the redaction helper
    - Create `src/config/secrets.py` with `CredentialReference` (non-secret name handle), `Secret` (value reachable only via explicit `reveal()`, with `__repr__`/`__str__` returning a redacted placeholder), and a module-level `redact(text, secrets)` helper
    - _Requirements: 12.1, 12.3_

  - [x] 1.5 Write unit tests for Secret redaction behavior
    - Assert `repr`/`str`/f-string interpolation never expose the value and `reveal()` returns it explicitly
    - _Requirements: 12.1, 12.3_

- [x] 2. Implement layered configuration loading and startup validation
  - [x] 2.1 Define Settings, component settings, and validation report models
    - Create `src/config/settings.py` with `AuthSettings`, `OAuthCredentials`, `EmailSettings`, `SlackSettings`, `NotionSettings`, `KeywordSourceSettings`, `TemplateStrategySettings`, the aggregate `Settings`, and `ConfigProblem` / `ValidationReport` (keys only, never values)
    - _Requirements: 11.5, 12.5_

  - [x] 2.2 Implement the ConfigurationSource protocol and the four sources
    - Create `src/config/sources.py` with the `ConfigurationSource` `Protocol` and `OverridesSource`, `EnvSource`, `DotEnvSource`, `ConfigFileSource`
    - Implement `.env` parsing: ignore comment lines (first non-whitespace `#`) and blank lines; split every other line at the first `=` trimming key and value; report a non-blank, non-comment line with no `=` as malformed by line number contributing no value
    - _Requirements: 10.4, 10.6_

  - [x] 2.3 Write property test for .env parsing
    - **Property 15: `.env` parsing follows the comment, blank-line, KEY=VALUE, and malformed-line rules**
    - **Validates: Requirements 10.4, 10.6**

  - [x] 2.4 Implement ConfigLoader precedence resolution
    - Create `src/config/config_loader.py` `ConfigLoader.load()` that consults sources in decreasing precedence (overrides → env → `.env` → config-file defaults), falling back to documented per-key defaults, and maps each value to its `Settings` field by documented key
    - _Requirements: 10.1, 10.2, 10.3, 10.5_

  - [x] 2.5 Write property test for configuration precedence
    - **Property 14: Configuration precedence selects the highest-precedence value or the defined default**
    - **Validates: Requirements 10.1, 10.2, 10.3**

  - [x] 2.6 Implement ConfigLoader.validate
    - Add `ConfigLoader.validate(settings)` that checks every required key for each enabled component and every selected delivery destination is present and well-formed (non-empty, correct type/format), treats unselected destinations as not required, and returns a `ValidationReport` listing all problems by key with secrets excluded
    - _Requirements: 11.1, 11.2, 11.3, 11.4, 11.5, 12.5_

  - [x] 2.7 Write property test for startup validation
    - **Property 16: Startup validation reports exactly the missing or malformed required keys and blocks all external requests**
    - **Validates: Requirements 11.1, 11.2, 11.3, 11.4, 12.5**

  - [x] 2.8 Write unit tests for field mapping and the validation happy path
    - Documented keys map to the correct `Settings` fields; a complete valid config produces validated `Settings` and allows the run
    - _Requirements: 10.5, 11.6_

- [x] 3. Checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 4. Implement YouTube error classification and authentication
  - [x] 4.1 Implement the YouTube error-mapping function
    - Create `src/infrastructure/youtube_error_mapping.py` with a pure `classify_response(...)` that evaluates failure signals in fixed precedence — HTTP 429 / 403-quota → `RateLimitError` (with `retry_after_seconds` from a numeric `Retry-After`, else unset); HTTP 400/401/404 / 403-auth → `NonTransientError` naming the request; HTTP ≥ 500 → `TransientError`; timeout → `TimeoutError`; connection error/reset → `TransientError`; any other error status → `NonTransientError` naming the request and status — building every reason through `redact(...)`
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 2.7, 2.8, 2.10, 2.11, 2.9_

  - [x] 4.2 Write property test for ordered error mapping
    - **Property 5: Every error response maps to exactly one error-hierarchy member by the documented precedence**
    - **Validates: Requirements 2.1, 2.2, 2.5, 2.6, 2.7, 2.8, 2.10, 2.11, 3.5, 4.4, 5.4**

  - [x] 4.3 Write property test for Retry-After handling
    - **Property 6: Rate-limit `retry_after_seconds` reflects a numeric Retry-After header exactly**
    - **Validates: Requirements 2.3, 2.4**

  - [x] 4.4 Implement AuthManager
    - Create `src/infrastructure/auth_manager.py` selecting the API key for Data API requests and OAuth bearer for Analytics requests; on an expired access token with a refresh token, obtain a new token and let the caller reissue exactly once; on refresh failure or expiry without a refresh token, raise `NonTransientError` naming the owned channel and indicating re-authorization; surface an invalid Data API key as `NonTransientError`; keep secrets out of all log entries; import the OAuth library lazily and only here
    - _Requirements: 13.1, 13.2, 13.3, 13.4, 13.5, 13.6, 13.7, 15.2_

  - [x] 4.5 Write unit tests for the authentication scenarios
    - Spy transport confirms API-key vs OAuth selection; scripted responses drive expired→refresh→reissue-once, refresh-failure, invalid-key, and expired-without-refresh
    - _Requirements: 13.1, 13.2, 13.3, 13.4, 13.5, 13.7_

- [x] 5. Implement YouTubeDataSource public data retrieval
  - [x] 5.1 Implement channel metadata retrieval and category mapping
    - Create `src/infrastructure/youtube_data_source.py` with the constructor (injected `HttpTransport`, `AuthManager`, `Clock`, base URL, timeout, optional providers, `max_items`) and `get_channel_metadata`, returning one `ChannelMetadata` with the requested id, retrieved title/subscriber/video counts, setting `detected_category` only when the retrieved topic maps to a supported `ChannelCategory`; route failures through `classify_response`
    - _Requirements: 1.2, 1.6, 1.7_

  - [x] 5.2 Write property test for channel metadata
    - **Property 1: Channel metadata mirrors the retrieved channel data**
    - **Validates: Requirements 1.2**

  - [x] 5.3 Write property test for detected category
    - **Property 2: Detected category is set if and only if the retrieved topic maps to a supported category**
    - **Validates: Requirements 1.6, 1.7**

  - [x] 5.4 Implement get_videos with recency filtering and pagination
    - Add `get_videos` returning one `VideoStats` (id, view count, ISO-8601 `published_at`) per retrieved video; when `published_within_days=N`, filter to videos within `[now − N days, now]` using the injected `Clock`; with no `N`, return all unfiltered; follow `nextPageToken` until no further page or the accumulated count reaches `max_items`
    - _Requirements: 1.3, 1.4, 1.5, 1.8, 16.4_

  - [x] 5.5 Write property test for video mapping and recency filtering
    - **Property 3: Video retrieval maps one-to-one and applies exact recency filtering**
    - **Validates: Requirements 1.3, 1.4, 1.5**

  - [x] 5.6 Write property test for pagination
    - **Property 4: Pagination stops at the first of no-further-page or the maximum item count**
    - **Validates: Requirements 1.8**

  - [x] 5.7 Write property test for no internal retry or backoff
    - **Property 7: The data source performs no internal retry or backoff**
    - **Validates: Requirements 16.5**

  - [x] 5.8 Write conformance test for the DataSource protocol
    - Assert `YouTubeDataSource` structurally satisfies the existing `DataSource` protocol without modifying it, with injected transport/clock
    - _Requirements: 1.1, 16.1, 16.3, 16.4_

- [x] 6. Implement audience activity, keyword metrics, and template performance
  - [x] 6.1 Implement get_audience_activity via the Analytics API
    - Add `get_audience_activity` to `youtube_data_source.py`: require OAuth via `AuthManager` (missing OAuth → `NonTransientError` indicating Analytics authorization required); build an `AudienceActivity` whose `channel_id` matches the request, buckets carry `day_of_week ∈ [0,6]`, `hour ∈ [0,23]`, non-negative activity, and `days_covered` bounded to `[0, days]`; empty-but-successful → zero-coverage activity; analytics auth/permission error → `NonTransientError` naming the channel; map other failures via `classify_response`
    - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6_

  - [x] 6.2 Write property test for audience activity
    - **Property 8: Audience activity has valid buckets and bounded coverage**
    - **Validates: Requirements 3.1, 3.2, 3.6**

  - [x] 6.3 Write unit tests for the audience-activity error branches
    - Missing OAuth and Analytics auth error each raise the documented `NonTransientError` and return no activity
    - _Requirements: 3.3, 3.4_

  - [x] 6.4 Implement keyword metrics and template performance seams
    - Create `src/infrastructure/keyword_metrics_provider.py` and `src/infrastructure/template_performance_strategy.py` defining the `Protocol`s; implement `get_keyword_metrics` (delegate to configured provider, cap at `max_keywords`, return `[]` when unconfigured) and `get_template_performance` (delegate to configured strategy over retrieved video stats, populate all `TemplatePerformance` fields, return `[]` when unconfigured) in `youtube_data_source.py`, mapping underlying failures via `classify_response`
    - _Requirements: 4.1, 4.2, 4.3, 4.4, 5.1, 5.2, 5.3, 5.4_

  - [x] 6.5 Write property test for keyword metrics
    - **Property 9: Keyword metrics map one-to-one within the requested maximum**
    - **Validates: Requirements 4.1, 4.2**

  - [x] 6.6 Write property test for template performance
    - **Property 10: Template performance maps one-to-one with all fields populated**
    - **Validates: Requirements 5.1, 5.2**

  - [x] 6.7 Write unit tests for the degradation branches
    - Unconfigured keyword provider and unconfigured template strategy each return `[]`
    - _Requirements: 4.3, 5.3_

- [x] 7. Checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 8. Implement the LLM generation provider
  - [x] 8.1 Implement LLMGenerationProvider over the LLMClient port
    - Create `src/generation/llm_generation_provider.py` implementing `generate_titles`, `generate_thumbnails`, `generate_outline`, `generate_script`, `generate_description`; request N candidates for titles/thumbnails and return one artifact per produced item (thumbnails carry a non-empty visual description and a text overlay); return raw artifacts without enforcing domain title-distinctness/length/overlay/description constraints; raise `GenerationError` (naming the item and `ContentIdea.idea_id`, no partial artifact, redacted reason) on failure/timeout/zero-items/empty-or-whitespace; for `count < 1` raise `GenerationError` without issuing a request
    - _Requirements: 6.2, 6.3, 6.4, 6.5, 6.6, 6.7, 6.8_

  - [x] 8.2 Write property test for raw-artifact pass-through
    - **Property 11: Generation returns the LLM's raw artifacts unmodified**
    - **Validates: Requirements 6.2, 6.3, 6.4, 6.5**

  - [x] 8.3 Write property test for generation failure conditions
    - **Property 12: Generation failure conditions raise without partial output or a wasted request**
    - **Validates: Requirements 6.6, 6.8**

  - [x] 8.4 Write conformance test for the GenerationProvider protocol
    - Assert `LLMGenerationProvider` structurally satisfies the existing `GenerationProvider` protocol without modifying it
    - _Requirements: 6.1, 16.1_

- [x] 9. Implement delivery rendering and the three deliverers
  - [x] 9.1 Implement the shared DigestRenderer
    - Create `src/delivery/digest_renderer.py` as a pure function from `DigestReport` to a rendered payload that always includes all three sections and the per-empty-section no-items indicator
    - _Requirements: 7.4, 8.4, 9.4_

  - [x] 9.2 Write property test for digest rendering
    - **Property 13: Digest rendering always produces three sections with correct no-items indicators**
    - **Validates: Requirements 7.2, 7.4, 8.2, 8.4, 9.2, 9.4**

  - [x] 9.3 Implement EmailDeliverer
    - Create `src/delivery/email_deliverer.py` implementing the single-method `Deliverer`; render-and-verify all three sections before transmitting via `SmtpTransport`; return `None` on success; raise `DeliveryError` with a redacted reason on transmission failure
    - _Requirements: 7.1, 7.2, 7.3, 7.5, 7.6_

  - [x] 9.4 Implement SlackDeliverer
    - Create `src/delivery/slack_deliverer.py` implementing `Deliverer` over `HttpTransport` (`chat.postMessage`); render-and-verify before posting; return `None` on success; raise `DeliveryError` with a redacted reason on failure; if a `DeliveryError` cannot be constructed/raised after a posting failure, log a delivery-failed entry identifying the destination and still surface a failure
    - _Requirements: 8.1, 8.2, 8.3, 8.5, 8.6, 8.7_

  - [x] 9.5 Implement NotionDeliverer
    - Create `src/delivery/notion_deliverer.py` implementing `Deliverer` over `HttpTransport` (`pages.create` with the API-version header); render-and-verify before recording; return `None` on success; raise `DeliveryError` with a redacted reason on failure
    - _Requirements: 9.1, 9.2, 9.3, 9.5, 9.6_

  - [x] 9.6 Write unit tests for delivery happy/failure paths
    - Succeeding fakes return `None`; failing fakes raise `DeliveryError`; the Slack error-construction-failure fallback logs and surfaces a failure
    - _Requirements: 7.3, 7.5, 8.3, 8.5, 8.6, 9.3, 9.5_

  - [x] 9.7 Write conformance tests for the Deliverer protocol
    - Assert each deliverer structurally satisfies the existing single-method `Deliverer` boundary without modifying it
    - _Requirements: 7.1, 8.1, 9.1, 16.1_

- [x] 10. Checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 11. Wire the composition root and finalize the dependency policy
  - [x] 11.1 Declare optional dependency extras in pyproject.toml
    - Add each required third-party client library (e.g. the OAuth `youtube` extra) under `[project.optional-dependencies]`, pinned to an exact version
    - _Requirements: 15.1_

  - [x] 11.2 Implement CompositionRoot and the entry point
    - Create `src/app/composition_root.py` that loads and validates `Settings` before constructing anything or issuing any request; on validation failure report all problem keys (secrets redacted) and never run the scheduler; otherwise construct `ResilientDataSource(YouTubeDataSource(...))`, the `LLMGenerationProvider`, and one `Deliverer` per selected destination; build a `Configuration` from `Settings`; run the `AutomationScheduler` on a complete `Schedule` or manual-only otherwise; abort and report the failing component if any required construction fails, including when reporting itself fails
    - _Requirements: 11.1, 11.2, 14.1, 14.2, 14.3, 14.4, 14.5, 14.6, 14.7_

  - [x] 11.3 Write property test for the built Configuration
    - **Property 18: The built Configuration faithfully reflects the Settings**
    - **Validates: Requirements 14.3**

  - [x] 11.4 Write unit tests for composition wiring
    - Root builds `ResilientDataSource(YouTubeDataSource)` and one deliverer per selected destination, runs the scheduler with the constructed components, honors schedule vs manual-only, and aborts reporting the component on construction failure including a nested reporting failure
    - _Requirements: 14.1, 14.2, 14.4, 14.5, 14.6, 14.7_

  - [x] 11.5 Write property test for secret redaction across all outputs
    - **Property 17: No secret value appears in any rendered output**
    - **Validates: Requirements 2.9, 6.7, 7.6, 8.7, 9.6, 11.5, 12.1, 12.2, 12.3, 13.6**

  - [x] 11.6 Write edge-isolation and extras-free core tests
    - Assert core modules import without the optional extras installed, and the core suite completes without importing any third-party client library
    - _Requirements: 15.4, 15.5_

  - [x] 11.7 Write dependency-policy and secret-hygiene smoke tests
    - Parse `pyproject.toml` to confirm each client library is an exactly-pinned optional extra; scan `domain/` and `analysis/` to confirm no third-party client imports; assert stdlib is used where it suffices; assert `.env` and local credential files are excluded from version control
    - _Requirements: 15.1, 15.2, 15.3, 12.4_

  - [x] 11.8 Run the existing-suite regression check
    - Run the full existing test suite and confirm all 33 existing correctness properties still pass after the feature is added
    - _Requirements: 16.2_

- [x] 12. Final checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional test sub-tasks and can be skipped for a faster MVP; core implementation tasks are never optional.
- Each task references specific granular requirements for traceability.
- Property tests use Hypothesis (min. 100 examples each), one test per property, tagged `# Feature: real-provider-integration, Property {number}: {property_text}`, and run against injected fakes (`FakeHttpTransport`, `FakeSmtpTransport`, spy `LLMClient`, `FakeClock`) with no real network access.
- Properties 5, 13, and 17 each consolidate many per-criterion requirements (error mapping, rendering, redaction); conformance, happy-path, OAuth, wiring, and dependency-policy checks are covered by example/integration/smoke tests.
- The `domain/` and `analysis/` layers gain no new imports; third-party client libraries stay confined to `infrastructure/`, `generation/`, and `delivery/`.

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1", "1.2", "1.3", "1.4", "9.1", "11.1"] },
    { "id": 1, "tasks": ["1.5", "2.1", "4.1", "8.1", "9.2"] },
    { "id": 2, "tasks": ["2.2", "4.2", "4.3", "4.4", "8.2", "8.3", "8.4", "9.3", "9.4", "9.5"] },
    { "id": 3, "tasks": ["2.3", "2.4", "4.5", "5.1", "9.6", "9.7"] },
    { "id": 4, "tasks": ["2.5", "2.6", "5.2", "5.3", "5.4"] },
    { "id": 5, "tasks": ["2.7", "2.8", "5.5", "5.6", "5.7", "5.8", "6.1"] },
    { "id": 6, "tasks": ["6.2", "6.3", "6.4"] },
    { "id": 7, "tasks": ["6.5", "6.6", "6.7", "11.2"] },
    { "id": 8, "tasks": ["11.3", "11.4", "11.5", "11.6", "11.7", "11.8"] }
  ]
}
```
