# <picture><source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/unpod-ai/odyssey/main/docs/assets/banner_dark.png"><img alt="odyssey" src="https://img.shields.io/badge/odyssey-%E2%9C%A8-blueviolet?style=for-the-badge&logo=rocket"></picture>

<p align="center">
  <strong>Training-Data Framework — Agent Traces In, Training Corpora Out.</strong>
</p>

<p align="center">
  <a href="https://github.com/unpod-ai/odyssey/actions"><img src="https://img.shields.io/badge/Build-passing-brightgreen?style=flat-square&logo=github-actions" alt="Build Status"></a>
  <a href="https://www.python.org/downloads/release/python-3120/"><img src="https://img.shields.io/badge/Python-3.12-blue?style=flat-square&logo=python&logoColor=white" alt="Python 3.12"></a>
  <a href="https://nodejs.org/"><img src="https://img.shields.io/badge/Node-22-green?style=flat-square&logo=nodedotjs&logoColor=white" alt="Node 22"></a>
  <a href="https://pnpm.io/"><img src="https://img.shields.io/badge/pnpm-workspace-orange?style=flat-square&logo=pnpm&logoColor=white" alt="pnpm"></a>
  <a href="https://astral.sh/uv"><img src="https://img.shields.io/badge/uv-fast_package_manager-00f5d4?style=flat-square" alt="uv"></a>
  <a href="https://github.com/astral-sh/ruff"><img src="https://img.shields.io/badge/Linter-Ruff-61dafb?style=flat-square" alt="Ruff"></a>
  <a href="https://github.com/unpod-ai/odyssey/blob/main/LICENSE"><img src="https://img.shields.io/badge/License-Apache_2.0-blue.svg?style=flat-square" alt="License"></a>
</p>

---

## 🧭 Overview

**Odyssey** is an end-to-end framework designed to solve the data loop for LLM agents. While observability platforms like Langfuse record traces to look at them, **Odyssey captures traces to fine-tune on them.**

It bridges the gap between active agent applications and deep training pipelines, organizing the flow from high-write real-time ingest to curated corpora, offline fine-tuning configurations, and rigorous evaluation.

---

## 🔄 Interactive Lifecycle & Flowchart

The diagram below outlines the full lifecycle of the Odyssey ecosystem. **Connections animate to show real-time data flow** across three distinct pillars:

<div align="center">
  <img src="docs/assets/pipeline.svg" width="100%" alt="Odyssey Lifecycle Flow">
</div>

### 🏛️ The Three Pillars

1. **CAPTURE & SERVE**: Active applications instrumented with `odyssey.init()` stream live agent traces. Traces land first on a durable, **Local Spool** to guarantee zero data loss on network blips. From there, they drain to the high-throughput, multi-tenant stdlib **Collector** (`:8787`), persisting as partitioned JSONL files served read-only via the **API** (`:8000`) and visualized inside a sleek Next.js **Dashboard** (`:3000`).
2. **PIPELINE & TRAIN**: Immutable raw traces enter a highly configurable **7-stage data preparation pipeline** (collection → cleaning → normalization → annotation → augmentation → validation → splitting) to produce high-density curated corpora. The **Soup Adapter** translates these datasets into valid configs (`soup.yaml`) targeting **Unsloth**, SFT, DPO, or GRPO, before exporting checkpoints to **Hugging Face** or S3-compatible object stores.
3. **SERVE & EVAL**: Checkpoints registered inside the **Model Registry** are served on high-performance engines like **Baseten** or **Cerebras** (or local vLLM / Ollama). Model completions files are collected and evaluated offline via the **Evaluation Harness** against benchmarks. Reports feed directly back into the API for fully closed-loop evaluation.

---

## 📂 Monorepo Layout

Click on any folder below to expand its technical specifications and deep dive into its exact role in the monorepo:

<details>
<summary>📦 <strong>packages/odyssey-core</strong> — <em>Capture Library</em></summary>
<br>

- **Path**: `packages/odyssey-core`
- **Role**: Pure stdlib Python library (no external dependencies) providing the instrumentation primitives.
- **Key Modules**:
  - `primitives.py`: The unified `JourneyEvent` schema.
  - `fold.py`: Live projection folding cumulative states.
  - `spool.py`: Local disk-backed spooling queue with redaction on-the-fly.
  - `integrations/`: Pre-packaged integrations for OpenAI, Anthropic, Gemini, Langchain, and LiveKit.
- **Run**: `cd packages/odyssey-core && uv sync --extra dev && bash scripts/run_tests.sh all`
</details>

<details>
<summary>🧱 <strong>packages/odyssey-schemas</strong> — <em>Wire Contract</em></summary>
<br>

- **Path**: `packages/odyssey-schemas`
- **Role**: Pydantic DTOs shared by the API and offline pipelines. The single source of truth generating `openapi.json` without pull-in from heavy packages like FastAPI or Torch.
- **Run**: `cd packages/odyssey-schemas && uv sync --extra dev && uv run pytest tests`
</details>

<details>
<summary>🚀 <strong>cli</strong> — <em>Unified CLI Entrypoint</em></summary>
<br>

- **Path**: `cli`
- **Role**: Owns the single `odyssey` entrypoint. Implements lazy command dispatch so importing never loads heavy libraries (like PyTorch or Transformers) unless explicitly dispatched.
- **Key Commands**: `odyssey spool`, `odyssey data`, `odyssey train`, `odyssey model`, `odyssey eval`, `odyssey api`, `odyssey doctor`
- **Run**: `cd cli && uv sync --extra dev && uv run odyssey --help`
</details>

<details>
<summary>⚡ <strong>services/collector</strong> — <em>Ingest Server</em></summary>
<br>

- **Path**: `services/collector`
- **Role**: High-speed, pure standard-library HTTP server (`:8787`) to accept compressed JSONL batches from capturing agents and stream them to disk partitions safely.
- **Run**: `cd services/collector && uv run odyssey-collector --data-dir ./collector-data`
</details>

<details>
<summary>🕸️ <strong>services/api</strong> — <em>Read API Server</em></summary>
<br>

- **Path**: `services/api`
- **Role**: FastAPI backend (`:8000`) exposing read-only routes for journeys, datasets, and models written by the collector and train pipelines.
- **Run**: `cd services/api && uv run uvicorn odyssey_api.main:app --host 127.0.0.1 --port 8000`
</details>

<details>
<summary>🖥️ <strong>apps/web</strong> — <em>Dashboard UI</em></summary>
<br>

- **Path**: `apps/web`
- **Role**: Next.js dashboard UI (`:3000`) pulling dataset, training run, and eval visualization directly from the API via the generated `@odyssey/sdk`.
- **Run**: `ODYSSEY_API_BASE_URL=http://127.0.0.1:8000 pnpm --filter @odyssey/web dev`
</details>

<details>
<summary>🎛️ <strong>data_preparation</strong> — <em>7-Stage Pipeline</em></summary>
<br>

- **Path**: `data_preparation`
- **Role**: Takes raw trace dumps and cleanses, normalizes (e.g. ChatML mapping), augments, and splits them into clean train/val/test splits.
- **Key Commands**: `odyssey data normalize`, `odyssey data build-corpus`
</details>

<details>
<summary>🧠 <strong>training</strong> — <em>Soup-cli Adapter</em></summary>
<br>

- **Path**: `training`
- **Role**: Config generator and model cards manager mapping curated corpora to perfect training files for [soup-cli](https://trysoup.dev) (supporting SFT, DPO, and GRPO fine-tuning).
- **Run**: `odyssey train sft-config --base <hf-id> --shard sft.jsonl --out soup.yaml`
</details>

<details>
<summary>🧪 <strong>evaluation</strong> — <em>Offline Scoring Harness</em></summary>
<br>

- **Path**: `evaluation`
- **Role**: Offline scoring harness. Never calls model APIs directly; instead, scores static completions files (JSONL) produced by any inference agent against frozen yaml benchmarks.
- **Run**: `odyssey eval run --benchmark evaluation/benchmarks/example-arithmetic.yaml --completions completions.jsonl`
</details>

<details>
<summary>⚙️ <strong>Other Components</strong> — <em>SDKs, Metadata registries, Specs</em></summary>
<br>

- `sdk/python` & `sdk/javascript`: Fully generated clients automatically kept in sync with the FastAPI schema via CI drift-checks.
- `datasets` & `models`: Lightweight metadata-only registries (no raw weight storage) holding metadata, config hashes, and model cards.
</details>

---

## ⚡ Quickstart

### Prerequisites & Setup
Odyssey requires **Python 3.12** strictly (upper-bounded at `<3.13` for compatibility with heavy downstream training wrappers).

We use `Taskfile` to drive simple monorepo setups. To install and configure the whole project:

```bash
task setup           # uv syncs all packages, extra packages, and Node dev deps
task check           # Runs linter, format checkers, type checks, and tests across the repo
task test            # Runs unit and integration tests only
```

### Running individual packages with CLI plugins:
Using the lazy-loaded `odyssey` CLI script, commands are dynamically discovered and loaded:

```bash
odyssey --help                                                                     # Show all mounted plugins
odyssey spool status --spool .odyssey                                              # Check status of local spool
odyssey data normalize --raw ./raw_exports --format openai_chat --out ./normalized # Normalize traces
odyssey doctor                                                                     # Diagnose plugin loading & boot timings
```

---

## 🏃 Running the Full Stack (Local Development)

Follow these steps to spin up the complete end-to-end trace loop locally:

### 1️⃣ Collector (Ingest Live Traces on Port 8787)
Starts the stdlib server to accept posts from active SDK agents:
```bash
cd services/collector
uv run odyssey-collector --data-dir ./collector-data
```
In your agent process, initialize Odyssey to send live traces to the collector:
```python
import odyssey
odyssey.init(sink=odyssey.HttpSink("http://127.0.0.1:8787"))
```

### 2️⃣ API (Serve Datasets and Runs on Port 8000)
Exposes the query interface over the written traces and pipeline registries:
```bash
cd services/api
uv run uvicorn odyssey_api.main:app --host 127.0.0.1 --port 8000
```
*Note: Run `./scripts/codegen.sh` to regenerate both Python and JavaScript SDK clients directly from the running FastAPI's openapi spec.*

### 3️⃣ Web Dashboard (Visualize Results on Port 3000)
Ensure you've built the local JavaScript SDK first, then spin up the Next.js frontend:
```bash
# Build the SDK from root
pnpm --filter @odyssey/sdk build

# Run the web UI
ODYSSEY_API_BASE_URL=http://127.0.0.1:8000 pnpm --filter @odyssey/web dev
```

### 4️⃣ Training & Fine-Tuning Pipeline
Translate registered corpora into standard `soup-cli` config files, ready to train on a GPU box:
```bash
# Generate SFT training configuration targeting Meta Llama-3.1
odyssey train sft-config --base meta-llama/Llama-3.1-8B-Instruct --shard sft.jsonl --out soup.yaml

# On your GPU machine: run the training job
soup train --config soup.yaml
```

### 5️⃣ Offline Evaluation
Score completed inference generations against benchmarks:
```bash
odyssey eval run --benchmark evaluation/benchmarks/example-arithmetic.yaml --completions completions.jsonl
```

---

## 📈 Monorepo Phase Checklist

Odyssey is built incrementally across key milestones. All completed steps have fully verified, tested code:

- [x] **Phase 0**: Code extraction of core library with history preserved under `packages/odyssey-core`
- [x] **Phase 1**: Workspace root, gitignore configuration, version pinnings, and ADR documentation
- [x] **Phase 2**: Single unified `cli/` console script utilizing lazy command dispatch plugins (ADR 0003)
- [x] **Phase 3**: Shared schema contract, FastAPI API server, auto OpenAPI generation, and Python client SDK
- [x] **Phase 4**: 7-Stage `data_preparation` pipeline & registry metadata layout
- [x] **Phase 5**: Training pipeline adapter (soup-cli bridge), Model registry schemas, and evaluation harness
- [x] **Phase 6**: Next.js dashboard UI, JavaScript/TypeScript SDK, and comprehensive workspace examples

---

## 📚 Complete Documentation Index

Our docs are organized by operational layers to help you find exactly what you need quickly:

| Category | Document | Description |
|---|---|---|
| **🚀 Start Here** | [Architecture Specs](docs/architecture.md) | High-level overview of capture & train pipelines |
| | [Component Guide](docs/COMPONENTS.md) | Detailed map of what every folder actually does |
| | [Build Scorecard](docs/WORKING.md) | The line-by-line completion status of features |
| | [Structure Guidelines](docs/STRUCTURE.md) | Monorepo layout rules and developer conventions |
| **📊 Data & Lifecycle** | [Journey Schema](docs/journey-schema.md) | The `JourneyEvent` wire format specified field-by-field |
| | [Data Contracts](docs/data-contracts.md) | Codegen pipeline keeping Python, JS, and APIs in sync |
| | [Model Lifecycle](docs/model-lifecycle.md) | The sequence turning active corpora into evaluations |
| **🛡️ Infrastructure** | [Env Variables](docs/environment-variables.md) | Full guide of every load-bearing environment variable |
| | [Service Runbooks](docs/runbooks/run-services.md) | Production checklist for API, Collector, and Web UI |
| **⚙️ Deep Dives** | [odyssey-core README](packages/odyssey-core/README.md) | Core capture, redaction, and integrations |
| | [data_preparation README](data_preparation/README.md) | Stages of filtering and data curation |
| | [training README](training/README.md) | Creating soup configurations for Unsloth & SFT/DPO |
| **🧠 Decisions (ADRs)** | [ADR 0001: Monorepo Layout](docs/adr/0001-monorepo-layout.md) | Monorepo layout rationale |
| | [ADR 0002: Large Artifacts](docs/adr/0002-artifacts-out-of-git.md) | Rationale for keeping datasets/weights out of git |
| | [ADR 0003: Lazy CLI Plugins](docs/adr/0003-single-cli-entrypoint.md) | Rationale behind unified `odyssey` command entry |
| | [ADR 0004: Event Sourcing](docs/adr/0004-capture-layer.md) | Decision to project state rather than store state |

---

## ⚖️ License

Apache-2.0. See `LICENSE` and `NOTICE` under `packages/odyssey-core`.
