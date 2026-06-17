# KubeHeal 🩺

> An autonomous, event-driven **Kubernetes self-healing agent** that diagnoses
> failing pods with a **local LLM**, proposes a fix, and applies it only after a
> **human approves in Slack** — with dry-run, verification, and automatic
> rollback. Runs entirely on your own machine; **no paid services**.

KubeHeal watches a cluster, and when a pod starts failing it figures out *why*,
proposes the *smallest safe fix*, and asks a human to approve it in Slack — then
applies it safely and confirms the workload actually recovered. The guiding
principle is **autonomy with guardrails**: the AI *proposes*; a human and a chain
of safety checks *dispose*. The agent never changes the cluster on its own.

---

## Why it exists

On-call engineers spend a lot of time on the same handful of Kubernetes failures:
a pod that needs a bit more memory, a misconfigured health check, a bad deploy
that should be rolled back. KubeHeal automates the **diagnosis and the proposed
fix** for those common cases, while keeping a human in control of every change —
shrinking time-to-recovery without handing the cluster over to an AI.

Two deliberate engineering choices set it apart:

- **Local LLM (via [Ollama](https://ollama.com)).** Pod logs and cluster details
  never leave the machine — important for privacy — and it costs nothing. The
  small model is kept reliable with strict structured output, a safety allow-list,
  and deterministic fixes where the model is weak.
- **Human-in-the-loop over Slack (Socket Mode).** Every change is an Approve/Reject
  (or Acknowledge) click. Socket Mode means **no public URL, tunnel, or ngrok**.

---

## What it can remediate

| Failure | KubeHeal's response | How |
| --- | --- | --- |
| **OOMKilled** | Raise the memory limit | local LLM, within the allow-list |
| **CrashLoop — wrong probe port** | Correct the liveness/readiness probe port | local LLM |
| **CrashLoop — slow start** (probe kills the app during warm-up) | Add a `startupProbe` so the liveness probe waits for startup | **deterministic** (no LLM) |
| **Bad recent deploy** (new revision crashing / unpullable image) | Propose reverting to the previous revision (`rollout undo`) | **deterministic** |
| **ImagePullBackOff / CreateContainerConfigError** (no prior good revision) | Post a **"needs a human"** notice (out of safe scope) | — |
| **App bug / missing config** | "needs a human" | — |

Every incident produces *some* Slack outcome — a fix to approve, a rollback to
approve, or a "needs a human" notice to acknowledge — so nothing is ever silently
dropped. Duplicate alerts for the same workload are de-duplicated, and an
unactioned alert is re-pinged on an interval until someone resolves it.

---

## How it works

```
                  ┌──────────────────────────────────────────────┐
   Kubernetes     │                KubeHeal process               │
   API (watch) ──▶│  Observer ─▶ decide ─▶ (LLM | deterministic) ─┼─▶ Slack alert
                  │     │                                          │   (Approve/Reject)
                  │     └─ logs + Pod events + current spec        │        │
                  │                                                │        ▼
                  │  Remediator ◀── on Approve ──────────── human clicks ◀──┘
                  │     dry-run ▶ apply ▶ verify ▶ rollback-on-failure
                  │  SQLite: approvals · audit log · cooldown      │
                  └──────────────────────────────────────────────┘
```

1. **Observe** — an event-driven watch on Pod status detects failures and resolves
   the owning **Deployment** (so fixes are durable, not applied to a throwaway pod).
   It gathers recent logs **and Pod events** (probe failures live in events, not logs).
2. **Decide** — for each incident KubeHeal picks the safest applicable strategy:
   *bad deploy → rollback*, *slow start → startupProbe*, *out-of-scope → needs a human*,
   otherwise *ask the local LLM* for a `resources`/probe patch.
3. **Validate** — LLM output is forced into a JSON schema and checked against a
   **safety allow-list** (only `resources` and probes may change). Hallucinated
   fields are rejected or stripped.
4. **Approve** — the proposal is posted to Slack as a Block Kit message showing a
   readable **before → after diff** (e.g. `resources.limits.memory: 10Mi → 40Mi`),
   with Approve/Reject buttons.
5. **Remediate** — on approval: **server-side dry-run → apply → verify** the rollout
   stays healthy for several consecutive checks → **roll back** automatically if it
   doesn't recover. Every step is written to a SQLite audit log.

See **[ARCHITECTURE.md](ARCHITECTURE.md)** for component and sequence diagrams and
the design rationale, and **[PLAN.md](PLAN.md)** for the full build log.

---

## Safety model

KubeHeal is built so that even a wrong diagnosis can't do harm:

- **Human approval is always required** — there is no auto-apply mode.
- **Patch allow-list** — the LLM may only change `resources` (cpu/memory) and
  probes. Image, command, env, securityContext, volumes, replicas are rejected.
- **Server-side dry-run** before every real change.
- **Sustained-health verification** — a fix must keep the workload healthy across
  several consecutive checks (a crash-looping pod is briefly "ready" on start, so
  one check isn't enough).
- **Automatic rollback** if verification fails.
- **Single, dedicated namespace** — it can never touch `kube-system`.
- **Full audit trail** of every diagnosis, decision, and applied change.

---

## Design note: model choice (and why the prompt explains specific cases)

KubeHeal deliberately runs on a **small, free, local model** —
`granite3.1-dense:2b` via Ollama — for **privacy** (pod logs never leave the
machine) and **zero cost**. That choice has a real trade-off: a 2B model often
**can't infer the right fix on its own**, so the system prompt
([`prompts/sre_system_prompt.txt`](prompts/sre_system_prompt.txt)) includes
**case-specific decision rules** (e.g. *wrong probe port* vs *slow start*), and
the trickiest fixes are computed **deterministically in code** rather than left to
the model (the slow-start `startupProbe` and the rollback). In short: we trade some
of the model's "figure it out yourself" generality for **reliability and safety**
with a weak local model.

That specificity is a property of the *model*, not the design — **the model is
swappable in one line.** Point it at a bigger, smarter model and you can lean on
its own reasoning (and trim the case rules):

```bash
ollama pull llama3.1:8b
# then set OLLAMA_MODEL in .env
OLLAMA_MODEL=llama3.1:8b
```

A larger model (e.g. `llama3.1:8b`, or a bigger Granite) generally needs fewer
hand-written rules — at the cost of more RAM/VRAM and slower inference. Everything
else (safety allow-list, dry-run, verify, rollback, human approval) stays exactly
the same regardless of model.

## Tech stack

Python · official `kubernetes` client (watch + patch) · **Ollama** running
`granite3.1-dense:2b` (swappable) · `slack-bolt` (Socket Mode) · Pydantic · SQLite ·
pytest · **Kind** (local cluster) · Docker. Everything is free and runs locally.

---

## Getting started

### Prerequisites

- **Docker** (running) — [Kind](https://kind.sigs.k8s.io) runs the cluster inside it
- **kind** and **kubectl**
- **[Ollama](https://ollama.com)**
- **Python 3.10+**
- A **Slack workspace** where you can create an app (a free personal workspace works)

### 1. Install Python deps

```bash
python -m venv .venv
# macOS/Linux:
source .venv/bin/activate
# Windows (PowerShell):
.\.venv\Scripts\Activate.ps1

pip install -r requirements.txt
```

### 2. Start the local cluster + namespace + RBAC

```bash
kind create cluster --name kubeheal
kubectl create namespace kubeheal-demo
kubectl apply -f deploy/rbac.yaml      # least-privilege ServiceAccount/Role
```

### 3. Pull the local model

```bash
ollama pull granite3.1-dense:2b
```

### 4. Create the Slack app

1. Go to **https://api.slack.com/apps → Create New App → From a manifest**, pick
   your workspace, and paste [`deploy/slack-manifest.yaml`](deploy/slack-manifest.yaml).
2. **Install App → Install to Workspace → Allow.**
3. Invite the bot to your channel: in Slack, type `/invite @KubeHeal` in the
   channel you'll use (e.g. `#all-kubeheal-dev`).

### 5. Configure

```bash
cp .env.example .env      # Windows: copy .env.example .env
```
Fill in `.env`:
- `SLACK_BOT_TOKEN` — Bot User OAuth Token (`xoxb-…`), from *OAuth & Permissions*
- `SLACK_APP_TOKEN` — App-Level Token (`xapp-…`) with the `connections:write`
  scope, from *Basic Information → App-Level Tokens*
- `SLACK_CHANNEL` — e.g. `#all-kubeheal-dev`

### 6. Run

```bash
python -m kubeheal.main
```

---

## Try it

With the agent running, break something and watch it get caught and proposed in
Slack:

```bash
# OOMKilled  -> proposes a memory bump
kubectl apply -f deploy/crashloop-demo.yaml

# Slow-start crashloop -> proposes adding a startupProbe
kubectl apply -f deploy/slowstart-demo.yaml

# Image/config errors -> "needs a human"
kubectl apply -f deploy/error-demos.yaml

# Bad deploy -> proposes a rollback to the previous revision
kubectl apply -f deploy/rollout-demo.yaml
kubectl -n kubeheal-demo rollout status deploy/rollout-demo
kubectl -n kubeheal-demo set image deploy/rollout-demo web=nginx:does-not-exist-9999
```

Each demo manifest has a header comment explaining the failure it triggers and the
fix KubeHeal proposes. Approve in Slack and watch the workload recover.

---

## Project layout

```
kubeheal/
  observer.py     # watch K8s, classify failures, resolve owning Deployment
  log_fetcher.py  # recent logs + Pod events
  brain.py        # local LLM: schema-constrained diagnosis + patch
  safety.py       # patch allow-list (resources + probes only)
  rollback.py     # detect a bad recent deploy -> revert to previous revision
  probefix.py     # deterministic slow-start fix (add a startupProbe)
  patchdiff.py    # render a before -> after diff for Slack
  remediator.py   # dry-run -> apply -> verify -> rollback
  slack_app.py    # Slack Bolt (Socket Mode): alerts, Approve/Reject/Acknowledge
  store.py        # SQLite: approvals, audit log, cooldown
  main.py         # wires it all together
deploy/           # demo workloads, RBAC, Slack app manifest, in-cluster manifest
tests/            # pytest suite (51 tests)
```

## Tests

```bash
python -m pytest -q
```

## Roadmap

- More failure types: unschedulable pods (lower over-large requests), CPU
  throttling (via metrics-server).
- Optional, flag-gated allow-list extensions (image rollback to a prior tag,
  replica scaling).
- Per-incident Slack threads and a `/metrics` (Prometheus) endpoint.

## License

MIT — see [LICENSE](LICENSE).
