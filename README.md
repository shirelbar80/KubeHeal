# KubeHeal

> Autonomous, event-driven Kubernetes remediation agent with a **local** LLM brain
> and a **human-in-the-loop** Slack approval flow. Zero paid services.

KubeHeal watches a Kubernetes cluster for failing pods (`CrashLoopBackOff`,
`OOMKilled`), asks a local LLM (via Ollama) to diagnose the failure and propose a
patch, then posts it to Slack for a human to **Approve** or **Reject**. Approved
patches are dry-run, applied, and verified — with a rollback path if recovery fails.

A human approval is **always required**. The agent never patches the cluster on
its own, and it can only mutate a restricted set of fields (`resources` + probes)
in a single dedicated namespace.

See [PLAN.md](PLAN.md) for the full design, decisions, and phased task breakdown.

## Status

✅ **Phase 1 — Observer complete.** Detects `OOMKilled` / `CrashLoopBackOff`,
resolves the owning Deployment, and extracts logs + current spec. Next:
Phase 2 (Brain / LLM diagnosis) → Phase 3 (Slack ChatOps).

## Tech stack

- Python 3.10+ (use the `py` launcher on Windows)
- Local cluster: **Kind**
- Local LLM: **Ollama** running `granite3.1-dense:2b`
- ChatOps: **Slack** via `slack-bolt` in **Socket Mode** (no ngrok)
- K8s access: official `kubernetes` Python client
- State: SQLite

## Prerequisites

- Docker Desktop (running)
- Kind (`kind`)
- Ollama (`ollama`)
- Python via `py`
- A Slack workspace where you can create an app

## Setup

> Detailed, copy-pasteable setup is added as each phase lands. High-level:

```powershell
# 1. Python deps (in a virtual env)
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

# 2. Local cluster
kind create cluster --name kubeheal

# 3. Local LLM
ollama pull granite3.1-dense:2b

# 4. Config
copy .env.example .env   # then fill in Slack tokens + channel

# 5. Run (once implemented)
py -m kubeheal.main
```

## Safety model

- **HITL always on** — no auto-approve.
- **Patch allow-list** — only `resources` + probes are mutable.
- **Server-side dry-run** before every real apply.
- **Single namespace** — never touches `kube-system`.
- **Audit log** of every diagnosis, decision, and applied patch.
- **Rollback** when post-patch verification fails.

## License

TBD.
