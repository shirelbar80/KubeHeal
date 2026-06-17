# KubeHeal

> Autonomous, event-driven Kubernetes remediation agent with a **local** LLM brain
> and a **human-in-the-loop** Slack approval flow. Zero paid services.

KubeHeal watches a Kubernetes cluster for failing pods (`CrashLoopBackOff`,
`OOMKilled`, `ImagePullBackOff`, `CreateContainerConfigError`), asks a local LLM
(via Ollama) to diagnose the failure and propose a patch, then posts it to Slack
for a human to **Approve** or **Reject**. Approved patches are dry-run, applied,
and verified — with a rollback path if recovery fails. When a failure looks like
a **bad recent deploy**, KubeHeal instead proposes reverting to the previous
revision (`rollout undo`) — deterministic, no LLM. Failures that can't be fixed
within the allow-list (e.g. a bad image with no prior good revision, or missing
config) skip the LLM and go straight to a **"needs a human"** notice.

A human approval is **always required**. The agent never patches the cluster on
its own, and it can only mutate a restricted set of fields (`resources` + probes)
in a single dedicated namespace.

See [ARCHITECTURE.md](ARCHITECTURE.md) for the architecture, diagrams, and design
decisions, and [PLAN.md](PLAN.md) for the full phased task breakdown.

## Status

✅ **Phases 1–4 complete & verified live (incl. a real Slack approve→heal).** Full
pipeline: detect (with Pod events) → diagnose (local LLM) → Slack Approve/Reject →
dry-run → apply → verify → rollback, with SQLite-backed cooldown + audit trail,
structured logging, a `kubeheal.io/last-remediation` annotation, and an optional
in-cluster Dockerfile/manifest. Demos: OOMKilled, CrashLoopBackOff (config — not
auto-fixable), and a fixable bad-liveness-probe.

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

# 5. Run
py -m kubeheal.main
```

## Slack setup

1. Create the app from [`deploy/slack-manifest.yaml`](deploy/slack-manifest.yaml):
   **https://api.slack.com/apps → Create New App → From a manifest**, pick your
   workspace, paste the YAML, Create.
2. **Install App → Install to Workspace → Allow.**
3. Collect the two tokens into `.env`:
   - **Bot User OAuth Token** (`xoxb-…`) from *OAuth & Permissions* → `SLACK_BOT_TOKEN`
   - **App-Level Token** (`xapp-…`) from *Basic Information → App-Level Tokens*
     (create one with the `connections:write` scope) → `SLACK_APP_TOKEN`
4. Invite the bot to your channel: `/invite @KubeHeal` in `#all-kubeheal-dev`.
5. `py -m kubeheal.main`, then break a demo workload:
   `kubectl apply -f deploy/crashloop-demo.yaml`.

Socket Mode means **no public URL / ngrok** is needed — button clicks arrive over
an outbound WebSocket.

## Safety model

- **HITL always on** — no auto-approve.
- **Patch allow-list** — only `resources` + probes are mutable.
- **Server-side dry-run** before every real apply.
- **Single namespace** — never touches `kube-system`.
- **Audit log** of every diagnosis, decision, and applied patch.
- **Rollback** when post-patch verification fails.

## License

TBD.
