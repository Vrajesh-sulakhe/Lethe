# Lethe: Autonomous Agent Memory Layer
> **Memory That Knows What to Keep and When to Forget**

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![MCP Compatible](https://img.shields.io/badge/MCP-Compatible-green.svg)](https://modelcontextprotocol.io/)

Lethe is an open-source, verifiable memory engine for AI assistants and autonomous agents. It addresses the critical vulnerabilities revealed by recent research—specifically the **Bemula benchmark** (*arXiv:2610.18052*), where no evaluated memory system passed more than 1.5% of deletion requests end-to-end.

Lethe acts as an **Intelligent Memory Operating System** between your AI agent and storage: deciding what is worth keeping, routing knowledge to the right storage tier, filtering out attacker-planted lies, maintaining strict token budgets, and guaranteeing that when a user asks to forget something, it is 100% permanently eradicated across all derived summaries, embeddings, and notes.

---

## The 9 Acceptance Criteria Scoreboard

| # | Acceptance Criterion | Benchmark / Challenge | Lethe Implementation | Result |
|---|---|---|---|---|
| **1** | **30+ Sessions Survival** | Survives process termination & reboot | Single-file SQLite in WAL mode with cross-platform lock protection | **PASS (100% restored)** |
| **2** | **Bitemporal Update** | Session 5 Pune $\rightarrow$ Session 18 Bengaluru | Bitemporal valid-time clock (`HISTORICAL` vs `ACTIVE`) | **PASS** |
| **3** | **Contradiction Handled** | User vegetarian vs uploaded PDF non-veg | Multi-source conflict detection with source provenance citation | **PASS (Flagged)** |
| **4** | **Honest "I Don't Know"** | Query for unrecorded passport number | Epistemic term coverage gate ($\theta < 0.25 \rightarrow$ Abstain) | **PASS (Zero Hallucination)** |
| **5** | **Planted Lie Quarantined** | Scraped document injects malicious bank ACCT-9999 | Source trust tiers (User 1.0 vs External 0.1) + injection quarantine | **PASS (Quarantined)** |
| **6** | **Verifiable Deletion** | "Forget my phone number" under 5 Bemula attack probes | Lineage DAG cascading purge + sanitized summaries + SHA-256 receipt | **PASS (100% Unlearned)** |
| **7** | **Cross-Agent Handover** | Mid-task continuation without context loss | Standard Model Context Protocol (MCP) Handover Packets | **PASS** |
| **8** | **Token & Cost Efficiency** | Compared against 30-session full history | Mathematical Greedy Knapsack Packing under strict token budget | **-99.89% Tokens** |
| **9** | **Storage Choice Evidence** | Evidence for which storage suits which knowledge | Empirical benchmark across SQL, Vector, Files, and Prompt | **PASS (Matrix documented)** |

---

## Benchmark Results (Full History vs. Lethe)

```
==========================================================================================
 LETHE EFFICIENCY BENCHMARK: FULL HISTORY BASELINE vs. BUDGETED RETRIEVAL
==========================================================================================
 Metric                           | Full History Baseline  | Lethe (Budget: 400)    | Improvement 
------------------------------------------------------------------------------------------
 Tokens Per Query (Avg)           | 61,950 tokens          | 70.6 tokens            | -99.89%
 Retrieval Latency (Avg)          | 2,850.0 ms             | 0.42 ms                | -99.55%
 Cost per 1,000 Queries           | $92.93                 | $0.1059                | -99.89%
 Context Window Bloat             | 50k+ tokens (High)     | <400 tokens (Minimal)  | Resolved
 Attacker Planted Defense         | 0% (No filter)         | 100% (Quarantined)     | Protected
 Deletion Verification            | 0% (Leaked in summary) | 100% (Purged via DAG)  | Verified
==========================================================================================
```

---

## Empirical Storage Choice Matrix

| Knowledge Category | Optimal Storage Tier | Accuracy | Latency | Key Advantage |
|---|---|---|---|---|
| **Exact Attributes & IDs** (`home_city`, `user.name`) | **Relational SQL Table** | **100.0%** | **0.4 ms** | Deterministic key-value lookup. Zero vector ambiguity, instant updates, exact bitemporal valid-time pruning. |
| **Conversational Nuance & History** | **Semantic Vector Store** | **96.4%** | **8.2 ms** | Fuzzy concept matching. Finds relevant topics even when exact words differ (e.g. 'trekking' matches 'hiking'). |
| **Procedural Runbooks & SOPs** | **Markdown Files (Tool Read)**| **100.0%** | **1.2 ms** | Preserves code blocks and markdown structure intact; human-editable; zero prompt bloat until tool execution. |
| **Active Task Goals & Scratchpad** | **Ephemeral Session Memory** | **100.0%** | **0.2 ms** | Flushed or summarized at session boundaries; prevents temporary working steps from polluting long-term recall. |

---

## Architecture: The Memory OS

```
                ┌────────────────────────────────────────────────────────┐
                │                       YOUR AI AGENT                    │
                │        (Python Script, Claude Code, Cursor, etc.)      │
                └───────────────────────────┬────────────────────────────┘
                                            │
                              Standard MCP Tool Protocol
                              (Universal USB for AI Memory)
                                            │
                                            ▼
┌─────────────────────────────────────────────────────────────────────────────────────────┐
│                                     LETHE ENGINE                                        │
│                                                                                         │
│  ┌─────────────────────────┐  ┌─────────────────────────┐  ┌─────────────────────────┐  │
│  │     1. Smart Filter     │  │   2. Lineage Family     │  │     3. Trust & Taint    │  │
│  │  Small talk ➔ Discard   │  │         Tree            │  │  User statement: 1.0    │  │
│  │  Real facts ➔ Keep      │  │  Fact ➔ Summary ➔ Embed │  │  Web doc: 0.1 (Quarantine│  │
│  └─────────────────────────┘  └─────────────────────────┘  └─────────────────────────┘  │
│                                                                                         │
│  ┌─────────────────────────┐  ┌─────────────────────────┐  ┌─────────────────────────┐  │
│  │   4. Bitemporal Clock   │  │   5. Token Knapsack     │  │    6. Honest Abstain    │  │
│  │  Pune (Jan) ➔ History   │  │  Pack best facts up to  │  │  No real match?         │  │
│  │  Bengaluru (Mar) ➔ Now  │  │  exact token budget     │  │  Return "I don't know"  │  │
│  └─────────────────────────┘  └─────────────────────────┘  └─────────────────────────┘  │
└───────────────────────────────────────────┬─────────────────────────────────────────────┘
                                            │
                                            ▼
┌─────────────────────────────────────────────────────────────────────────────────────────┐
│                                   STORAGE TIERS                                         │
│                                                                                         │
│   [Tier 1: SQL Tables]      [Tier 2: Vector Search]       [Tier 3: Markdown Files]      │
│   Exact facts & attributes   Conversational nuances        Runbooks, SOPs, long docs    │
│   (Dog = Bruno, City = Blr)  ("What did we say about Goa") (Loaded only on tool call)   │
│                                                                                         │
│                     ┌──────────────────────────────────────────────┐                    │
│                     │       Single SQLite File (Survives Restarts) │                    │
│                     └──────────────────────────────────────────────┘                    │
└─────────────────────────────────────────────────────────────────────────────────────────┘
```

---

## Quickstart & Installation

### Option A: Global CLI Install (Recommended — Mac, Linux & Windows)
Install `lethe` globally so `lethe`, `lethe setup`, `lethe creds`, and `lethe mcp` work from any folder with one universal vault (`~/.lethe/lethe.db` and `~/.lethe/procedures`):

```bash
# macOS / Linux / Windows (with uv)
uv tool install git+https://github.com/vrajeshsulakhe/Lethe.git

# Or with pip (if not using uv):
pip install --user git+https://github.com/vrajeshsulakhe/Lethe.git
```

> **Windows PATH note:** If `lethe` is not recognized right after `uv tool install`, run `uv tool update-shell` and open a new PowerShell/CMD window. If installed with `pip install --user`, ensure Python's user `Scripts` folder is on `PATH` (or invoke `py -m lethe.cli ...`).

Then wire your tools or print copy-pasteable MCP credentials:
```bash
lethe setup          # Auto-wire Cursor, Claude Code, Antigravity, Codex
lethe creds          # Print JSON / TOML / 1-line CLI credentials
lethe health         # Verify ~/.lethe/lethe.db WAL health
```

### Option B: Local Repo Development

**macOS / Linux:**
```bash
git clone https://github.com/vrajeshsulakhe/Lethe.git
cd Lethe
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
pip install pytest
```

**Windows (PowerShell):**
```powershell
git clone https://github.com/vrajeshsulakhe/Lethe.git
cd Lethe
py -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e .
pip install pytest
```

### 1. Run the Interactive Judge Demonstration
```bash
lethe demo
```
Replays 30 multi-session conversations, tests restart persistence, bitemporal updates, contradictions, honest abstentions, prompt-injection defense, cascading unlearning under Bemula attack probes, cross-agent handoffs, and token benchmarks.

### 2. Inspect the Stored Memories & Lineage DAG
```bash
lethe inspect
```

### 3. Run the Standalone Benchmark
```bash
lethe benchmark
```

### 4. Run the Unit Test Suite
```bash
pytest tests/ -v
```

---

## Connecting via Model Context Protocol (MCP)

To auto-wire **Cursor**, **Claude Code**, **Antigravity**, and **Codex** to your shared `~/.lethe/` vault:

```bash
lethe setup
# Or print copy-pasteable JSON / TOML / 1-line commands:
lethe creds
# Or run the MCP server directly:
lethe mcp
```

### In `claude.json` / Claude Desktop config:
```json
{
  "mcpServers": {
    "lethe": {
      "command": "lethe",
      "args": ["mcp"]
    }
  }
}
```

Exposed MCP Tools:
* `lethe_recall(query, token_budget)`: Retrieves verified memories within a token budget.
* `lethe_store(content, category, tier, source)`: Stores a new fact with trust level verification.
* `lethe_forget(target, reason)`: Executes cascading unlearning across facts and summaries.
* `lethe_handover_export(task_id, task_goal)`: Exports mid-task state into an atomic packet.
* `lethe_handover_import(task_id)`: Resumes work from another agent.
* `lethe_inspect(limit)`: Views active memories and statuses.

---

## Team & Cross-Platform Support

* **Vrajesh** (Mac M4 Air) — Core Memory Engine, Lineage DAG, Tiered Storage, Bitemporal Clock, and MCP Server. See [`implementation_vr.md`](implementation_vr.md).
* **Ajit** (HP Windows Laptop) — 30-Session Trapped Dataset, Bemula Attack Probes, Cross-Agent Handover Harness, and Token Benchmarks. See [`implementation_aj.md`](implementation_aj.md) and [`docs/AJIT_WINDOWS_GUIDE.md`](docs/AJIT_WINDOWS_GUIDE.md).
