# RaitBox examples — a guided tutorial series

Each file is a **self-contained Jupyter notebook** and a mini-tutorial: it opens
with *what you'll learn · key concepts · how it works · prerequisites* and ends
with a *recap & related notebooks*. They're ordered by **increasing complexity**
(and setup effort): work through them in order, or jump to a topic.

Every LLM-backed notebook uses **Amazon Bedrock** (`bedrock-runtime`) — no stubs,
no third-party model keys unless noted. Trivial API-only demos were merged into
`01-basics`; every remaining notebook has a genuine need for a sandbox.

New to the ideas here? Read [../docs/CONCEPTS.md](../docs/CONCEPTS.md) first — it
explains sandboxes, backends, the execution model, and the eval/RL concepts these
notebooks build on.

## Learning path

**Foundations** — AWS creds + region only
- `01-basics` — the whole sandbox API: run_code, files, commands, errors, async, backends

**Agents — LLMs driving sandboxes** — + Bedrock model access
- `02-agent-feedback-loop` — generate → run → feed errors back → retry
- `03-claude-code-interpreter` — a code-execution **tool** via Converse tool use
- `04-data-analysis-chart` — analyze data, download a generated chart

**Evaluation — scoring model code**
- `05-eval-harness` — hidden tests, one isolated sandbox per task
- `06-iterative-self-repair` — iterate against a real `pytest` suite until green
- `07-pass-at-k` — pass@1 / pass@k with concurrent sandboxes

**Training data & environments — for RL/SFT**
- `08-verifiable-task-synthesis` — generate + sandbox-verify new coding tasks
- `09-reward-comparison` — SWE-RL similarity reward vs execution reward
- `10-parallel-eval-runner` — Harbor-style dataset × agent × parallel envs → report

**Frameworks & infrastructure** — extra setup (a key, or a MicroVM image)
- `11-langgraph-bedrock-code-interpreter` — LangGraph agent (Responses API; needs a Bedrock API key)
- `12-background-and-snapshots` — background processes, `get_host`, pause/resume *(microvm)*
- `13-claude-code-cli-in-sandbox` — run the Claude Code **CLI** inside a MicroVM *(microvm)*
- `14-swebench-lite` — model diff → `git apply` → re-run suite *(microvm)*
- `15-swe-gym-env` — a Gym-style `reset`/`step` env, executable reward, trajectory JSONL *(microvm)*

**Beyond E2B — RaitBox extensions** (each showcases a distinct added capability)
- `16-full-stack-preview` — serve an agent-built web app via a signed preview link; `secrets` + volume *(microvm)*
- `17-lsp-guided-agent` — ground a coding agent with real `sbx.lsp` symbols before it edits *(microvm)*
- `18-computer-use-agent` — screenshot → model → mouse/keyboard loop with `sbx.computer_use` *(microvm + desktop image)*
- `19-warm-pool-eval` — cut cold starts with `WarmPool`; read the win from OpenTelemetry spans
- `20-interactive-pdb-pty` — hold a live `pdb` session over `sbx.pty` *(microvm)*
- `21-custom-image-and-mcp` — declarative `Image` builder + registry, then expose sandboxes over MCP

## Prerequisites at a glance
- **All:** AWS credentials on the standard chain + a region (`AWS_DEFAULT_REGION`).
- **LLM notebooks (02–19):** Bedrock model access for `RAITBOX_BEDROCK_MODEL`.
- **`11`:** additionally a Bedrock API key (`OPENAI_API_KEY`) for the Responses endpoint.
- **MicroVM notebooks (12–18, 20):** a built image ARN in `RAITBOX_MICROVM_IMAGE`
  (execution role optional) — see `../docs/INTEGRATION.md`. `18` needs a
  **desktop-enabled** image (`xorg-x11-server-Xvfb` + `xdotool`/`scrot`, which are
  not in the AL2023 base) — build one in `21`.
- **`16`:** optional `RAITBOX_EFS_ID` (volume); **`21`:** optional
  `RAITBOX_BUILD_BUCKET` + `RAITBOX_BUILD_ROLE_ARN` (to actually build) and the
  `raitbox[mcp]` extra for a live MCP server.

Run with `pip install -e ..` (plus any in-cell `%pip install`), then open in Jupyter.
