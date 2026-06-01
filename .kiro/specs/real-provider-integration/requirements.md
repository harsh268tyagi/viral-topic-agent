# Requirements Document

## Introduction

The Viral Topic Agent currently runs end to end against deterministic, in-memory dummy providers (a `DummyDataSource`, the `InMemoryGenerationProvider`, and the in-memory `EmailDeliverer`/`SlackDeliverer`/`NotionDeliverer` stubs). This feature, **Real Provider Integration**, replaces those stubs with concrete implementations that work against a real YouTube channel, a real large language model, and real delivery destinations (email, Slack, Notion), so the Creator can run the existing pipeline against live data with no changes to the domain, analysis, generation-consumer, or orchestration layers.

The integration is deliberately confined to the existing external-boundary seams. The concrete `YouTube_Data_Source` implements the existing `Data_Source` protocol exactly and raises the existing error hierarchy (`RateLimitError` with `retry_after_seconds`, `TransientError`, `NonTransientError`, `TimeoutError`) so the existing `Resilient_Data_Source` decorator keeps working unchanged. The concrete `LLM_Generation_Provider` implements the existing `Generation_Provider` protocol and raises `Generation_Error`, returning raw artifacts while domain validation stays in the `Concept_Generator` and `Script_Generator` consumers. The concrete `Email_Deliverer`, `Slack_Deliverer`, and `Notion_Deliverer` implement the existing single-method `Deliverer` boundary (`deliver(report) -> None`, raising `Delivery_Error` on failure).

Several data points the agent consumes are **not** directly available from the public YouTube Data API v3:

- **Audience activity** (watch-time / traffic heatmaps used for publish-time prediction) requires the YouTube Analytics API together with OAuth for the owned channel, not just an API key.
- **Keyword demand and competition** are not exposed by the YouTube Data API and must come from a configurable keyword-metrics source or be omitted.
- A ready-made **"viral template performance"** feed does not exist and must be derived from retrieved video statistics through a configurable strategy or be omitted.

This document specifies, for each of those gaps, how the data is sourced, approximated, or configured, and how the integration degrades gracefully (returning empty/partial data or raising the appropriate classified error) so the existing graceful-degradation markers (`low-confidence`, `insufficient-data`, `unavailable`) continue to apply.

This document also specifies configuration and secret handling. The Creator asked specifically about environment variables and asked for a better suggestion if one exists. The recommendation captured here is a **layered configuration model** rather than environment variables alone: a `Config_Loader` assembles validated `Settings` from a precedence-ordered set of `Configuration_Sources` (explicit overrides, then process environment variables, then a local `.env` file, then configuration-file defaults). This keeps the convenient env-var/`.env` flow for local development while allowing real environment variables or an external secret provider in production, validating all required values at startup, and keeping every `Secret` out of logs and run summaries.

A dependency-policy decision is also captured. The runtime core currently has no third-party dependencies, and adding real YouTube, LLM, Slack, and Notion clients introduces them. This document decides to **allow a minimal, exactly-pinned set of third-party client libraries declared as optional dependency extras and confined to the edge layers**, while preferring the Python standard library (for example `smtplib` and `urllib`) wherever it provides the capability. The tradeoff: optional third-party extras (such as `google-auth`/`google-api-python-client` for OAuth) reduce edge-code complexity and risk for tricky flows, at the cost of additional supply-chain surface; isolating them to the edges and keeping them optional preserves the dependency-free core and lets the existing 33 property-based tests run without installing them.

Requirements describe what the integration does, not how it is implemented; concrete client choices, request shapes, and module layout are deferred to the design phase, except where this document records an explicit decision the Creator requested.

## Glossary

- **Viral_Topic_Agent**: The existing system whose pipeline this feature connects to real external services.
- **Real_Provider_Integration**: The feature specified in this document: the edge-layer components and configuration that connect the Viral_Topic_Agent to real external services.
- **Creator**: The human user who owns the YouTube channel and operates the Viral_Topic_Agent.
- **Owned_Channel**: A YouTube channel the Creator owns and has authorized the Viral_Topic_Agent to analyze.
- **Competitor_Channel**: A YouTube channel, other than an Owned_Channel, that the Creator designates for monitoring.
- **Channel_Category**: A supported content classification: gaming, music, entertainment, or sports.
- **Data_Source**: The existing protocol (`get_channel_metadata`, `get_videos`, `get_audience_activity`, `get_keyword_metrics`, `get_template_performance`) that every analysis component depends on through the Resilient_Data_Source.
- **Resilient_Data_Source**: The existing decorator that classifies Data_Source errors and applies retry, rate-limit backoff, and timeout handling; the single point through which Data_Source calls flow.
- **Data_Source_Error_Hierarchy**: The existing exceptions a Data_Source may raise: `RateLimitError` (carrying `retry_after_seconds`), `TransientError`, `NonTransientError`, and `TimeoutError` (a `TransientError` subtype).
- **YouTube_Data_API**: The public YouTube Data API v3, accessed with an API key, providing public channel and video data.
- **YouTube_Analytics_API**: The YouTube Analytics API, accessed with OAuth for an Owned_Channel, providing owned-channel audience activity.
- **YouTube_Data_Source**: The concrete Data_Source implementation backed by the YouTube_Data_API and the YouTube_Analytics_API.
- **Keyword_Metrics_Provider**: A configurable source of search demand and competition values for candidate keywords, used to satisfy `get_keyword_metrics`.
- **Template_Performance_Strategy**: A configurable strategy that derives viral template performance values from retrieved video statistics, used to satisfy `get_template_performance`.
- **Generation_Provider**: The existing protocol (`generate_titles`, `generate_thumbnails`, `generate_outline`, `generate_script`, `generate_description`) consumed by the Concept_Generator and Script_Generator.
- **LLM_Service**: The external large language model service used to produce creative artifacts.
- **LLM_Generation_Provider**: The concrete Generation_Provider implementation backed by the LLM_Service.
- **Generation_Error**: The existing exception a Generation_Provider raises, carrying the failed item name and the affected Content_Idea identifier.
- **Content_Idea**: A discovered candidate topic for a new video, as defined by the existing domain model.
- **Deliverer**: The existing single-method boundary (`deliver(report) -> None`, raising `Delivery_Error` on failure) for pushing a digest to one destination.
- **Delivery_Destination**: A configured output target for the digest, one of email, Slack, or Notion.
- **Email_Deliverer**: The concrete Deliverer that transmits a Digest_Report by email.
- **Slack_Deliverer**: The concrete Deliverer that posts a Digest_Report to Slack.
- **Notion_Deliverer**: The concrete Deliverer that records a Digest_Report in a Notion database.
- **Delivery_Error**: The existing exception a Deliverer raises when one delivery attempt fails.
- **Digest_Report**: The existing compiled report with exactly three typed sections (scored ideas, competitor spikes, outliers).
- **Automation_Scheduler**: The existing orchestration component that runs the pipeline on a Schedule, prevents overlapping runs, and records a per-step run summary.
- **Schedule**: The existing recurrence configuration requiring both a recurrence interval and a run time to be valid.
- **Configuration**: The existing persisted settings model (authorized channels, selected Channel_Category, monitored Competitor_Channels, Schedule, Delivery_Destinations) consumed by the Automation_Scheduler.
- **Settings**: The validated, in-memory set of configuration values and Credential_References assembled by the Config_Loader for the Real_Provider_Integration.
- **Config_Loader**: The component that assembles Settings from the Configuration_Sources and validates them.
- **Configuration_Source**: One source of configuration values: explicit overrides, process environment variables, a `.env` file, or a configuration file.
- **Secret**: A credential value, including a YouTube Data API key, an OAuth client secret, an OAuth refresh token, an OAuth access token, an SMTP password, a Slack token, and a Notion token.
- **Credential_Reference**: A non-secret name or handle that identifies a Secret and is safe to record in logs and summaries.
- **OAuth_Credentials**: The set of values authorizing owned-channel access: client identifier, client secret, refresh token, and access token.
- **Auth_Manager**: The component that selects and applies API-key or OAuth authentication and refreshes expired OAuth access tokens.
- **Composition_Root**: The factory that reads Settings, constructs the real Resilient_Data_Source, LLM_Generation_Provider, and Deliverers, builds a Configuration, and runs the Automation_Scheduler.

## Requirements

### Requirement 1: YouTube Data Source — Protocol Conformance and Public Data Retrieval

**User Story:** As a Creator, I want a real data source backed by the YouTube Data API that implements the existing Data_Source contract, so that the pipeline retrieves my channel's live public data without any change to the analysis components.

#### Acceptance Criteria

1. THE YouTube_Data_Source SHALL implement the Data_Source protocol methods get_channel_metadata, get_videos, get_audience_activity, get_keyword_metrics, and get_template_performance with the parameter signatures and return types of the existing Data_Source protocol.
2. WHEN get_channel_metadata is called with a channel identifier, THE YouTube_Data_Source SHALL retrieve the channel title, subscriber count, and video count from the YouTube_Data_API and SHALL return exactly one ChannelMetadata value populated with the requested channel identifier, the retrieved title, the retrieved subscriber count, and the retrieved video count.
3. WHEN get_videos is called with a channel identifier, THE YouTube_Data_Source SHALL retrieve the channel's published videos and per-video view counts from the YouTube_Data_API and SHALL return one VideoStats value per retrieved video, each populated with the video identifier, the view count, and the ISO-8601 published timestamp.
4. WHEN get_videos is called with a published_within_days value of N, THE YouTube_Data_Source SHALL return only the VideoStats whose published timestamp is at or after the instant N days before the request time and at or before the request time.
5. WHEN get_videos is called with no published_within_days value, THE YouTube_Data_Source SHALL return one VideoStats value per retrieved video without filtering by published timestamp.
6. WHEN get_channel_metadata retrieves a channel topic or category that maps to a supported Channel_Category, THE YouTube_Data_Source SHALL set the detected Channel_Category on the returned ChannelMetadata to that supported Channel_Category.
7. IF get_channel_metadata retrieves a channel topic or category that does not map to a supported Channel_Category, THEN THE YouTube_Data_Source SHALL leave the detected Channel_Category unset on the returned ChannelMetadata.
8. WHEN the YouTube_Data_API returns a retrieval result across multiple pages, THE YouTube_Data_Source SHALL request successive pages until either the YouTube_Data_API reports no further page or the count of retrieved items reaches the requested maximum item count, whichever occurs first.

### Requirement 2: YouTube Data Source — Error Classification and Mapping

**User Story:** As a Creator, I want the real data source to raise the existing classified errors, so that the Resilient_Data_Source applies the same retry, backoff, and timeout behavior it does today.

#### Acceptance Criteria

1. WHEN the YouTube_Data_API responds to a request with HTTP status 429, THE YouTube_Data_Source SHALL raise a RateLimitError.
2. WHEN the YouTube_Data_API responds to a request with HTTP status 403 whose reported reason indicates a quota or rate limit, THE YouTube_Data_Source SHALL raise a RateLimitError.
3. WHEN the YouTube_Data_Source raises a RateLimitError AND the YouTube_Data_API response includes a Retry-After header carrying a numeric seconds value, THE YouTube_Data_Source SHALL set retry_after_seconds on the RateLimitError to that numeric value.
4. WHEN the YouTube_Data_Source raises a RateLimitError AND the YouTube_Data_API response includes no Retry-After header OR includes a Retry-After header that does not carry a numeric seconds value, THE YouTube_Data_Source SHALL leave retry_after_seconds unset on the RateLimitError.
5. IF a request to the YouTube_Data_API does not return a complete response within the configured request timeout, THEN THE YouTube_Data_Source SHALL raise a TimeoutError.
6. IF a request to the YouTube_Data_API fails with a network connection error or a connection reset, THEN THE YouTube_Data_Source SHALL raise a TransientError.
7. WHEN the YouTube_Data_API responds to a request with an HTTP status of 500 or greater, THE YouTube_Data_Source SHALL raise a TransientError.
8. WHEN the YouTube_Data_API responds to a request with HTTP status 400, 401, or 404, or with a 403 whose reported reason indicates an authorization or permission failure, THE YouTube_Data_Source SHALL raise a NonTransientError whose reason identifies the failing request.
9. WHEN the YouTube_Data_Source raises any member of the Data_Source_Error_Hierarchy, THE YouTube_Data_Source SHALL set the error reason to a description that excludes every Secret value.
10. WHEN the YouTube_Data_API returns an error response, THE YouTube_Data_Source SHALL determine the raised error by evaluating the following conditions in order and applying the first that matches — the rate-limit conditions of criteria 1 and 2, then the authorization condition of criterion 8, then the server-error condition of criterion 7, then the unmatched-error-status condition of criterion 11 — so that the response maps to exactly one member of the Data_Source_Error_Hierarchy.
11. WHEN the YouTube_Data_API responds to a request with an HTTP error status that satisfies none of the conditions in criteria 1, 2, 7, and 8, THE YouTube_Data_Source SHALL raise a NonTransientError whose reason identifies the failing request and the received HTTP status.

### Requirement 3: Owned-Channel Audience Activity Sourcing

**User Story:** As a Creator, I want audience activity sourced from the YouTube Analytics API when I have authorized it, so that publish-time prediction uses my real watch-time data and degrades gracefully when analytics access is unavailable.

#### Acceptance Criteria

1. WHERE OAuth_Credentials authorizing the YouTube_Analytics_API for the Owned_Channel are configured, WHEN get_audience_activity is called for the Owned_Channel, THE YouTube_Data_Source SHALL retrieve audience activity from the YouTube_Analytics_API and SHALL return an AudienceActivity value whose channel identifier equals the requested Owned_Channel identifier, whose days_covered reflects the retrieved data, and whose hourly activity buckets each carry a day-of-week in the range 0 to 6, an hour in the range 0 to 23, and a non-negative activity value.
2. WHEN get_audience_activity returns an AudienceActivity value, THE YouTube_Data_Source SHALL set the days_covered field to the number of days spanned by the retrieved audience activity, bounded between 0 and the requested number of days.
3. IF get_audience_activity is called AND no OAuth_Credentials authorizing the YouTube_Analytics_API for the Owned_Channel are configured, THEN THE YouTube_Data_Source SHALL raise a NonTransientError whose reason indicates that audience activity requires YouTube_Analytics_API authorization for the Owned_Channel.
4. IF the YouTube_Analytics_API responds to get_audience_activity with an authorization or permission error, THEN THE YouTube_Data_Source SHALL raise a NonTransientError whose reason identifies the Owned_Channel and SHALL NOT return an AudienceActivity value.
5. WHEN the YouTube_Analytics_API responds to get_audience_activity with a rate limit, a server error of status 500 or greater, a network connection error, or no complete response within the configured request timeout, THE YouTube_Data_Source SHALL raise the member of the Data_Source_Error_Hierarchy that corresponds to that condition per Requirement 2.
6. WHERE OAuth_Credentials authorizing the YouTube_Analytics_API for the Owned_Channel are configured AND the YouTube_Analytics_API returns a successful response containing no audience activity data, THE YouTube_Data_Source SHALL return an AudienceActivity value whose channel identifier equals the requested Owned_Channel identifier, whose days_covered is 0, and whose hourly activity buckets are empty.

### Requirement 4: Keyword Metrics Sourcing

**User Story:** As a Creator, I want keyword demand and competition supplied by a configurable source, so that SEO keyword-gap analysis works when a keyword source is configured and degrades to insufficient-data when one is not.

#### Acceptance Criteria

1. WHERE a Keyword_Metrics_Provider is configured, WHEN get_keyword_metrics is called for a Channel_Category with a maximum keyword count, THE YouTube_Data_Source SHALL retrieve candidate keywords with their demand and competition values from the Keyword_Metrics_Provider and SHALL return one KeywordMetric value per retrieved keyword.
2. WHEN get_keyword_metrics returns KeywordMetric values, THE YouTube_Data_Source SHALL return no more than the requested maximum keyword count.
3. IF get_keyword_metrics is called AND no Keyword_Metrics_Provider is configured, THEN THE YouTube_Data_Source SHALL return an empty list of KeywordMetric values.
4. IF a configured Keyword_Metrics_Provider is unavailable or returns an error when get_keyword_metrics is called, THEN THE YouTube_Data_Source SHALL raise the member of the Data_Source_Error_Hierarchy that corresponds to that condition per Requirement 2.

### Requirement 5: Viral Template Performance Sourcing

**User Story:** As a Creator, I want viral template performance derived from real retrieved video data through a configurable strategy, so that idea scoring and format recommendation have inputs when a strategy is configured and degrade gracefully when one is not.

#### Acceptance Criteria

1. WHERE a Template_Performance_Strategy is configured, WHEN get_template_performance is called for a Channel_Category, THE YouTube_Data_Source SHALL derive template performance values for that Channel_Category by applying the Template_Performance_Strategy to retrieved video statistics and SHALL return one TemplatePerformance value per derived template.
2. WHEN the YouTube_Data_Source returns a TemplatePerformance value, THE YouTube_Data_Source SHALL populate the template identifier, the Channel_Category, the observed performance, the sample size, the Short-format average view count, and the long-form-format average view count.
3. IF get_template_performance is called AND no Template_Performance_Strategy is configured, THEN THE YouTube_Data_Source SHALL return an empty list of TemplatePerformance values.
4. IF deriving template performance requires retrieving data from the YouTube_Data_API AND that retrieval fails, THEN THE YouTube_Data_Source SHALL raise the member of the Data_Source_Error_Hierarchy that corresponds to that failure per Requirement 2.

### Requirement 6: LLM Generation Provider

**User Story:** As a Creator, I want creative artifacts produced by a real language model behind the existing generation interface, so that titles, thumbnails, outlines, scripts, and descriptions are generated for my ideas without changing the generation consumers.

#### Acceptance Criteria

1. THE LLM_Generation_Provider SHALL implement the Generation_Provider protocol methods generate_titles, generate_thumbnails, generate_outline, generate_script, and generate_description with the parameter signatures and return types of the existing Generation_Provider protocol.
2. WHEN generate_titles is called for a Content_Idea with a count N where N is an integer of 1 or greater, THE LLM_Generation_Provider SHALL request N title candidates from the LLM_Service and SHALL return one title string per title candidate produced by the LLM_Service.
3. WHEN generate_thumbnails is called for a Content_Idea with a count N where N is an integer of 1 or greater, THE LLM_Generation_Provider SHALL request N thumbnail concepts from the LLM_Service and SHALL return one ThumbnailDraft per concept produced by the LLM_Service, each populated with a non-empty visual description and a text overlay.
4. WHEN generate_outline, generate_script, or generate_description is called for a Content_Idea, THE LLM_Generation_Provider SHALL request the corresponding artifact from the LLM_Service and SHALL return the artifact string produced by the LLM_Service.
5. THE LLM_Generation_Provider SHALL return the artifacts produced by the LLM_Service without enforcing the domain title-distinctness, title-length, thumbnail-overlay-length, or description-length constraints.
6. IF a request to the LLM_Service for a requested item fails, does not complete within the configured request timeout, returns zero content items, or returns only content that is empty or contains solely whitespace characters, THEN THE LLM_Generation_Provider SHALL raise a Generation_Error that identifies the failed item and the affected Content_Idea identifier and SHALL NOT return a partial artifact for that item.
7. WHEN the LLM_Generation_Provider raises a Generation_Error, THE LLM_Generation_Provider SHALL set the error reason to a description that excludes every Secret value.
8. IF generate_titles or generate_thumbnails is called with a count less than 1, THEN THE LLM_Generation_Provider SHALL raise a Generation_Error that identifies the failed item and the affected Content_Idea identifier and SHALL NOT issue a request to the LLM_Service.

### Requirement 7: Email Delivery

**User Story:** As a Creator, I want the digest delivered to my email, so that I receive recommendations without logging in.

#### Acceptance Criteria

1. THE Email_Deliverer SHALL implement the Deliverer protocol with a single deliver method that accepts a Digest_Report.
2. WHEN deliver is called, THE Email_Deliverer SHALL render the Digest_Report and verify that all three report sections are present, including the no-items indicator for each section that contains zero items, before attempting transmission.
3. WHEN deliver is called AND the Digest_Report is transmitted successfully to the configured email recipient, THE Email_Deliverer SHALL return without a value.
4. WHEN the Email_Deliverer renders a Digest_Report for transmission, THE Email_Deliverer SHALL include all three report sections and SHALL include the no-items indicator for each section that contains zero items.
5. IF transmission of the Digest_Report to the configured email service fails, THEN THE Email_Deliverer SHALL raise a Delivery_Error whose reason describes the failure.
6. WHEN the Email_Deliverer raises a Delivery_Error, THE Email_Deliverer SHALL set the error reason to a description that excludes every Secret value.

### Requirement 8: Slack Delivery

**User Story:** As a Creator, I want the digest posted to my Slack workspace, so that my team sees recommendations in our shared channel.

#### Acceptance Criteria

1. THE Slack_Deliverer SHALL implement the Deliverer protocol with a single deliver method that accepts a Digest_Report.
2. WHEN deliver is called, THE Slack_Deliverer SHALL render the Digest_Report and verify that all three report sections are present, including the no-items indicator for each section that contains zero items, before attempting to post.
3. WHEN deliver is called AND the Digest_Report is posted successfully to the configured Slack destination, THE Slack_Deliverer SHALL return without a value.
4. WHEN the Slack_Deliverer renders a Digest_Report for posting, THE Slack_Deliverer SHALL include all three report sections and SHALL include the no-items indicator for each section that contains zero items.
5. IF posting the Digest_Report to the configured Slack destination fails, THEN THE Slack_Deliverer SHALL raise a Delivery_Error whose reason describes the failure.
6. IF the Slack_Deliverer cannot construct or raise a Delivery_Error after a posting failure, THEN THE Slack_Deliverer SHALL record a delivery-failed log entry that identifies the Slack destination and SHALL surface a delivery failure to the caller.
7. WHEN the Slack_Deliverer raises a Delivery_Error, THE Slack_Deliverer SHALL set the error reason to a description that excludes every Secret value.

### Requirement 9: Notion Delivery

**User Story:** As a Creator, I want the digest recorded in my Notion database, so that recommendations are archived where I organize my work.

#### Acceptance Criteria

1. THE Notion_Deliverer SHALL implement the Deliverer protocol with a single deliver method that accepts a Digest_Report.
2. WHEN deliver is called, THE Notion_Deliverer SHALL render the Digest_Report and verify that all three report sections are present, including the no-items indicator for each section that contains zero items, before attempting to record it.
3. WHEN deliver is called AND the Digest_Report is recorded successfully in the configured Notion database, THE Notion_Deliverer SHALL return without a value.
4. WHEN the Notion_Deliverer records a Digest_Report, THE Notion_Deliverer SHALL include all three report sections and SHALL include the no-items indicator for each section that contains zero items.
5. IF recording the Digest_Report in the configured Notion database fails, THEN THE Notion_Deliverer SHALL raise a Delivery_Error whose reason describes the failure.
6. WHEN the Notion_Deliverer raises a Delivery_Error, THE Notion_Deliverer SHALL set the error reason to a description that excludes every Secret value.

### Requirement 10: Configuration Loading and Layered Precedence

**User Story:** As a Creator who is also a developer, I want configuration assembled from layered sources with a clear precedence, so that I can use a local .env file during development and real environment variables or a secret provider in production without changing code.

#### Acceptance Criteria

1. THE Config_Loader SHALL assemble Settings from the Configuration_Sources in the following order of decreasing precedence: explicit overrides, then process environment variables, then the .env file, then configuration-file defaults.
2. WHEN a configuration key is supplied by more than one Configuration_Source, THE Config_Loader SHALL select the value from the Configuration_Source with the highest precedence.
3. WHEN a configuration key is absent from every Configuration_Source AND a default value is defined for that key, THE Config_Loader SHALL use the defined default value.
4. WHEN the Config_Loader reads the .env file, THE Config_Loader SHALL treat a line whose first non-whitespace character is a number sign (#) as a comment and ignore it, SHALL ignore blank lines, and SHALL parse every other line as a KEY=VALUE pair split at the first equals sign, trimming surrounding whitespace from the key and the value.
5. THE Config_Loader SHALL map each supplied configuration value to its corresponding Settings field using the documented configuration key for that field.
6. IF a non-blank, non-comment line in the .env file contains no equals sign, THEN THE Config_Loader SHALL report that line as malformed, identified by its line number, and SHALL NOT apply a value from that line.

### Requirement 11: Startup Validation of Required Configuration

**User Story:** As a Creator, I want the agent to validate configuration at startup and report every problem at once, so that I can fix all configuration errors before any external request is made.

#### Acceptance Criteria

1. WHEN the Composition_Root starts, THE Config_Loader SHALL validate that every required configuration value for each enabled component is present and well-formed, where well-formed means non-empty and conforming to the documented value type and format for that configuration key, before any external request is issued.
2. IF one or more required configuration values are missing or malformed at startup, THEN THE Config_Loader SHALL report every missing or malformed configuration value, each identified by its configuration key, SHALL prevent the Automation_Scheduler from running, and SHALL NOT issue any external request.
3. WHEN the Config_Loader validates configuration at startup, THE Config_Loader SHALL evaluate the configuration values for every Delivery_Destination selected in the Settings and SHALL report each selected Delivery_Destination whose required configuration values are missing or malformed.
4. WHERE a Delivery_Destination is not selected in the Settings, THE Config_Loader SHALL treat the configuration values for that Delivery_Destination as not required.
5. WHEN the Config_Loader reports a missing or malformed configuration value, THE Config_Loader SHALL identify the value by its configuration key and SHALL exclude every Secret value from the report.
6. WHEN every required configuration value for each enabled component is present and well-formed at startup, THE Config_Loader SHALL produce validated Settings and SHALL allow the Composition_Root to run the Automation_Scheduler.

### Requirement 12: Secret Protection

**User Story:** As a Creator, I want my credentials kept out of logs and run summaries, so that operating the agent does not expose my secrets.

#### Acceptance Criteria

1. WHEN the Real_Provider_Integration records a Secret in a log entry or a run summary, THE Real_Provider_Integration SHALL record the Credential_Reference for that Secret in place of the Secret value.
2. WHEN the Composition_Root emits a startup summary or the Automation_Scheduler emits a run summary, THE Real_Provider_Integration SHALL exclude every Secret value from the emitted summary.
3. IF a log entry, an error message, or a run summary would otherwise contain a Secret value, THEN THE Real_Provider_Integration SHALL redact that Secret value from the output.
4. THE Real_Provider_Integration SHALL load each Secret from a Configuration_Source at runtime and SHALL keep the .env file and any local credential file excluded from version control.
5. IF a required Secret cannot be loaded from any Configuration_Source at runtime, THEN THE Config_Loader SHALL prevent startup, SHALL report the Credential_Reference of the Secret that could not be loaded, and SHALL exclude every Secret value from the report.

### Requirement 13: Authentication and Authorization

**User Story:** As a Creator, I want public data accessed with an API key and my owned-channel analytics accessed with OAuth, so that the agent uses the right credential for each request and tells me clearly when my credentials need attention.

#### Acceptance Criteria

1. WHEN a request retrieves public Owned_Channel or Competitor_Channel data from the YouTube_Data_API, THE Auth_Manager SHALL authenticate the request using the configured YouTube Data API key.
2. WHEN a request retrieves Owned_Channel audience activity from the YouTube_Analytics_API, THE Auth_Manager SHALL authenticate the request using the OAuth_Credentials for that Owned_Channel.
3. WHEN a request to the YouTube_Analytics_API fails with a response indicating that the access token within the OAuth_Credentials is expired AND a refresh token is available, THE Auth_Manager SHALL obtain a new access token using the refresh token and SHALL reissue the failed request exactly once with the new access token.
4. IF obtaining a new access token using the refresh token fails, THEN THE Auth_Manager SHALL raise a NonTransientError that identifies the Owned_Channel and indicates that the OAuth_Credentials require re-authorization.
5. IF the configured YouTube Data API key is rejected as invalid by the YouTube_Data_API, THEN THE YouTube_Data_Source SHALL raise a NonTransientError whose reason indicates that the YouTube Data API key is invalid.
6. WHEN the Auth_Manager obtains or refreshes an access token, THE Auth_Manager SHALL exclude every Secret value from log entries.
7. IF a request to the YouTube_Analytics_API fails with a response indicating that the access token within the OAuth_Credentials is expired AND no refresh token is available, THEN THE Auth_Manager SHALL raise a NonTransientError that identifies the Owned_Channel and indicates that the OAuth_Credentials require re-authorization.

### Requirement 14: Composition Root and Scheduled Run

**User Story:** As a Creator, I want a single entry point that reads my configuration, wires up the real components, and runs the scheduler, so that I can run the agent against my channel without manual wiring.

#### Acceptance Criteria

1. WHEN the Composition_Root runs with valid Settings, THE Composition_Root SHALL construct a Resilient_Data_Source that wraps the YouTube_Data_Source.
2. WHEN the Composition_Root runs with valid Settings, THE Composition_Root SHALL construct the LLM_Generation_Provider and SHALL construct one Deliverer for each Delivery_Destination selected in the Settings.
3. WHEN the Composition_Root runs with valid Settings, THE Composition_Root SHALL build a Configuration from the Settings that includes the authorized channels, the selected Channel_Category, the monitored Competitor_Channels, the Schedule, and the Delivery_Destinations.
4. WHEN the Composition_Root has constructed the components, THE Composition_Root SHALL run the Automation_Scheduler with the constructed Resilient_Data_Source, the constructed Deliverers, and the built Configuration.
5. WHERE the Settings specify a Schedule with both a recurrence interval and a run time, THE Composition_Root SHALL run the Automation_Scheduler on that Schedule.
6. WHERE the Settings specify no Schedule with both a recurrence interval and a run time, THE Composition_Root SHALL run the Automation_Scheduler only when the Creator manually triggers a run.
7. IF construction of any required component fails, THEN THE Composition_Root SHALL prevent the Automation_Scheduler from running, including when reporting the construction failure itself fails, and SHALL report the failure identifying the component that failed to construct when reporting is possible.

### Requirement 15: Third-Party Dependency Policy and Edge Isolation

**User Story:** As a developer, I want third-party client libraries confined to the edges and kept optional, so that the dependency-free core and its property tests keep working without installing them.

#### Acceptance Criteria

1. THE Real_Provider_Integration SHALL declare every third-party client library it requires as an optional dependency extra in the project metadata, pinned to an exact version.
2. THE Real_Provider_Integration SHALL confine imports of third-party client libraries to the infrastructure, generation, and delivery edge modules and SHALL NOT include an import of a third-party client library in the domain layer or the analysis layer.
3. WHERE the Python standard library provides a required capability, THE Real_Provider_Integration SHALL use the Python standard library for that capability rather than adding a third-party dependency for it.
4. WHEN the optional integration extras are not installed, THE Viral_Topic_Agent core test suite SHALL run to completion without importing any third-party client library.
5. IF a third-party client library is imported by a core module when the optional integration extras are not installed, THEN the affected test SHALL fail with an import error rather than be guarded at runtime.

### Requirement 16: Preservation of Existing Behavior and Testability

**User Story:** As a developer, I want the real providers to drop into the existing seams and stay testable with injected fakes, so that the existing 33 correctness properties keep passing and I can test the edges without real network access.

#### Acceptance Criteria

1. THE Real_Provider_Integration SHALL satisfy the existing Data_Source, Generation_Provider, and Deliverer protocols without modifying those protocol definitions.
2. WHEN the Viral_Topic_Agent test suite runs after the Real_Provider_Integration is added, THE Real_Provider_Integration SHALL leave each of the existing 33 correctness properties passing.
3. THE YouTube_Data_Source, the LLM_Generation_Provider, and each Deliverer SHALL accept an injected transport or client dependency so that each component can be exercised in tests without performing a real network request.
4. WHEN the YouTube_Data_Source or a Deliverer performs a time-dependent operation, THE component SHALL obtain the current time from an injected Clock.
5. THE YouTube_Data_Source SHALL signal failures only by raising members of the Data_Source_Error_Hierarchy and SHALL NOT perform retry or rate-limit backoff internally, leaving retry, backoff, and timeout policy to the Resilient_Data_Source.
