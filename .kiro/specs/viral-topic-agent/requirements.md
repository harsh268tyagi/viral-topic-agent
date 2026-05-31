# Requirements Document

## Introduction

The Viral Topic Agent is an automated system that helps a YouTube creator grow a channel by analyzing the creator's existing channel, discovering trending and historically viral content ideas and templates, and turning those ideas into ready-to-produce assets. The system filters ideas by channel category (for example gaming, music, entertainment, sports), scores each idea by predicted view potential for the specific channel, tracks competitor channels, and delivers a recurring digest of recommendations to the creator's preferred destination. The system is designed to run end to end on a schedule with minimal manual intervention.

This document defines the functional and quality requirements for the system. Requirements describe what the system does, not how it is implemented. Implementation choices (programming language, data source APIs, hosting, model selection) are deferred to the design phase. Where external data sources are required, requirements reference the data source abstractly so that the design can select an appropriate provider.

## Glossary

- **Viral_Topic_Agent**: The complete system described in this document, composed of the components defined below.
- **Creator**: The human user who owns the YouTube channel and operates the Viral_Topic_Agent.
- **Owned_Channel**: The YouTube channel that the Creator owns and has authorized the Viral_Topic_Agent to analyze.
- **Competitor_Channel**: A YouTube channel, other than the Owned_Channel, that the Creator designates for monitoring.
- **Channel_Analyzer**: The component that retrieves and summarizes performance data for the Owned_Channel.
- **Trend_Discovery_Engine**: The component that identifies trending and historically viral content ideas and templates across configurable time windows.
- **Time_Window**: One of three discovery periods: weekly (trailing 7 days), monthly (trailing 30 days), and all-time (full available history).
- **Content_Idea**: A candidate topic for a new video, including a working title concept and a supporting rationale.
- **Viral_Template**: A reusable content format or structure (for example "tier-list ranking", "reaction", "challenge") associated with high view performance.
- **Channel_Category**: A content classification such as gaming, music, entertainment, or sports.
- **Category_Filter**: The component that restricts Content_Ideas and Viral_Templates to a selected Channel_Category.
- **Competitor_Tracker**: The component that monitors Competitor_Channels and detects performance spikes.
- **Outlier_Detector**: The component that identifies videos whose view count substantially exceeds a channel's baseline.
- **Baseline_View_Count**: The median view count of a channel's recent videos, used as the reference for outlier detection.
- **Concept_Generator**: The component that produces title and thumbnail concepts for a Content_Idea.
- **Publish_Time_Predictor**: The component that recommends a publishing time based on Owned_Channel audience activity.
- **Script_Generator**: The component that converts a selected Content_Idea into a video outline, script draft, SEO tags, and a description.
- **SEO_Analyzer**: The component that identifies keywords with high search demand and low competition.
- **Format_Recommender**: The component that recommends whether a Content_Idea is better produced as a Short or a long-form video.
- **Idea_Scorer**: The component that assigns each Content_Idea a predicted view potential score for the Owned_Channel.
- **Idea_Score**: A numeric value from 0 to 100 representing predicted view potential, where higher values indicate greater predicted potential.
- **Digest_Service**: The component that compiles recommendations into a report and delivers the report to a configured Delivery_Destination.
- **Delivery_Destination**: A configured output target for the digest, one of email, Slack, or Notion.
- **Automation_Scheduler**: The component that runs the end-to-end workflow on a recurring schedule.
- **Configuration**: The persisted set of Creator settings, including authorized channels, selected Channel_Category, monitored Competitor_Channels, schedule, and Delivery_Destination.
- **Data_Source**: An external provider of YouTube channel and video data that the Viral_Topic_Agent queries.

## Requirements

### Requirement 1: Channel Authorization and Connection

**User Story:** As a Creator, I want to connect and authorize my YouTube channel, so that the agent can analyze my channel data on my behalf.

#### Acceptance Criteria

1. WHEN the Creator initiates channel connection, THE Viral_Topic_Agent SHALL request authorization to access the Owned_Channel data within 5 seconds of the initiation.
2. WHEN the Creator grants authorization, THE Viral_Topic_Agent SHALL store the authorization credentials in the Configuration.
3. IF authorization is denied by the Creator, THEN THE Viral_Topic_Agent SHALL record an authorization-failed status and SHALL NOT attempt to retrieve Owned_Channel data.
4. WHEN a data request is made with valid stored authorization credentials, THE Viral_Topic_Agent SHALL retrieve the requested Owned_Channel data from the Data_Source within 30 seconds.
5. IF stored authorization credentials are expired when a data request is made, THEN THE Viral_Topic_Agent SHALL return an authorization-expired error that identifies the affected Owned_Channel.
6. WHERE the Creator authorizes more than one Owned_Channel, up to a maximum of 50 Owned_Channels, THE Viral_Topic_Agent SHALL associate each retrieved data set with the corresponding Owned_Channel identifier.
7. IF the Creator neither grants nor denies authorization within 300 seconds of the authorization request, THEN THE Viral_Topic_Agent SHALL record an authorization-timeout status and SHALL NOT attempt to retrieve Owned_Channel data.
8. IF storing the authorization credentials in the Configuration fails, THEN THE Viral_Topic_Agent SHALL record a credential-storage-failed status, SHALL return an error indicating the credentials were not saved, and SHALL NOT mark the Owned_Channel as connected.
9. IF the Data_Source is unavailable or returns an error when a data request is made with valid stored authorization credentials, THEN THE Viral_Topic_Agent SHALL retry the request up to 3 times and, if all retries fail, SHALL return a data-retrieval-failed error that identifies the affected Owned_Channel and SHALL retain the stored authorization credentials.

### Requirement 2: Owned Channel Analysis

**User Story:** As a Creator, I want the agent to analyze my current channel, so that I understand my channel's performance and content profile.

#### Acceptance Criteria

1. WHEN channel analysis is requested for an authorized Owned_Channel, THE Channel_Analyzer SHALL retrieve the channel metadata, the published video list, and per-video view counts from the Data_Source.
2. WHEN Owned_Channel data is retrieved, THE Channel_Analyzer SHALL compute the Baseline_View_Count as the median of the per-video view counts of the most recent 30 published videos of the Owned_Channel, or of all published videos when the Owned_Channel has fewer than 30 published videos.
3. WHEN Owned_Channel data is retrieved, THE Channel_Analyzer SHALL produce a channel profile that includes the detected Channel_Category, the total subscriber count, the total video count, and the Baseline_View_Count.
4. IF the Data_Source returns zero published videos for the Owned_Channel, THEN THE Channel_Analyzer SHALL produce a channel profile that marks the Baseline_View_Count as unavailable.
5. IF the Data_Source reports a partial failure but returns retrievable Owned_Channel data, THEN THE Channel_Analyzer SHALL produce the channel profile from the retrieved data and SHALL record the reported partial-failure reason.
6. IF the Data_Source request returns no Owned_Channel data, THEN THE Channel_Analyzer SHALL return a data-retrieval error that identifies the Owned_Channel and the failure reason.
7. IF the Owned_Channel has at least 1 and fewer than 5 published videos, THEN THE Channel_Analyzer SHALL produce a channel profile that marks the Baseline_View_Count as low-confidence.

### Requirement 3: Trend and Viral Idea Discovery

**User Story:** As a Creator, I want the agent to suggest ideas and viral templates that are trending weekly, monthly, and all-time, so that I can choose timely topics.

#### Acceptance Criteria

1. WHEN idea discovery is requested, THE Trend_Discovery_Engine SHALL produce between 1 and 20 Content_Ideas for each Time_Window: weekly, monthly, and all-time.
2. WHEN the Trend_Discovery_Engine produces a Content_Idea, THE Trend_Discovery_Engine SHALL associate the Content_Idea with between 1 and 5 Viral_Templates.
3. WHEN the Trend_Discovery_Engine produces a Content_Idea, THE Trend_Discovery_Engine SHALL include the Time_Window from which the Content_Idea was derived and a supporting rationale that references at least one observed performance metric value recorded within that Time_Window.
4. IF the Data_Source returns no data for all requested Time_Windows, THEN THE Trend_Discovery_Engine SHALL return an empty result for each of the weekly, monthly, and all-time Time_Windows.
5. IF the Data_Source returns no data for a single requested Time_Window, THEN THE Trend_Discovery_Engine SHALL return an empty result for that Time_Window and SHALL produce between 1 and 20 Content_Ideas for each remaining requested Time_Window.
6. WHERE the Creator requests a specific Time_Window, THE Trend_Discovery_Engine SHALL produce between 1 and 20 Content_Ideas for the requested Time_Window and SHALL return an empty result for each non-requested Time_Window.
7. IF the Data_Source does not respond within 5 seconds or is unavailable for a requested Time_Window, THEN THE Trend_Discovery_Engine SHALL return an empty result for that Time_Window, SHALL include an error indication identifying the affected Time_Window, and SHALL produce results for each remaining requested Time_Window.
8. WHEN idea discovery is requested, THE Trend_Discovery_Engine SHALL return a response within 10 seconds.

### Requirement 4: Category-Based Filtering

**User Story:** As a Creator, I want to filter ideas by my channel's category, so that I only see ideas relevant to my content.

#### Acceptance Criteria

1. WHEN the Creator selects one of the supported Channel_Categories, THE Category_Filter SHALL return only the Content_Ideas and Viral_Templates associated with the selected Channel_Category.
2. WHERE no Channel_Category is selected by the Creator and a Channel_Category was detected for the Owned_Channel during channel analysis, THE Category_Filter SHALL apply the detected Channel_Category.
3. THE Category_Filter SHALL support the Channel_Categories gaming, music, entertainment, and sports.
4. IF a selected Channel_Category produces no matching Content_Ideas but produces matching Viral_Templates, THEN THE Category_Filter SHALL return the matching Viral_Templates.
5. IF a selected Channel_Category produces no matching Content_Ideas and no matching Viral_Templates, THEN THE Category_Filter SHALL return an empty result and a no-matches indicator for the selected Channel_Category.
6. IF the Creator selects a Channel_Category that is not one of the supported Channel_Categories, THEN THE Category_Filter SHALL reject the selection and SHALL return an unsupported-category error that identifies the selected Channel_Category.
7. IF no Channel_Category is selected by the Creator and no Channel_Category was detected for the Owned_Channel during channel analysis, THEN THE Category_Filter SHALL return a category-unavailable indicator and SHALL NOT apply category filtering to the Content_Ideas and Viral_Templates.

### Requirement 5: Idea Scoring

**User Story:** As a Creator, I want each idea scored by predicted view potential for my channel, so that I can prioritize the highest-potential ideas.

#### Acceptance Criteria

1. WHEN the Idea_Scorer evaluates a Content_Idea, THE Idea_Scorer SHALL assign an integer Idea_Score from 0 to 100 inclusive.
2. WHEN the Idea_Scorer assigns an Idea_Score, THE Idea_Scorer SHALL compute the Idea_Score using the Owned_Channel Baseline_View_Count and the observed performance of each Viral_Template associated with the Content_Idea.
3. WHEN multiple Content_Ideas are scored, THE Idea_Scorer SHALL order the Content_Ideas first in descending Idea_Score sequence and, for Content_Ideas with an equal Idea_Score, in descending associated Viral_Template observed performance sequence.
4. IF the Baseline_View_Count is unavailable or equal to zero for the Owned_Channel, THEN THE Idea_Scorer SHALL compute the Idea_Score using the Channel_Category aggregate performance and SHALL mark the Idea_Score as low-confidence.
5. IF the Baseline_View_Count is unavailable or equal to zero for the Owned_Channel and the Channel_Category aggregate performance is also unavailable, THEN THE Idea_Scorer SHALL withhold the Idea_Score for the Content_Idea and SHALL return an insufficient-data indicator that identifies the Content_Idea.

### Requirement 6: Competitor Channel Tracking

**User Story:** As a Creator, I want to monitor rival channels and be alerted to their spikes, so that I can react to competitor successes.

#### Acceptance Criteria

1. WHEN the Creator adds a Competitor_Channel that is not already present in the Configuration, THE Competitor_Tracker SHALL store the Competitor_Channel identifier in the Configuration.
2. WHEN competitor monitoring runs, THE Competitor_Tracker SHALL retrieve, for each monitored Competitor_Channel, the list of videos published within the trailing 30 days and the per-video view counts for those videos from the Data_Source.
3. WHEN the Competitor_Tracker retrieves videos for a monitored Competitor_Channel, THE Competitor_Tracker SHALL compute the Baseline_View_Count for that Competitor_Channel from the retrieved per-video view counts.
4. WHEN a monitored Competitor_Channel video has a view count greater than zero that exceeds that Competitor_Channel's Baseline_View_Count by a factor of 3 or more, THE Competitor_Tracker SHALL flag the video as a competitor spike.
5. WHEN a competitor spike is flagged, THE Competitor_Tracker SHALL record the Competitor_Channel identifier, the video identifier, the video view count, and the spike factor.
6. IF a monitored Competitor_Channel has fewer than 5 retrieved videos within the trailing 30 days, THEN THE Competitor_Tracker SHALL record an insufficient-data status for that Competitor_Channel, SHALL skip competitor spike detection for that Competitor_Channel, and SHALL continue monitoring the remaining Competitor_Channels.
7. IF a monitored Competitor_Channel is unavailable from the Data_Source, THEN THE Competitor_Tracker SHALL record an unavailable status for that Competitor_Channel and SHALL continue monitoring the remaining Competitor_Channels.
8. IF the Creator adds a Competitor_Channel when the Configuration already contains 50 monitored Competitor_Channels, THEN THE Competitor_Tracker SHALL reject the addition and SHALL return a limit-reached indication that identifies the maximum of 50 monitored Competitor_Channels.

### Requirement 7: Outlier and Viral Detection

**User Story:** As a Creator, I want the agent to detect videos that far exceed a channel's normal performance, so that I can identify proven viral content.

#### Acceptance Criteria

1. WHEN outlier detection runs for a channel, THE Outlier_Detector SHALL compute the Baseline_View_Count from the view counts of that channel's most recent published videos, using up to the 50 most recent published videos.
2. WHEN the Baseline_View_Count is greater than zero and a video view count is greater than zero and the ratio of that video view count to the Baseline_View_Count is 5.0 or greater, THE Outlier_Detector SHALL classify the video as an outlier and SHALL set the outlier factor for that video to that ratio.
3. WHEN the Outlier_Detector classifies a video as an outlier, THE Outlier_Detector SHALL record the video identifier, the video view count, and the outlier factor, where the outlier factor is the ratio of the video view count to the Baseline_View_Count.
4. IF a channel has fewer than 5 published videos, THEN THE Outlier_Detector SHALL return an insufficient-data indicator for that channel and SHALL NOT classify any of the channel's videos as an outlier.
5. IF the Baseline_View_Count for the channel is zero or unavailable when outlier detection runs, THEN THE Outlier_Detector SHALL return an insufficient-data indicator for that channel and SHALL NOT classify any of the channel's videos as an outlier.

### Requirement 8: Title and Thumbnail Concept Generation

**User Story:** As a Creator, I want title and thumbnail concepts for each idea, so that I can produce click-worthy assets faster.

#### Acceptance Criteria

1. WHEN the Concept_Generator processes a Content_Idea, THE Concept_Generator SHALL produce at least 3 distinct title concepts for the Content_Idea.
2. WHEN the Concept_Generator processes a Content_Idea, THE Concept_Generator SHALL produce at least 1 thumbnail concept that includes a visual description and a text overlay suggestion of at most 30 characters.
3. WHEN the Concept_Generator produces a title concept, THE Concept_Generator SHALL limit each title concept to between 1 and 100 characters inclusive.
4. WHERE the Content_Idea is associated with a Channel_Category, THE Concept_Generator SHALL produce title and thumbnail concepts that belong to the associated Channel_Category.
5. IF the Concept_Generator cannot produce the required title and thumbnail concepts for a Content_Idea, THEN THE Concept_Generator SHALL NOT produce partial concepts for that Content_Idea and SHALL return an error indication identifying the Content_Idea that could not be processed.

### Requirement 9: Publish Time Prediction

**User Story:** As a Creator, I want a recommended best time to publish, so that my videos reach the most active audience.

#### Acceptance Criteria

1. WHEN a publish-time recommendation is requested for the Owned_Channel, THE Publish_Time_Predictor SHALL retrieve the Owned_Channel audience activity data covering at least the most recent 7 days from the Data_Source.
2. WHEN audience activity data covering at least 7 days is available, THE Publish_Time_Predictor SHALL recommend exactly one publishing day of week and exactly one contiguous time window between 1 and 3 hours in duration, expressed in the Creator's configured time zone.
3. IF no time zone is configured for the Creator, THEN THE Publish_Time_Predictor SHALL express the recommended day of week and time window in UTC.
4. IF retrieval of audience activity data fails, THEN THE Publish_Time_Predictor SHALL retry the retrieval up to a maximum of 3 total attempts including the original request, and SHALL return an audience-data-retrieval error that identifies the Owned_Channel if all 3 attempts fail.
5. IF audience activity data is unavailable for the Owned_Channel, THEN THE Publish_Time_Predictor SHALL return a recommendation derived from the Channel_Category aggregate activity and SHALL mark the recommendation as low-confidence.
6. IF both the Owned_Channel audience activity data and the Channel_Category aggregate activity are unavailable, THEN THE Publish_Time_Predictor SHALL return a no-data error that identifies the Owned_Channel and SHALL NOT return a recommendation.

### Requirement 10: Trend-to-Script Generation

**User Story:** As a Creator, I want a chosen idea turned into a script, SEO tags, and a description, so that I can move directly into production.

#### Acceptance Criteria

1. WHEN the Creator selects a Content_Idea for script generation, THE Script_Generator SHALL produce a video outline, a script draft, a set of SEO tags, and a video description within 60 seconds.
2. WHEN the Script_Generator produces SEO tags, THE Script_Generator SHALL include every keyword supplied by the SEO_Analyzer for the Content_Idea and SHALL produce between 5 and 30 SEO tags in total.
3. WHEN the Script_Generator produces a video description, THE Script_Generator SHALL limit the description to between 100 and 5000 characters.
4. IF the SEO_Analyzer returns no keywords for the Content_Idea, THEN THE Script_Generator SHALL produce the video outline, script draft, and video description, and SHALL display an indication that the SEO tags are unavailable.
5. IF the Script_Generator cannot produce the video outline, script draft, or video description, THEN THE Script_Generator SHALL display an error indication to the Creator identifying the failed item and SHALL retain the selected Content_Idea so the Creator can retry generation.

### Requirement 11: SEO and Keyword Gap Analysis

**User Story:** As a Creator, I want to find high-demand, low-competition keywords, so that I can target topics I can rank for.

#### Acceptance Criteria

1. WHEN keyword analysis is requested for a Channel_Category, THE SEO_Analyzer SHALL retrieve search demand and competition data for up to 1,000 candidate keywords from the Data_Source within 10 seconds.
2. WHEN at least 4 candidate keywords are analyzed and a candidate keyword has search demand at or above the 50th percentile (top 50 percent) of analyzed keywords and competition at or below the 50th percentile (bottom 50 percent) of analyzed keywords, THE SEO_Analyzer SHALL classify the keyword as a keyword gap.
3. WHEN the SEO_Analyzer classifies keyword gaps, THE SEO_Analyzer SHALL order the keyword gaps in descending search demand sequence, breaking ties by ascending competition value.
4. IF no candidate keyword meets the keyword-gap criteria, THEN THE SEO_Analyzer SHALL return an empty keyword-gap result with a no-gap indicator.
5. IF the Data_Source is unavailable or returns an error during retrieval, THEN THE SEO_Analyzer SHALL return no keyword-gap result, retain any previously stored results, and provide an error indication identifying the retrieval failure.
6. IF fewer than 4 candidate keywords are retrieved for the Channel_Category, THEN THE SEO_Analyzer SHALL return an empty keyword-gap result with an insufficient-data indicator.

### Requirement 12: Format Recommendation

**User Story:** As a Creator, I want to know whether an idea suits a Short or a long-form video, so that I produce each idea in the best format.

#### Acceptance Criteria

1. WHEN the Format_Recommender evaluates a Content_Idea AND observed view-count data is available for at least 5 Viral_Template videos in the Short format and at least 5 Viral_Template videos in the long-form format, THE Format_Recommender SHALL recommend exactly one format: Short or long-form.
2. WHEN the Format_Recommender recommends a format AND the observed average view count of one format is higher than that of the other format, THE Format_Recommender SHALL recommend the format with the higher observed average view count.
3. IF the observed average view counts of the Short format and the long-form format are equal, THEN THE Format_Recommender SHALL recommend the Short format.
4. WHEN the Format_Recommender produces a recommendation, THE Format_Recommender SHALL include a rationale that references the observed average view count for both the Short format and the long-form format.
5. IF observed view-count data is available for fewer than 5 Viral_Template videos in either the Short format or the long-form format for the Content_Idea, THEN THE Format_Recommender SHALL withhold the format recommendation AND SHALL return an insufficient-performance-data indicator for the Content_Idea.

### Requirement 13: Recurring Digest Delivery

**User Story:** As a Creator, I want a recurring digest of recommendations delivered to my preferred channel, so that I receive guidance without logging in.

#### Acceptance Criteria

1. WHEN a digest is generated, THE Digest_Service SHALL compile the scored Content_Ideas, the flagged competitor spikes, and the detected outliers into a single report that contains a distinct section for each of these three item types.
2. WHEN a report section for an item type contains zero items, THE Digest_Service SHALL include a no-items indicator within that section.
3. WHEN a digest report is compiled, THE Digest_Service SHALL deliver the report to each Delivery_Destination configured in the Configuration.
4. IF no Delivery_Destination is configured in the Configuration when a digest report is compiled, THEN THE Digest_Service SHALL NOT attempt delivery and SHALL record a no-destination-configured status.
5. THE Digest_Service SHALL support the Delivery_Destinations email, Slack, and Notion.
6. IF delivery to a configured Delivery_Destination fails, THEN THE Digest_Service SHALL retry delivery to that Delivery_Destination up to a maximum of 3 total delivery attempts including the original attempt, and SHALL record a per-destination delivery-failed status for that Delivery_Destination if all 3 attempts fail.
7. WHERE the Creator configures more than one Delivery_Destination, THE Digest_Service SHALL deliver the report to each configured Delivery_Destination independently such that a delivery failure at one Delivery_Destination does not prevent delivery to the remaining Delivery_Destinations.

### Requirement 14: End-to-End Automation and Scheduling

**User Story:** As a Creator, I want the complete process to run automatically on a schedule, so that I get recommendations hands-free.

#### Acceptance Criteria

1. WHEN the Creator configures a recurring schedule that specifies both a recurrence interval and a run time, THE Automation_Scheduler SHALL store the schedule in the Configuration.
2. IF the Creator submits a schedule that omits the recurrence interval or the run time, THEN THE Automation_Scheduler SHALL reject the schedule, SHALL NOT store it in the Configuration, and SHALL return an error indicating which required schedule field is missing.
3. WHEN a scheduled run is triggered, THE Automation_Scheduler SHALL execute channel analysis, trend discovery, category filtering, idea scoring, competitor tracking, outlier detection, and digest delivery in that listed order.
4. IF a scheduled run is triggered while a previous run of the same schedule is still in progress, THEN THE Automation_Scheduler SHALL NOT start the overlapping run concurrently and SHALL record the overlapping trigger as skipped in the run summary.
5. IF a step in a scheduled run fails, THEN THE Automation_Scheduler SHALL record the failed step, SHALL skip each remaining step whose input depends on the failed step's output, and SHALL continue executing each remaining step that does not depend on the failed step's output.
6. WHEN a scheduled run completes, THE Automation_Scheduler SHALL record a run summary that lists each step with a status of succeeded, failed, or skipped, the run start time, and the run completion time.
7. WHERE no schedule is configured, THE Automation_Scheduler SHALL execute the workflow only when the Creator manually triggers a run.

### Requirement 15: Configuration Persistence and Round-Trip Integrity

**User Story:** As a Creator, I want my settings reliably saved and reloaded, so that the agent behaves consistently across runs.

#### Acceptance Criteria

1. WHEN the Creator saves Configuration settings, THE Viral_Topic_Agent SHALL serialize the authorized channels, the selected Channel_Category, the monitored Competitor_Channels, the schedule, and the Delivery_Destination of the Configuration to persistent storage.
2. WHEN the Viral_Topic_Agent successfully writes the serialized Configuration to persistent storage, THE Viral_Topic_Agent SHALL set the configuration-saved status to saved.
3. WHILE a persisted Configuration exists, WHEN the Viral_Topic_Agent starts a run, THE Viral_Topic_Agent SHALL deserialize the Configuration from persistent storage.
4. FOR ALL valid Configuration values, serializing the Configuration and then deserializing the serialized Configuration SHALL produce a Configuration that is field-by-field equal to the original Configuration across the authorized channels, the selected Channel_Category, the monitored Competitor_Channels, the schedule, and the Delivery_Destination (round-trip property).
5. IF the persisted Configuration cannot be deserialized due to corruption or format errors, THEN THE Viral_Topic_Agent SHALL return a configuration-invalid error that identifies the failing setting, SHALL NOT start the run, and SHALL NOT overwrite the persisted Configuration.
6. IF the Viral_Topic_Agent cannot serialize or write the Configuration to persistent storage, THEN THE Viral_Topic_Agent SHALL return a configuration-save error that identifies the failing setting and SHALL retain the previously persisted Configuration unchanged.
7. WHILE no persisted Configuration exists, WHEN the Viral_Topic_Agent starts a run, THE Viral_Topic_Agent SHALL return a configuration-missing notification and SHALL NOT start the run.

### Requirement 16: Data Source Error Handling and Rate Limits

**User Story:** As a Creator, I want the agent to handle data source failures gracefully, so that a single failure does not stop the whole process.

#### Acceptance Criteria

1. IF a Data_Source request exceeds the Data_Source rate limit, THEN THE Viral_Topic_Agent SHALL pause the affected requests and SHALL resume them after the retry interval reported by the Data_Source, or after a default interval of 60 seconds when the Data_Source reports no interval.
2. IF a Data_Source request fails with a transient error (a network timeout, a connection reset, or a Data_Source response indicating temporary unavailability), THEN THE Viral_Topic_Agent SHALL retry the request up to 3 times, waiting at least 2 seconds between successive retries, before recording a failure.
3. IF a Data_Source request fails with a non-transient error (an authentication rejection, an invalid-request rejection, or a Data_Source response indicating the request target does not exist), THEN THE Viral_Topic_Agent SHALL record the failure with the request target and the failure reason without retrying.
4. IF a Data_Source request does not receive a complete response within 30 seconds, THEN THE Viral_Topic_Agent SHALL treat the request as a transient failure.
5. IF a rate-limited Data_Source request remains paused for more than 300 seconds in total, THEN THE Viral_Topic_Agent SHALL record the failure with the request target and a failure reason indicating a rate-limit timeout.
6. WHEN a Data_Source request fails after all retries or is recorded as a non-transient failure, THE Viral_Topic_Agent SHALL record the failure with the request target and the failure reason within 5 seconds.
7. WHEN a Data_Source request has been recorded as a failure, THE Viral_Topic_Agent SHALL continue processing all requests that do not depend on the failed request.
