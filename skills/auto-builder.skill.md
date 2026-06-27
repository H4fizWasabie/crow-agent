---
name: auto-builder
description: Autonomous app builder — takes frozen spec, builds full app via Builder model, tests, verifies, delivers
intent: development
triggers:
  - build it
  - build this
  - proceed with build
  - lets build it
  - start building
  - build the app
  - auto build
  - /build
parameters:
  spec_path:
    type: string
    required: false
    description: Path to frozen spec file. Defaults to builder-out/<app>/spec.md
  builder_model:
    type: string
    required: false
    description: OpenRouter model ID for Builder. Defaults from env BUILDER_MODEL
  output_dir:
    type: string
    required: false
    description: Base output dir. Defaults to ~/crow-builds/
---

# Auto Builder

Build a full app autonomously using a two-architect protocol (Crow + Builder model).

## Prerequisites

**Primary:** `~/crow-builds/.env` file with:
  ```
  OPENROUTER_API_KEY=<your-crow-api-key>
  BUILDER_MODEL=openrouter/owl-alpha
  ```

**Fallback:** `opencode` CLI installed (snap) — provides free models via Zen tier:
  - `opencode/deepseek-v4-flash-free`
  - `opencode/nemotron-3-ultra-free`
  - `opencode/north-mini-code-free`
  - `opencode/mimo-v2.5-free`
  (These need no API key — zero cost)

- `TELEGRAM_TOKEN` env var — for sending delivery notifications
- Frozen spec must exist at `~/crow-builds/<app-name>/spec.md`

## Authorization

This skill IS your authorization to make API calls.

When the skill triggers (via "build it" or `/build`), Crow reads `~/crow-builds/.env` and has **full authority** to call the Builder model via OpenRouter API. No further confirmation needed per call. Crow can spend API credits, install packages, write files, run servers, and deliver artifacts.

**Limits of authority:**
- Only within the current build session
- Only within `~/crow-builds/<app-name>/` directory
- Never expose the API key from `.env` into source code, logs, or Telegram messages
- Never commit or push to remote

## Protocol

This skill uses a **two-architect model**:

| Role | Who | Job |
|------|-----|-----|
| **Orchestrator** | Crow (you) | Execute tools, manage files, run server, verify pages, zip, report |
| **Builder** | External LLM (see backends below) | Design architecture, write code, solve problems, review rendered pages |

### Builder Backends

**Primary — OpenRouter API (providers.py):**
- Model: `openrouter/owl-alpha` (1M context, designed for agentic work)
- Crow orchestrates file-by-file: Builder generates text → Crow writes + tests
- Requires `OPENROUTER_API_KEY` in `.env`
- Full control over file locations and directory structure

**Fallback — opencode CLI (snap):**
- Models: any `opencode/*` free model (zero cost, no key needed)
- Hands-off mode: Crow passes spec to `opencode run --format json`, opencode writes files autonomously
- Crow monitors JSON output for status, errors, and file paths
- Less control but simpler; best for straightforward apps
- Activate when: OpenRouter fails after 3 retries, or `.env` has no key

**Choosing between them:**
1. Try OpenRouter first (if key exists)
2. On 3 failures → fall back to opencode with free model
3. If both fail → Phase 6 (Report)

## Workflow Phases

### Phase 0: Confirm & Announce

Before starting any build:

1. Read `~/crow-builds/.env` → extract `OPENROUTER_API_KEY` + `BUILDER_MODEL`
2. Verify frozen spec exists at `~/crow-builds/<app-name>/spec.md`
3. Verify `TELEGRAM_TOKEN` is set
4. Confirm with user: "Starting build for **[app name]**. Will report progress and deliver zip when done."
5. Send Telegram: "🏗️ Auto Builder started: **[app name]** — Phase 1: Scaffold"

### Phase 1: Scaffold

1. Send Builder the spec. Prompt:
   ```
   Spec: [full spec content]
   
   Design the project structure for this app.
   Choose the best stack (Python/JS/Go etc) based on the requirements.
   Output:
   1. Stack choice + reasoning (1 line)
   2. Directory/file tree
   3. For each file: a brief description of its purpose
   4. Key dependencies/packages
   
   I will create these files. Be concise and precise.
   ```

2. Create all directories with `mkdir -p`
3. Create empty/placeholder files with `Write` tool
4. Send Telegram: "✅ Scaffold done — [N] files created"
5. **On failure:** retry up to 3 times. If still fail, skip to Phase 6 (Report) with error.

### Phase 2: Build

Depends on which backend is active (see Protocol).

**Primary path (OpenRouter API) — Iterative file-by-file:**

1. Send Builder the current file list + context. Prompt:
   ```
   Project: [app-name]
   File: [filepath]
   Context: [what this file should do, any dependencies]
   
   Write the complete [filepath] file.
   Include imports, error handling, logging.
   Follow these conventions:
   - Python: PEP 8, type hints, f-strings
   - JS/TS: ES2022+, async/await
   - HTML: semantic elements, responsive
   - CSS: mobile-first, CSS vars
   ```
2. Save file with `Write` tool
3. Report result back to Builder
4. Repeat until Builder says build is complete
5. **Retry:** 3 attempts per file. If a file fails 3 times, flag it, skip, continue.

**Fallback path (opencode CLI) — Hands-off:**

1. `cd ~/crow-builds/<app-name>/`
2. Run: `opencode run '<app-name>: <spec summary>. Build the full app here.' --format json --model opencode/deepseek-v4-flash-free 2>&1`
3. Parse JSON output for:
   - `type: "tool_use"` — opencode is writing files, running commands
   - `type: "text"` — opencode's report/explanation
   - `type: "step_finish"` — check `reason` and `tokens`
4. On error → retry with different free model. Cycle: deepseek-v4-flash → nemotron-3-ultra → north-mini-code
5. After all steps done → verify files exist in `~/crow-builds/<app-name>/`

**After writing all files (both paths) — install dependencies:**

1. Create virtual env / node_modules as appropriate
2. Run package manager install
3. If install fails → send error to Builder → Builder fixes → retry
4. After successful install → send Telegram: "✅ Dependencies installed"

**Phase 2 complete** → send Telegram: "✅ Build complete — [N] files written"

### Phase 3: Test (Dev Server + Screenshots)

1. Start dev server in background:
   - Python: `python app.py` or `uvicorn ...`
   - Node: `npm run dev` or `npx serve .`
   - Go: `go run .`
2. Wait for server-ready log or health check
3. Use `browser_fetch` (Firecrawl) to verify each page/flow renders:
   - Homepage `/`
   - Each route/endpoint
   - Check for 200 status and meaningful content (not error pages)
4. Kill dev server
6. Send Telegram: "✅ Pages verified"
7. Send verified page content to Builder for review. Prompt:
   ```
   Here is the rendered page output from browser_fetch.

   Does the UI match the spec? List any issues:
   1. Layout problems
   2. Missing elements
   3. Visual bugs
   4. Functionality concerns
   ```
8. Builder identifies issues → Crow fixes → re-fetch → loop until Builder approves or 3 retries exhausted

### Phase 4: Verify

Build a verification checklist from the frozen spec:

1. Extract all UI flows from spec (e.g., "user can create account", "user can search")
2. For each flow, verify via browser_fetch + curl/API calls
3. Verify each item passes
4. Report pass/fail per item

**Grading:**
- All pass → ✅ Verified
- 1-2 minor fail → ⚠️ Mostly verified (flag in report)
- 3+ fail → ❌ Verify failed (include all failures in report)

### Phase 5: Package & Deliver

1. Create `~/crow-builds/<app-name>/` structure if not already:

```
app-name/
├── spec.md              # Frozen spec
├── src/                 # Application source code
├── pages/               # Page verification output
├── verify-checklist.md  # Verification results
├── report.md            # Final report
└── how-to-run.md        # Launch instructions
```

2. `how-to-run.md` must include:
   - Python version / Node version required
   - Environment variables needed
   - `pip install -r requirements.txt` or `npm install`
   - `python app.py` or equivalent
   - Any config files to create

3. Create zip (exclude dependencies):
   ```bash
   cd ~/crow-builds/<app-name>
   zip -r ../<app-name>.zip . -x "*/node_modules/*" -x "*/venv/*" -x "*/__pycache__/*" -x ".venv/*" -x "*/site-packages/*" -x "../.env"
   ```

4. Send to Telegram:
   - Zip file as document
   - Report summary text:
   ```
   🏗️ Auto Builder — Complete
   
   App: [app-name]
   Stack: [chosen stack]
   
   📁 [N] files, [N] lines of code
   ✅ [N] pages verified
   ✅ Verified: [pass/fail]
   
   How to run: [summary from how-to-run.md]
   ```

5. Cleanup: zip file (keep the build dir)

**On delivery failure** (Telegram down, file too large): Save zip at known path, send Telegram message with path + instructions to download.

### Phase 6: Report (on failure)

If any phase fails after 3 retries:

1. Note which phase and what failed
2. Save partial build artifacts
3. Send Telegram:
   ```
   ⚠️ Auto Builder — Partial Report
   
   App: [app-name]
   Failed at: Phase [N]
   Error: [error description]
   
   Partial artifacts saved at: ~/crow-builds/<app-name>/
   
   To resume: fix the issue and type "resume build"
   ```

## Resume Build

If user says "resume build":

1. Read `~/crow-builds/<app-name>/state.json` for last completed phase
2. Resume from next phase
3. Do NOT redo completed phases

## State Persistence

After each phase completes, write to `~/crow-builds/<app-name>/state.json`:

```json
{
  "app_name": "...",
  "phases": {
    "scaffold": "done",
    "build": "done",
    "test": "done",
    "verify": "in_progress",
    "package": "pending"
  },
  "error": null
}
```

## Telegram Updates

Send Telegram message at each phase transition:
- Phase start → "🏗️ Starting Phase [N]: [name]"
- Phase success → "✅ [name] complete"
- Phase retry → "🔄 Retry [N/3]: [name]"
- Phase fail → "⚠️ [name] failed after 3 retries"
- Complete → "🏗️ Auto Builder — Complete" with report

## Safety

- This skill has API spending authority — treat it as privileged
- Never expose `OPENROUTER_API_KEY` from `.env` in generated code, logs, or Telegram
- When using opencode fallback: opencode may write files anywhere in CWD. Always `cd` into `~/crow-builds/<app-name>/` before running.
- Never commit or push code to any git remote
- Never install untrusted packages without checking
- Never expose ports to `0.0.0.0` unless spec explicitly requires it
- Always kill dev server after testing
- Keep `~/crow-builds/.env` outside the zip artifact


## Usage Log
- [2026-06-19 05:27] outcome=not used
- [2026-06-19 23:55] outcome=not used
- [2026-06-20 03:16] outcome=not used
- [2026-06-20 06:20] outcome=not used
- [2026-06-20 09:22] outcome=not used
- [2026-06-20 09:28] outcome=not used
- [2026-06-20 09:29] outcome=not used
- [2026-06-20 09:32] outcome=not used
- [2026-06-20 09:35] outcome=not used
- [2026-06-20 09:39] outcome=not used
- [2026-06-20 09:41] outcome=not used
- [2026-06-20 09:46] outcome=not used
- [2026-06-20 09:48] outcome=not used
- [2026-06-20 10:04] outcome=not used
- [2026-06-20 10:31] outcome=not used
- [2026-06-20 10:36] outcome=not used
- [2026-06-20 10:47] outcome=not used
- [2026-06-20 10:53] outcome=not used
- [2026-06-20 11:02] outcome=not used
- [2026-06-20 11:09] outcome=not used
- [2026-06-21 04:30] outcome=not used
- [2026-06-21 04:45] outcome=not used
- [2026-06-21 04:55] outcome=not used
- [2026-06-21 04:55] outcome=not used
- [2026-06-21 04:55] outcome=not used
- [2026-06-21 05:09] outcome=not used
