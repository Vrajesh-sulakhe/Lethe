# Lethe Platform: Live Cross-Tool Presentation Playbook

A step-by-step presentation guide for **Vrajesh & Ajit** to showcase Lethe as an autonomous cross-tool Memory Platform to mentors, evaluators, and judges.

---

## 1. The Opening Hook & The "Cake Metaphor" (30 Seconds)

> *"Today, developers don't use just one AI tool. We brainstorm in **Antigravity**, refactor code in **Claude Code CLI**, and edit UI components in **Cursor IDE**.*
> 
> *The problem? **Context amnesia and context-window choking**.*
> *Every time you switch tools, your context drops to zero. You have to re-explain your whole architecture.*
> 
> *Tools like Copilot try to fix this with 'compact context', which simply uses an LLM to mash 50,000 tokens into a lossy summary. As the recent **Bemula benchmark (arXiv:2610.18052)** showed, if you summarize a secret or prompt injection, you can **never** delete it—it's permanently poisoned.*
> 
> ***Think of a project's history as a 50-pound cake:** You can't eat it all at once or you'll choke. Other tools try to stuff the entire cake down the model's throat, or puree it into mush.*
> ***Lethe is the master chef:** it stores the entire cake in local SQLite, and whenever Claude, Cursor, or Antigravity is hungry, Lethe cuts **one clean, 400-token slice** with the exact facts needed—never overflowing the context window."*

---

## 2. Live Demo Script (Step-by-Step)

### Step 1: One-Command Auto-Integration (`lethe setup`)
Open your terminal in front of the judges and run:
```bash
python -m lethe.cli setup --check
```
* **Say to the judges**: *"Lethe is zero-config. It scans the developer's laptop, discovers Cursor, Claude Code, Antigravity, and VS Code, and wires our universal Model Context Protocol (MCP) memory bus into their configuration files."*

---

### Step 2: Live Real-Time Developer Dashboard (`lethe ui`)
Launch the dashboard:
```bash
python -m lethe.cli ui
```
* Opens the dark-mode dashboard at `http://127.0.0.1:8765`.
* **Point to the screen**:
  1. **The Cake vs. The Slice Meter**: Show **57,913 baseline tokens** vs. **75.9 delivered tokens** (**-99.87% token reduction**).
  2. **Connected Tools Badges**: Cursor, Claude Code, Antigravity, Codex.
  3. **Security & Epistemic Gate**: Point out the flagged conflict (`Vegetarian` vs `Chicken biryani PDF`) and quarantined prompt injection (`ACCT-9999-EVIL`).
  4. **Lineage DAG**: Point out the parent-child derivation and cryptographic deletion receipt.

---

### Step 3: Live Cross-Tool Handover (Claude Code $\rightarrow$ Cursor IDE)
Run the automated live cross-tool handover demonstration:
```bash
python -m lethe.platform.cross_tool_demo
```
* **Walk the judges through what just happened**:
  1. **Claude Code CLI (Tool 1)** audited an infrastructure bottleneck (Redis session exhaustion) and stored it into Lethe.
  2. Claude Code process was **terminated completely**.
  3. **Cursor IDE (Tool 2)** connected over MCP, imported the handover packet, and asked Lethe: *"What Redis issue was found during the audit?"*
  4. Lethe served a **34-token precision slice** in **7 milliseconds**.
  5. Cursor fixed the bug and committed the resolution back to Lethe's Lineage DAG.
  6. **Zero context was lost, zero human re-prompting required.**

---

### Step 4: Full 9-Criteria Benchmark Verification
Run the complete benchmark and 30-session evaluation suite:
```bash
python -m lethe.cli demo
```
* Shows all 9 judge acceptance criteria passing live:
  - Persistence across restarts
  - Bitemporal updates (Pune $\rightarrow$ Bengaluru)
  - Contradiction detection with source citations
  - Honest "I don't know" abstention (zero hallucination)
  - Planted injection quarantine
  - 100% Bemula 5-vector unlearning defense
  - Cross-agent handover
  - Token and cost savings benchmark
  - Empirical 4-tier storage choice matrix

---

## 3. Quick Reference for Judge Q&A

| Judge Question | Winning Answer |
| :--- | :--- |
| **"How is this different from Copilot / Claude compact context?"** | *"Copilot compact is a **lossy LLM summary inside a single walled garden** (VS Code). Lethe is a **cross-tool Memory OS**. Cursor, Claude Code, and Antigravity all share the same memory bus. Plus, Lethe uses tiered storage and a Lineage DAG—exact facts stay in SQL with zero hallucination, and deletions are verifiably permanent."* |
| **"Won't the memory bus overflow the model's context window?"** | *"Never. That's the **Knapsack Token Governor**. Regardless of whether Lethe holds 100k tokens of project history, it mathematically packs only the highest-scoring memories up to a strict budget (default 400 tokens). It serves only a slice of the cake."* |
| **"What is the Bemula benchmark and why does deletion matter?"** | *"The Bemula benchmark (arXiv:2610.18052) showed mainstream memory systems fail to unlearn sensitive data 98.5% of the time because derived summaries and embeddings retain the secret. Lethe uses a Lineage DAG with recursive SQL CTE to cascade deletions through all descendants and issue a SHA-256 audit receipt."* |
