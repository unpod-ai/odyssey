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
  <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1000 440" width="100%" style="background: #0B0F19; border-radius: 12px; font-family: system-ui, -apple-system, sans-serif; box-shadow: 0 10px 30px rgba(0,0,0,0.5); border: 1px solid #1E293B;">
    <defs>
      <!-- Gradients -->
      <linearGradient id="grad-capture" x1="0%" y1="0%" x2="100%" y2="100%">
        <stop offset="0%" stop-color="#8338EC"/>
        <stop offset="100%" stop-color="#3A86FF"/>
      </linearGradient>
      <linearGradient id="grad-train" x1="0%" y1="0%" x2="100%" y2="100%">
        <stop offset="0%" stop-color="#3A86FF"/>
        <stop offset="100%" stop-color="#00F5D4"/>
      </linearGradient>
      <linearGradient id="grad-eval" x1="0%" y1="0%" x2="100%" y2="100%">
        <stop offset="0%" stop-color="#00F5D4"/>
        <stop offset="100%" stop-color="#FF006E"/>
      </linearGradient>
      <!-- Glow Filters -->
      <filter id="glow-capture" x="-20%" y="-20%" width="140%" height="140%">
        <feDropShadow dx="0" dy="4" stdDeviation="6" flood-color="#8338EC" flood-opacity="0.4"/>
      </filter>
      <filter id="glow-train" x="-20%" y="-20%" width="140%" height="140%">
        <feDropShadow dx="0" dy="4" stdDeviation="6" flood-color="#3A86FF" flood-opacity="0.4"/>
      </filter>
      <filter id="glow-eval" x="-20%" y="-20%" width="140%" height="140%">
        <feDropShadow dx="0" dy="4" stdDeviation="6" flood-color="#FF006E" flood-opacity="0.4"/>
      </filter>
    </defs>

    <style>
      .title { font-weight: 800; font-size: 14px; letter-spacing: 1px; fill: #E2E8F0; }
      .subtitle { font-size: 11px; fill: #94A3B8; }
      .node-text { font-weight: 600; font-size: 12px; fill: #F8FAFC; }
      .node-subtext { font-size: 10px; fill: #94A3B8; }
      
      .panel { fill: #0F172A; stroke: #1F2937; stroke-width: 1.5; rx: 12px; }
      .panel-capture { stroke: #8338EC; stroke-opacity: 0.4; }
      .panel-train { stroke: #3A86FF; stroke-opacity: 0.4; }
      .panel-eval { stroke: #FF006E; stroke-opacity: 0.4; }

      .node { rx: 8px; fill: #1E293B; stroke: #334155; stroke-width: 1.5; transition: all 0.3s ease; }
      .node:hover { fill: #334155; stroke-width: 2; cursor: pointer; }
      
      .node-capture { stroke: url(#grad-capture); }
      .node-train { stroke: url(#grad-train); }
      .node-eval { stroke: url(#grad-eval); }

      /* Animated Flows */
      .flow-line { fill: none; stroke-width: 2.5; stroke-linecap: round; }
      .flow-capture { stroke: #8338EC; stroke-dasharray: 8, 8; animation: dash 1.5s linear infinite; }
      .flow-train { stroke: #3A86FF; stroke-dasharray: 8, 8; animation: dash 1.5s linear infinite; }
      .flow-eval { stroke: #FF006E; stroke-dasharray: 8, 8; animation: dash 1.5s linear infinite; }
      
      /* Cross-pipeline loop (back-feeding reports to api) */
      .flow-loop { stroke: #00F5D4; stroke-dasharray: 8, 8; animation: dash-reverse 2s linear infinite; }

      @keyframes dash {
        to { stroke-dashoffset: -32; }
      }
      @keyframes dash-reverse {
        to { stroke-dashoffset: 32; }
      }

      /* Pulse active indicators */
      .pulse-dot { animation: pulse 2s infinite; }
      @keyframes pulse {
        0% { r: 3.5px; opacity: 1; }
        50% { r: 7px; opacity: 0.4; }
        100% { r: 3.5px; opacity: 1; }
      }
    </style>

    <!-- Panel 1: CAPTURE -->
    <rect x="20" y="20" width="290" height="400" class="panel panel-capture" />
    <text x="40" y="50" class="title">01. CAPTURE &amp; SERVE (Live)</text>
    <text x="40" y="68" class="subtitle">Agent Traces In &amp; Real-time APIs</text>

    <!-- Panel 2: TRAIN -->
    <rect x="350" y="20" width="300" height="400" class="panel panel-train" />
    <text x="370" y="50" class="title">02. PIPELINE &amp; TRAIN (Batch)</text>
    <text x="370" y="68" class="subtitle">7-Stage Prep &amp; Soup Finetuning</text>

    <!-- Panel 3: SERVE & EVAL -->
    <rect x="690" y="20" width="290" height="400" class="panel panel-eval" />
    <text x="710" y="50" class="title">03. SERVE &amp; EVAL (Offline)</text>
    <text x="710" y="68" class="subtitle">Baseten, Cerebras &amp; Scoring</text>

    <!-- ================== NODES: CAPTURE ================== -->
    <!-- App / LiveKit / SDKs Source -->
    <g filter="url(#glow-capture)">
      <rect x="40" y="95" width="250" height="55" class="node node-capture" />
      <text x="55" y="118" class="node-text">Live Sources &amp; SDKs</text>
      <text x="55" y="135" class="node-subtext">Langchain • Langfuse • LiveKit • OpenAI</text>
    </g>

    <!-- Local Spool -->
    <g>
      <rect x="40" y="180" width="250" height="50" class="node" />
      <text x="55" y="203" class="node-text">Local Spool (Core)</text>
      <text x="55" y="218" class="node-subtext">Append-only local JSONL buffer</text>
    </g>

    <!-- Collector -->
    <g>
      <rect x="40" y="260" width="250" height="50" class="node" />
      <text x="55" y="283" class="node-text">Collector (:8787)</text>
      <text x="55" y="298" class="node-subtext">Fast stdlib multi-tenant ingest</text>
    </g>

    <!-- API / SDKs / Web UI -->
    <g>
      <rect x="40" y="340" width="250" height="55" class="node" />
      <text x="55" y="363" class="node-text">API (:8000) &amp; Web UI (:3000)</text>
      <text x="55" y="380" class="node-subtext">FastAPI, Next.js, JS/Python SDKs</text>
    </g>

    <!-- ================== NODES: TRAIN ================== -->
    <!-- Raw Traces / Filesystem -->
    <g>
      <rect x="375" y="95" width="250" height="50" class="node" />
      <text x="390" y="118" class="node-text">Immutable Raw Traces</text>
      <text x="390" y="133" class="node-subtext">Date-partitioned file repository</text>
    </g>

    <!-- Data Prep -->
    <g filter="url(#glow-train)">
      <rect x="375" y="175" width="250" height="55" class="node node-train" />
      <text x="390" y="198" class="node-text">7-Stage Data Prep</text>
      <text x="390" y="215" class="node-subtext">Clean • Normalize • Augment • Split</text>
    </g>

    <!-- Soup Trainer Config -->
    <g>
      <rect x="375" y="260" width="250" height="50" class="node" />
      <text x="390" y="283" class="node-text">Soup Config &amp; CLI</text>
      <text x="390" y="298" class="node-subtext">Unsloth • SFT • DPO • GRPO configs</text>
    </g>

    <!-- Hugging Face / Checkpoint Upload -->
    <g>
      <rect x="375" y="340" width="250" height="50" class="node" />
      <text x="390" y="363" class="node-text">Hugging Face / Object Store</text>
      <text x="390" y="378" class="node-subtext">Upload checkpoint manifests &amp; bytes</text>
    </g>

    <!-- ================== NODES: INFERENCE ================== -->
    <!-- Model Registry -->
    <g>
      <rect x="710" y="95" width="250" height="50" class="node" />
      <text x="725" y="118" class="node-text">Model Registry</text>
      <text x="725" y="133" class="node-subtext">Track configs, metrics, metadata</text>
    </g>

    <!-- Inference Serving -->
    <g filter="url(#glow-eval)">
      <rect x="710" y="175" width="250" height="55" class="node node-eval" />
      <text x="725" y="198" class="node-text">Inference Servers</text>
      <text x="725" y="215" class="node-subtext">Baseten • Cerebras • vLLM • Ollama</text>
    </g>

    <!-- Completions -->
    <g>
      <rect x="710" y="260" width="250" height="50" class="node" />
      <text x="725" y="283" class="node-text">Model Completions</text>
      <text x="725" y="298" class="node-subtext">Output responses as JSONL rows</text>
    </g>

    <!-- Offline Eval Harness -->
    <g>
      <rect x="710" y="340" width="250" height="50" class="node" />
      <text x="725" y="363" class="node-text">Offline Eval Harness</text>
      <text x="725" y="378" class="node-subtext">Score benchmarks &amp; feed back to API</text>
    </g>

    <!-- ================== FLOW LINES & INTERCONNECTIONS ================== -->
    <!-- Col 1 Flow -->
    <path d="M 165,150 L 165,180" class="flow-line flow-capture" />
    <path d="M 165,230 L 165,260" class="flow-line flow-capture" />
    <path d="M 165,310 L 165,340" class="flow-line flow-capture" />

    <!-- Col 2 Flow -->
    <path d="M 500,145 L 500,175" class="flow-line flow-train" />
    <path d="M 500,230 L 500,260" class="flow-line flow-train" />
    <path d="M 500,310 L 500,340" class="flow-line flow-train" />

    <!-- Col 3 Flow -->
    <path d="M 835,145 L 835,175" class="flow-line flow-eval" />
    <path d="M 835,230 L 835,260" class="flow-line flow-eval" />
    <path d="M 835,310 L 835,340" class="flow-line flow-eval" />

    <!-- Cross Column Connections -->
    <!-- Collector/FileStore (Col 1) to Raw Traces (Col 2) -->
    <path d="M 290,285 L 330,285 Q 350,285 350,120 L 375,120" class="flow-line flow-train" />
    
    <!-- HF/Object Store (Col 2) to Model Registry (Col 3) -->
    <path d="M 625,365 L 665,365 Q 685,365 685,120 L 710,120" class="flow-line flow-eval" />
    
    <!-- Eval Harness (Col 3) back to API (Col 1) -->
    <path d="M 710,365 Q 670,365 670,410 L 330,410 Q 290,410 290,365" class="flow-line flow-loop" />

    <!-- Glowing pulse indicators -->
    <circle cx="165" cy="165" r="4" fill="#8338EC" class="pulse-dot" />
    <circle cx="500" cy="160" r="4" fill="#3A86FF" class="pulse-dot" />
    <circle cx="835" cy="160" r="4" fill="#FF006E" class="pulse-dot" />
  </svg>
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
