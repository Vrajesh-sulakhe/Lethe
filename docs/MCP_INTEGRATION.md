# Lethe MCP Integration Guide

This guide provides copy-pasteable configuration snippets, zero-config setup commands, transport options (`stdio`, `sse`, and `streamable-http`), and path-update instructions for connecting **Cursor**, **Claude Code**, **Antigravity**, **Codex**, and **VS Code** to the shared Lethe Memory Operating System.

---

## 1. Quickest Setup: Automated Zero-Config Wiring

Lethe includes a built-in configuration engine (`lethe.platform.setup`) that detects installed AI tools and merges the `lethe` server entry into their JSON or TOML configuration files without overwriting existing MCP servers or comments.

```bash
# 1. Inspect detected tools without modifying files (dry run)
python -m lethe.cli setup --check

# 2. Wire all detected tools using the current virtualenv Python executable
python -m lethe.cli setup

# 3. Or wire all tools using the environment-resolving wrapper (lethe.sh / lethe.cmd)
python -m lethe.cli setup --use-wrapper

# 4. Or target a single tool
python -m lethe.cli setup --tool cursor
python -m lethe.cli setup --tool claude
python -m lethe.cli setup --tool antigravity
python -m lethe.cli setup --tool codex
python -m lethe.cli setup --tool vscode
```

---

## 2. Wrapper Scripts (`lethe.sh` & `lethe.cmd`)

To prevent `"interpreter not found"` errors when virtual environments are recreated or tools launch with a stripped `PATH`, use the repository wrapper scripts:

* **macOS / Linux:** `/abs/path/to/Lethe/lethe.sh`
* **Windows:** `C:\abs\path\to\Lethe\lethe.cmd`

Both scripts automatically resolve the Python interpreter in priority order:
1. `$LETHE_PYTHON` (if explicitly set)
2. `<repo>/.venv`
3. `<repo>/venv`
4. `python3` / `python` on system `PATH`

Verify pre-flight database and procedure directory health at any time:
```bash
./lethe.sh health
# Or run the MCP pre-flight check directly:
./lethe.sh mcp --check-only
```

---

## 3. Tool-Specific Configuration Files (Copy-Pasteable)

Replace `/ABS/PATH/TO/Lethe` (or `C:\\ABS\\PATH\\TO\\Lethe` on Windows) with your local repository path.

### 3.1 Cursor IDE
**Config File Locations:**
* Workspace: `<workspace>/.cursor/mcp.json`
* Global: `~/.cursor/mcp.json`

```json
{
  "mcpServers": {
    "lethe": {
      "command": "/ABS/PATH/TO/Lethe/lethe.sh",
      "args": [
        "mcp",
        "--db",
        "/ABS/PATH/TO/Lethe/lethe.db",
        "--procedures-dir",
        "/ABS/PATH/TO/Lethe/procedures"
      ],
      "env": {
        "PYTHONUNBUFFERED": "1"
      }
    }
  }
}
```

### 3.2 Claude Code CLI
**Config File Locations:**
* Global: `~/.claude.json` or `~/.claude/mcp.json`
* Workspace: `<workspace>/.mcp.json`

```json
{
  "mcpServers": {
    "lethe": {
      "command": "/ABS/PATH/TO/Lethe/lethe.sh",
      "args": [
        "mcp",
        "--db",
        "/ABS/PATH/TO/Lethe/lethe.db",
        "--procedures-dir",
        "/ABS/PATH/TO/Lethe/procedures"
      ],
      "env": {
        "PYTHONUNBUFFERED": "1"
      }
    }
  }
}
```

### 3.3 Antigravity (Gemini Agent)
**Config File Locations:**
* Global: `~/.gemini/antigravity/mcp_config.json`
* Workspace: `<workspace>/.agent/mcp.json`

```json
{
  "mcpServers": {
    "lethe": {
      "command": "/ABS/PATH/TO/Lethe/lethe.sh",
      "args": [
        "mcp",
        "--db",
        "/ABS/PATH/TO/Lethe/lethe.db",
        "--procedures-dir",
        "/ABS/PATH/TO/Lethe/procedures"
      ],
      "env": {
        "PYTHONUNBUFFERED": "1"
      }
    }
  }
}
```

### 3.4 OpenAI Codex CLI (`config.toml`)
**Config File Locations:**
* Global: `~/.codex/config.toml`
* Workspace: `<workspace>/.codex/config.toml`

```toml
[mcp_servers.lethe]
command = "/ABS/PATH/TO/Lethe/lethe.sh"
args = ["mcp", "--db", "/ABS/PATH/TO/Lethe/lethe.db", "--procedures-dir", "/ABS/PATH/TO/Lethe/procedures"]

[mcp_servers.lethe.env]
PYTHONUNBUFFERED = "1"
```

### 3.5 VS Code (GitHub Copilot MCP / Cline / Roo Code)
**Config File Locations:**
* Workspace: `<workspace>/.vscode/mcp.json`
* macOS Cline Global: `~/Library/Application Support/Code/User/globalStorage/saoudrizwan.claude-dev/settings/cline_mcp_settings.json`
* Windows Cline Global: `%APPDATA%\Code\User\globalStorage\saoudrizwan.claude-dev\settings\cline_mcp_settings.json`

```json
{
  "mcpServers": {
    "lethe": {
      "command": "/ABS/PATH/TO/Lethe/lethe.sh",
      "args": [
        "mcp",
        "--db",
        "/ABS/PATH/TO/Lethe/lethe.db",
        "--procedures-dir",
        "/ABS/PATH/TO/Lethe/procedures"
      ],
      "env": {
        "PYTHONUNBUFFERED": "1"
      }
    }
  }
}
```

---

## 4. Updating Configurations When Paths Change

If you move the `Lethe` repository directory, relocate `lethe.db`, or switch Python virtual environments:

1. **Automated Re-Wiring (Recommended):**
   From the new directory, run:
   ```bash
   ./lethe.sh setup --db /new/path/to/lethe.db --procedures-dir /new/path/to/procedures
   ```
   Lethe parses each existing `.json` and `.toml` file in-place, updates only the `lethe` entry (`command`, `args`, `env`), and preserves all other configured MCP servers.

2. **Verify the Updated Wiring:**
   ```bash
   ./lethe.sh setup --check
   ./lethe.sh health
   ```

3. **Reload Your AI Tool:**
   * **Cursor:** Open Command Palette (`Cmd+Shift+P` / `Ctrl+Shift+P`) $\rightarrow$ `MCP: Reload Servers`.
   * **Claude Code:** Run `/mcp` in the interactive CLI to confirm `lethe` is connected.
   * **Antigravity / Codex / VS Code:** Restart the agent session or reload the window.

---

## 5. Remote / Container Deployment (`sse` and `streamable-http`)

When running Lethe as a shared daemon across Docker containers, remote devboxes, or cloud IDEs:

```bash
# Start MCP Server over SSE on port 8000
./lethe.sh mcp --transport sse --host 0.0.0.0 --port 8000

# Or start over Streamable HTTP (/mcp endpoint)
./lethe.sh mcp --transport streamable-http --host 0.0.0.0 --port 8000
```

### Health & Telemetry Endpoints (SSE / Streamable HTTP & Dashboard)
When running in HTTP/SSE mode (`lethe mcp --transport sse`) or Dashboard mode (`lethe ui`), Lethe exposes live JSON probes:
* `GET http://127.0.0.1:8000/health` — Returns SQLite WAL status, `PRAGMA quick_check` integrity, schema version (`PRAGMA user_version`), record counts, and probe latency in milliseconds.
* `GET http://127.0.0.1:8000/stats` — Returns token savings metrics, tier distribution, status breakdown, and health summary.
* MCP Tool `lethe_health()` — Callable directly by any connected agent over `stdio`, `sse`, or `streamable-http`.

---

## 6. Exposed MCP Tools Reference

| Tool Name | Purpose |
| :--- | :--- |
| `lethe_health` | Returns SQLite WAL health, schema version, integrity check, and storage counts. |
| `lethe_store` | Stores a memory through the Trust Gate, Bitemporal Clock, and Lineage DAG. |
| `lethe_recall` | Retrieves verified memories within a strict BPE token budget using greedy knapsack packing. |
| `lethe_forget` | Executes cascading Lineage DAG unlearning across SQL & vectors and returns a SHA-256 receipt. |
| `lethe_handover_export` | Serializes mid-task state (`task_goal`, `active_memory_ids`, `epistemic_state`, `tool_trace`). |
| `lethe_handover_import` | Restores a handover packet created by another tool or session. |
| `lethe_inspect` | Lists stored memories filtered by `status`, `tier`, `workspace_id`, and `limit`. |
| `lethe_procedures_list` | Lists Tier 3 Markdown runbooks in `procedures/` without polluting context. |
| `lethe_procedure_read` | Reads a Tier 3 Markdown runbook on demand. |
| `lethe_procedure_write` | Writes or updates a Tier 3 Markdown runbook on disk. |
| `lethe_flush_ephemeral` | Flushes Tier 4 session scratchpad memory and optionally promotes a summary to long-term memory. |
