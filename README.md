# Viral Topic Agent

An automated agent that grows a YouTube channel by analyzing the channel, discovering trending and historically viral content ideas and templates, scoring them for the specific channel, tracking competitors, generating production-ready assets, and delivering a recurring digest to the creator's preferred destination.

The whole flow runs end to end on a schedule with minimal manual intervention.

## Features

- **Channel authorization & analysis** — connect up to 50 owned channels and compute a performance profile (subscriber count, video count, baseline view count, detected category).
- **Trend discovery** — surface 1–20 content ideas per time window (weekly / monthly / all-time), each backed by 1–5 viral templates and a metric-grounded rationale.
- **Category filtering** — restrict ideas and templates to gaming, music, entertainment, or sports.
- **Idea scoring** — assign each idea a 0–100 predicted-view-potential score and rank them, degrading gracefully when channel data is sparse.
- **Competitor tracking** — monitor rival channels and flag videos that spike ≥ 3× their baseline.
- **Outlier detection** — identify the channel's own videos that exceed its baseline by ≥ 5×.
- **Asset generation** — produce title/thumbnail concepts and full scripts (outline, script draft, SEO tags, description).
- **SEO keyword-gap analysis** — find high-demand, low-competition keywords.
- **Publish-time & format recommendations** — suggest the best day/time window and whether to go Short or long-form.
- **Recurring digest delivery** — compile recommendations and deliver to email, Slack, and/or Notion independently with bounded retry.
- **Automation scheduling** — run the full pipeline on a recurring schedule with overlap prevention and per-step run summaries.

## Design Principles

- **Resilience first.** Every external `Data_Source` call flows through a single `ResilientDataSource` layer that handles retries, rate-limit backoff, and timeouts. One failed request never corrupts unrelated results.
- **Component isolation.** Each analytical component is a pure-logic unit that consumes already-retrieved data and returns a deterministic result. Side effects (network, persistence, delivery) live at the edges.
- **Graceful degradation.** Partial data produces partial results with explicit status markers (`low-confidence`, `insufficient-data`, `unavailable`) rather than hard failures, except where a requirement mandates withholding output.
- **Round-trip integrity.** Configuration serialization is lossless and field-by-field reversible.

## Requirements

- Python **3.11+**

The runtime has **no third-party dependencies**. Testing uses `pytest` and `hypothesis`.

## Installation

```bash
# from the project root, in a virtual environment
python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # macOS/Linux

pip install -e ".[test]"
```

## Running the Tests

```bash
pytest
```

The suite includes:

- **Property-based tests** (Hypothesis, ≥ 100 examples each) validating the 33 documented correctness properties.
- **Unit / example tests** covering concrete branches, supported-set checks, and error paths.
- **Integration tests** verifying latency budgets and the end-to-end scheduled run.

## Project Structure

The package is organized into layered subpackages. Domain logic depends only
inward (toward more abstract layers); infrastructure concerns sit at the edges.

```
viral-topic-agent/
├── pyproject.toml
├── README.md
├── docs/
│   └── KNOWLEDGE.md                  # architecture, components, properties (start here)
├── src/viral_topic_agent/
│   ├── __init__.py                   # package version
│   ├── domain/                       # immutable core data models + enums
│   │   └── models.py
│   ├── infrastructure/               # cross-cutting primitives + external boundary
│   │   ├── clock.py                  # injectable Clock (RealClock / FakeClock)
│   │   ├── result.py                 # Result[T, E] (Ok / Err)
│   │   ├── datasource.py             # DataSource protocol + error hierarchy
│   │   └── resilient_data_source.py  # retry / rate-limit / timeout layer
│   ├── persistence/                  # configuration storage ("db" layer)
│   │   └── config_store.py           # (de)serialization + ConfigurationStore
│   ├── connection/                   # channel authorization lifecycle
│   │   └── connection_manager.py
│   ├── analysis/                     # pure transformations over retrieved data
│   │   ├── baseline.py               # shared median baseline computation
│   │   ├── channel_analyzer.py       # owned-channel profile
│   │   ├── trend_discovery.py        # trend / viral idea discovery
│   │   ├── category_filter.py        # category-based filtering
│   │   ├── scoring.py                # idea scoring + ranking
│   │   ├── competitor_tracker.py     # competitor monitoring + spikes
│   │   ├── outlier_detector.py       # outlier video detection
│   │   ├── seo_analyzer.py           # keyword-gap analysis
│   │   ├── publish_time_predictor.py # best publish day / window
│   │   └── format_recommender.py     # Short vs long-form
│   ├── generation/                   # creative assets via a provider interface
│   │   ├── provider.py               # GenerationProvider interface + stub
│   │   ├── concept_generator.py      # title / thumbnail concepts
│   │   └── script_generator.py       # script / SEO tags / description
│   ├── delivery/                     # report compilation + delivery
│   │   ├── deliverer.py              # Deliverer interface + per-destination stubs
│   │   └── digest_service.py         # digest report + delivery policy
│   └── orchestration/                # end-to-end pipeline
│       └── automation_scheduler.py
└── tests/
```

Each layer's `__init__.py` documents its responsibility. The `generation` and
`delivery` packages re-export their public symbols, so both
`from viral_topic_agent.generation import ScriptGenerator` and
`from viral_topic_agent.generation.script_generator import ScriptGenerator`
work.

## Quick Start

The pipeline is wired together by `AutomationScheduler`. With dependency injection you can run it against any `DataSource` implementation:

```python
from viral_topic_agent.orchestration.automation_scheduler import AutomationScheduler
from viral_topic_agent.infrastructure.resilient_data_source import ResilientDataSource, RetryPolicy
from viral_topic_agent.infrastructure.clock import RealClock
from viral_topic_agent.delivery import EmailDeliverer, SlackDeliverer
from viral_topic_agent.domain.models import Configuration, DeliveryDestination, Schedule

# `my_data_source` implements the DataSource protocol (see datasource.py).
source = ResilientDataSource(my_data_source, RetryPolicy(), RealClock())

scheduler = AutomationScheduler(
    source=source,
    deliverers={
        DeliveryDestination.EMAIL: EmailDeliverer(),
        DeliveryDestination.SLACK: SlackDeliverer(),
    },
)

config = Configuration(
    authorized_channels=(...),
    selected_category=None,
    monitored_competitors=("competitor-1",),
    schedule=Schedule(recurrence_interval="daily", run_time="08:00"),
    delivery_destinations=(DeliveryDestination.EMAIL, DeliveryDestination.SLACK),
)

summary = scheduler.run(config, RealClock(), manual=True)
for step in summary.steps:
    print(step.step, step.status.value)
```

The external YouTube data provider and the LLM used for generation sit behind the `DataSource` and `GenerationProvider` interfaces, so concrete providers can be swapped in without touching domain logic.

## Documentation

- **[docs/KNOWLEDGE.md](docs/KNOWLEDGE.md)** — architecture overview, component reference, data flow, error-handling model, and the full list of correctness properties.
- **`.kiro/specs/viral-topic-agent/`** — the requirements, design, and implementation plan that drive this codebase.

## License

MIT
