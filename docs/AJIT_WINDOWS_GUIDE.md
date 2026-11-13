# Lethe: Windows Setup & Execution Guide for Ajit (HP Laptop)

Welcome to the Lethe team, Ajit! This guide gives you the exact, step-by-step instructions to run, verify, and demo Lethe on your HP Windows machine using PowerShell or Command Prompt.

---

## 1. Quick Prerequisites on Windows
Ensure you have **Python 3.10+** installed:
```powershell
python --version
# Output should be Python 3.10, 3.11, 3.12, or 3.13
```

---

## 2. Clone & Environment Setup

Open **PowerShell** (or Windows Terminal) and run:

```powershell
# 1. Clone repository
git clone https://github.com/vrajeshsulakhe/Lethe.git
cd Lethe

# 2. Create virtual environment
python -m venv .venv

# 3. Activate virtual environment
# In PowerShell:
.venv\Scripts\Activate.ps1
# (If PowerShell blocks scripts, run: Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass)
# Or in Command Prompt:
# .venv\Scripts\activate.bat

# 4. Set UTF-8 encoding for Windows terminal
$env:PYTHONIOENCODING="utf-8"

# 5. Install Lethe in editable mode with all dependencies
pip install -e .
pip install pytest
```

---

## 3. Running the Tests & Verifying Your Machine

Run the automated test suite to confirm everything is green:

```powershell
pytest tests\ -v
```
You should see all 11 tests pass in less than 1 second!

---

## 4. Running the Interactive Judge Demo

To run the complete 30-session interactive judge verification suite:

```powershell
lethe demo
```

This command will:
1. Ingest 30 realistic multi-session conversations into SQLite (`lethe.db`).
2. Simulate process death, restart, and assert 100% memory persistence.
3. Test the Bitemporal Update: Session 5 Pune $\rightarrow$ Session 18 Bengaluru.
4. Test the Contradiction Detection: User vegetarian statement vs. uploaded travel PDF.
5. Test the Honest Abstention: Querying an unrecorded passport number $\rightarrow$ clean "I don't know".
6. Test Planted False Memory Quarantine: Blocking malicious bank account injection.
7. Test Verifiable Cascading Deletion: Erasing phone number, purging derived summaries, and running the 5 Bemula attack probes.
8. Test Cross-Agent Handover: Simulating task handoff from Python agent to Claude Code/MCP.
9. Print the Token/Latency Benchmark table (showing $\ge 99\%$ token savings).
10. Print the Storage Choice Empirical Evidence Table.

---

## 5. Running Standalone Commands

### A. Inspect Stored Memories & Lineage DAG
```powershell
lethe inspect
```
Displays all records, their categories, trust levels, and lineage dependencies.

### B. Run the Token & Latency Benchmark
```powershell
lethe benchmark
```
Outputs the ASCII benchmark scoreboard comparing full history against Lethe.

### C. Wire Cursor / Claude Code / Antigravity / Codex & Print Credentials
```powershell
# Auto-wire all detected tools to ~/.lethe/lethe.db
lethe setup

# Print JSON / TOML / 1-line commands for manual UI setup
lethe creds
```

### D. Run the MCP Server Manually
```powershell
lethe mcp
```
Starts the Model Context Protocol server over `stdio` so Cursor, Claude Code, Antigravity, or Codex can connect to Lethe.

---

## 6. Windows-Specific Tips & FAQ

* **Global CLI on Windows (No venv activation needed):** Run `uv tool install .` (then `uv tool update-shell` and reopen PowerShell) or `pip install --user -e .` with Python's `Scripts` folder on `PATH`. If `lethe` is still not found on `PATH`, run `py -m lethe.cli ...` or `.\lethe.cmd ...` from the repo folder.
* **File Paths:** All Lethe modules use `pathlib.Path`, so you never need to worry about forward vs. backslashes (`\` vs `/`).
* **Database Locks:** SQLite in Lethe is configured with `PRAGMA busy_timeout = 30000;` and WAL mode, preventing Windows file-locking collisions.
* **No C++ Build Tools Needed:** Lethe uses a pure-Python cosine vector similarity fallback, meaning you do **not** need Microsoft Visual Studio C++ Build Tools installed.

