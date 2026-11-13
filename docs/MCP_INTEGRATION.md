# Lethe MCP Integration Guide

This guide provides copy-pasteable configuration snippets, zero-config setup commands, transport options (`stdio`, `sse`, and `streamable-http`), and path-update instructions for connecting **Claude Desktop**, **Gemini Desktop**, **OpenAI / ChatGPT Desktop**, **Cursor**, **Claude Code**, **Antigravity**, **Codex**, and **VS Code** to the shared Lethe Memory Operating System.

---

## 1. Quickest Setup: Automated Zero-Config Wiring

Lethe includes a built-in configuration engine (`lethe.platform.setup`) that detects installed AI CLI, IDE, and Desktop apps and merges the `lethe` server entry into their JSON or TOML configuration files without overwriting existing MCP servers or comments.

```bash
# 1. Inspect detected tools without modifying files (dry run)
python -m lethe.cli setup --check

# 2. Wire all detected tools using the current virtualenv Python executable
python -m lethe.cli setup

# 3. Or wire all tools using the environment-resolving wrapper (lethe.sh / lethe.cmd)
python -m lethe.cli setup --use-wrapper

# 4. Or target a single Desktop, IDE, or CLI tool
python -m lethe.cli setup --tool claude-desktop
python -m lethe.cli setup --tool gemini-desktop
python -m lethe.cli setup --tool openai-desktop
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

### 3.6 Claude Desktop App (`claude-desktop`)
**Config File Locations:**
* macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
* Windows (Installer): `%APPDATA%\Claude\claude_desktop_config.json`
* Windows (Microsoft Store MSIX): `%LOCALAPPDATA%\Packages\Claude_pzs8sxrjxfjjc\LocalCache\Roaming\Claude\claude_desktop_config.json`
* Linux: `~/.config/Claude/claude_desktop_config.json`

*(Note: Desktop GUI apps often launch with a minimal system `PATH`. Using the absolute path to `lethe.sh` / `lethe.cmd` or your Python binary via `lethe setup --tool claude-desktop` ensures zero spawn errors.)*

```json
{
  "mcpServers": {
    "lethe": {
      "command": "/ABS/PATH/TO/Lethe/lethe.sh",
      "args": [
        "mcp",
        "--db",
        "/Users/YOUR_USER/.lethe/lethe.db",
        "--procedures-dir",
        "/Users/YOUR_USER/.lethe/procedures"
      ],
      "env": {
        "PYTHONUNBUFFERED": "1",
        "PYTHONIOENCODING": "utf-8"
      }
    }
  }
}
```

### 3.7 Gemini Desktop & Gemini CLI (`gemini-desktop` / `gemini`)
**Config File Locations:**
* macOS Desktop: `~/Library/Application Support/Gemini/gemini_desktop_config.json` (or `mcp_config.json`)
* Windows Desktop: `%APPDATA%\Gemini\gemini_desktop_config.json` (or `%LOCALAPPDATA%\Google\Gemini\mcp_config.json`)
* Linux Desktop: `~/.config/Gemini/gemini_desktop_config.json`
* Global Shared Gemini Settings: `~/.gemini/settings.json` and `~/.gemini/config/mcp_config.json`

```json
{
  "mcpServers": {
    "lethe": {
      "command": "/ABS/PATH/TO/Lethe/lethe.sh",
      "args": [
        "mcp",
        "--db",
        "/Users/YOUR_USER/.lethe/lethe.db",
        "--procedures-dir",
        "/Users/YOUR_USER/.lethe/procedures"
      ],
      "env": {
        "PYTHONUNBUFFERED": "1",
        "PYTHONIOENCODING": "utf-8"
      }
    }
  }
}
```

### 3.8 OpenAI / ChatGPT Desktop & Codex Desktop (`openai-desktop` / `chatgpt`)
**Config File Locations:**
* macOS Desktop JSON: `~/Library/Application Support/ChatGPT/mcp.json` (or `~/Library/Application Support/OpenAI/ChatGPT/mcp.json`, `~/.chatgpt/mcp.json`)
* Windows Desktop JSON: `%APPDATA%\ChatGPT\mcp.json` (or `%APPDATA%\OpenAI\ChatGPT\mcp.json`)
* Linux Desktop JSON: `~/.config/ChatGPT/mcp.json`
* Codex Desktop / CLI Shared TOML: `~/.codex/config.toml` (or `~/Library/Application Support/Codex/config.toml`)

**For JSON-based OpenAI / ChatGPT Desktop (`mcp.json`):**
```json
{
  "mcpServers": {
    "lethe": {
      "command": "/ABS/PATH/TO/Lethe/lethe.sh",
      "args": [
        "mcp",
        "--db",
        "/Users/YOUR_USER/.lethe/lethe.db",
        "--procedures-dir",
        "/Users/YOUR_USER/.lethe/procedures"
      ],
      "env": {
        "PYTHONUNBUFFERED": "1",
        "PYTHONIOENCODING": "utf-8"
      }
    }
  }
}
```

**For TOML-based OpenAI Codex Desktop (`~/.codex/config.toml`) or Remote Connector (`streamable-http`):**
```toml
[mcp_servers.lethe]
command = "/ABS/PATH/TO/Lethe/lethe.sh"
args = ["mcp", "--db", "/Users/YOUR_USER/.lethe/lethe.db", "--procedures-dir", "/Users/YOUR_USER/.lethe/procedures"]

[mcp_servers.lethe.env]
PYTHONUNBUFFERED = "1"
PYTHONIOENCODING = "utf-8"
```
*(If connecting via ChatGPT Desktop Developer Mode custom HTTP MCP URL, run `lethe mcp --transport streamable-http --port 8000` and connect to `http://127.0.0.1:8000/mcp`.)*

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
   * **Claude Desktop / Gemini Desktop / OpenAI (ChatGPT) Desktop:** Quit and relaunch the desktop app (`Cmd+Q` on macOS / `Alt+F4` on Windows) so the updated MCP config is loaded.
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
| `lethe_context` | Single-call session boot returning global identity, recent memories, active handover, and unread sync delta within BPE budget. |
| `lethe_store` | Silently stores a memory through the Trust Gate, Bitemporal Clock, and Lineage DAG (auto-globals personal/identity categories and Desktop sessions). |
| `lethe_recall` | Retrieves verified memories within a strict BPE token budget (handles specific search, `"who am i"` identity queries, and `"recent memories"` browse queries). |
| `lethe_forget` | Executes cascading Lineage DAG unlearning across SQL, FTS5, and vectors and returns a SHA-256 receipt. |
| `lethe_handover_export` | Serializes mid-task state (`task_goal`, `active_memory_ids`, `epistemic_state`, `tool_trace`). |
| `lethe_handover_import` | Restores a handover packet created by another tool or session. |
| `lethe_inspect` | Lists newest stored memories filtered by `status`, `tier`, `scope`, `workspace_id`, and `limit`. |
| `lethe_procedures_list` | Lists Tier 3 Markdown runbooks in `procedures/` without polluting context. |
| `lethe_procedure_read` | Reads a Tier 3 Markdown runbook on demand. |
| `lethe_procedure_write` | Writes or updates a Tier 3 Markdown runbook on disk. |
| `lethe_flush_ephemeral` | Flushes Tier 4 session scratchpad memory and optionally promotes a summary to long-term memory. |

### Autonomous Zero-Command Workflows, Prompts & Client Rules

* **MCP Server Instructions & Prompts**: Lethe advertises autonomous memory instructions in MCP `initialize` plus 4 dynamic MCP Prompts (`session_boot`, `capture_memory`, `resume_handover`, `memory_checkpoint`) so Claude Desktop, Gemini Desktop, ChatGPT Desktop, Cursor, and Claude Code recall and store automatically without requiring manual `lethe_*` commands.
* **Desktop `cwd="/"` Safe Scoping**: Desktop apps launched at `/` automatically map to workspace `global_desktop` instead of an empty workspace ID, and personal/identity categories (`user_identity`, `user_profile`, `preferences`, `personal`, `bio`) default to `global` scope in trusted workspaces so memories shared in Antigravity/Cursor are immediately visible in Desktop apps.
* **Automated Client Rules & Hooks**:
  * `lethe setup --with-hooks` installs Claude Code `SessionStart`/`UserPromptSubmit` hooks plus autonomous memory rules in `~/.claude/CLAUDE.md`, `.cursor/rules/lethe-sync.mdc`, `~/.gemini/GEMINI.md`, and `~/.codex/AGENTS.md` (with `.bak` backups and bounded markers).
  * `lethe setup --with-rules` installs only the autonomous instruction rules without modifying hooks.
  * `lethe creds` includes a copy-pasteable Desktop Custom Instructions snippet for Claude, Gemini, and ChatGPT Desktop Projects.
