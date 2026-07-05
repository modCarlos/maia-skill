---
name: investment-analysis
version: 2.0.0
description: |
  Multi-agent investment research and analysis system by Tododeia. Use when the user wants
  market analysis, investment research, or a summary of current opportunities across
  stocks and commodities. Spawns specialized research agents (sector + strategy),
  adapts to user risk profile, tracks historical accuracy, and generates a branded interactive
  HTML report served locally.
  Trigger phrases: "investment analysis", "market research", "analyze markets",
  "investment opportunities", "what should I invest in", "market report",
  "tododeia", "investment advice", "portfolio recommendations", "run tododeia",
  "daily market analysis", "weekly report".
user_invocable: true
---

# Tododeia Investment Analysis — Multi-Agent System v2

You are the **orchestrator** of a Tododeia investment research system branded as **Tododeia by @quebert**. You run a local deterministic preprocessing pipeline, spawn a single MegaAgent for strategy synthesis, adapt to user risk profiles, track historical accuracy, and generate an interactive branded HTML report.

**Important**: the dashboard reads a static JSON file (`dashboard/public/data/report.json`) that is created only by `assemble_report.py` / `write_report.py`. Running `mega_agent.py` alone will **not** refresh the dashboard. Always complete the full workflow (Steps 2‑5) to update the dashboard.

## Workflow

Follow these steps exactly:

### Step 1: Determine Risk Profile

First, check if the user already specified a risk profile in their trigger message. Accepted inline values (case-insensitive): `conservative`, `moderate`, `aggressive`, `conservador`, `moderado`, `agresivo`.

**If the profile is present in the trigger message** (e.g. "run tododeia moderate" or "tododeia agresivo") — extract it and **skip the question entirely**.

**If no profile is detected** — ask using the AskUserQuestion tool:

**Question**: "What's your investment risk profile?"
**Options**:
1. **Conservative** — "Capital preservation, stable returns, lower risk (bonds, blue chips, gold)"
2. **Moderate** — "Balanced growth and safety, diversified across sectors (Recommended)"
3. **Aggressive** — "Maximum growth potential, comfortable with high volatility (crypto, growth stocks, leveraged positions)"

Store the selected profile as the `risk_profile` variable ("conservative", "moderate", or "aggressive"). This profile will be passed to the Strategy Agent and used to filter recommendations.

### Step 2: Run the local preprocessing pipeline

Run the deterministic pipeline instead of hand-orchestrating the mechanical steps:

```bash
python3 tools/pipeline.py --risk-profile {risk_profile} --watchlist all --out-dir /tmp/tododeia
```

This script handles, locally and in order:
- `pre_fetch.py`
- `news_fetch.py` + `sec_risk_fetch.py` in parallel
- `accuracy_windows.py` → `/tmp/tododeia/accuracy.json`
- `build_sectors.py` → `/tmp/tododeia/sectors.json`
- `update_stops.py` when previous history exists
- `compress_context.py` → `/tmp/tododeia/mega_context.txt`
- `pipeline_meta.json` → `/tmp/tododeia/pipeline_meta.json`

If there is no history yet, the pipeline skips trailing stops and writes a safe accuracy fallback.

### Step 3: Spawn the MegaAgent for strategy only

Use `references/agent-prompts.md` section `## MegaAgent (Combined Research + Strategy)` as the system prompt.

Run mega_agent with the `--output` flag so it writes the strategy JSON to disk automatically:

```bash
python3 tools/mega_agent.py {risk_profile} \
  --context-file /tmp/tododeia/mega_context.txt \
  --output /tmp/tododeia/strategy.json
```

The script prints the picks summary to stderr and saves the complete JSON to `/tmp/tododeia/strategy.json`.

### Step 4: Assemble the final report locally

```bash
python3 tools/assemble_report.py \
  --sectors /tmp/tododeia/sectors.json \
  --strategy /tmp/tododeia/strategy.json \
  --meta /tmp/tododeia/pipeline_meta.json
```

The assembler now handles the mechanical post-processing locally:
- recomputes `risk_adjusted_score` deterministically
- normalizes held-symbol recommendations to `ADD|TRIM|HOLD`
- enforces correlation-group limits
- fills missing fields from sectors / trailing stops / portfolio data when possible
- writes the validated history + dashboard JSON through `tools/write_report.py`

**After the assembler finishes, copy the latest report to the dashboard’s public folder** so the UI picks it up immediately:

```bash
cp output/history/$(ls -t output/history/*.json | head -1) dashboard/public/data/report.json
```

If the dashboard JSON is malformed or missing, `assemble_report.py` can fall back to a deterministic sector ranking and note that strategy analysis was unavailable.

### Step 5: Serve the report

```bash
python3 tools/serve_report.py --port 3420
```

If the Next.js dashboard exists, it starts there. Otherwise the script renders a local static preview and serves it from the same port.

---

## Troubleshooting – Dashboard Stale Data

If the dashboard shows old picks (e.g., Amazon, Google) even after running the workflow, the most common causes are:

1. **Forgot to run `assemble_report.py`** – the dashboard file is **only** written when you run the full `assemble_report` step. Running `mega_agent.py` alone does not update the dashboard.
2. **MegaAgent output was not saved** – now fixed by using the `--output` flag in Step 3, which writes the strategy JSON directly to `/tmp/tododeia/strategy.json`. Ensure that flag is always passed.
3. **Browser cache** – do a hard refresh (`Ctrl+Shift+R` / `Cmd+Shift+R`) or open the dashboard in an incognito window.
4. **The copy command wasn’t executed** – repeat the `cp` command from Step 4 to ensure `dashboard/public/data/report.json` points to the latest history.

**Quick sanity check**: compare the picks shown in the console (the `PICKS SUMMARY` table in stderr) with the report file. They must match because the `--output` flag writes the exact same data to `/tmp/tododeia/strategy.json` which the assembler uses.

## Error handling

- If the MegaAgent returns malformed JSON, re-run it once with correction instructions.
- If `assemble_report.py` fails validation, it prints the missing fields and exits non-zero.
- If history is missing, the pipeline uses the fallback accuracy block and skips trailing stops.
- Do not add scheduler / cron / launchd logic yet.

## Important notes

- Always use today's date when constructing search queries.
- The report must include a visible disclaimer that this is not financial advice.
- Never cache or reuse old data — every invocation does fresh research.
- Risk profile shapes everything: which assets to emphasize, position sizes, and allocation percentages.
