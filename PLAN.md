# KubeHeal — Implementation Plan & Task Breakdown

> Autonomous, event-driven Kubernetes remediation agent with a local LLM brain
> and a Human-in-the-Loop (HITL) Slack approval flow. **Zero-cost stack** — every
> component below has a free tier or is fully open source.

---

## 1. Summary of What We're Building

A daemon that:

1. **Watches** a local K8s cluster for failing pods (`CrashLoopBackOff`, `OOMKilled`, etc.).
2. **Diagnoses** the failure by feeding pod logs + spec to a **local LLM** (Ollama).
3. **Proposes** a YAML/JSON patch.
4. **Asks a human** to approve/reject/edit the patch via **Slack**.
5. **Applies** the patch through the K8s API once approved, then verifies recovery.

---

## 1a. Locked Decisions (confirmed)

| Topic | Decision |
| --- | --- |
| Local cluster tool | **Kind** (Kubernetes in Docker) |
| LLM model | **`granite3.1-dense:2b`** via Ollama (swappable; 8B optional but slower on 4 GB VRAM) |
| Slack transport | **Socket Mode** (no ngrok) |
| Patch allow-list | **`resources` (limits/requests) + probes only** |
| Human approval | **Always required** — no auto-approve, ever |
| Namespace scope | **One dedicated namespace: `kubeheal-demo`** |
| Text-override (NL rewrite) | **Deferred — added LATER as a Phase 3 stretch, not in MVP** |
| Run location | **Local for MVP**; in-cluster as Phase 4 stretch |
| OpenShift | **Out of scope for MVP**; noted as future work |
| LLM client lib | **`ollama` Python lib** |

> Target machine: Windows 11, i7-10750H (6c/12t), 16 GB RAM, NVIDIA GTX 1650 (4 GB VRAM). Python via the `py` launcher (3.14).

---

## 2. Recommended Architecture Changes (vs. the original brief)

The original design is solid. These are the changes I recommend for best-practice + cost reasons. **None are set in stone — see open questions in §9.**

| #   | Original                             | Recommended Change                                                                                                            | Why                                                                                                                                                                                                                                                                                         |
| --- | ------------------------------------ | ----------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| A   | **ngrok** to tunnel Slack webhooks   | **Slack Socket Mode** (WebSocket)                                                                                             | No public URL, no tunnel, no ngrok account/limits. Slack pushes events over an outbound WebSocket your app opens. Eliminates a whole class of setup pain and the free-ngrok rotating-URL problem. FastAPI still used for internal health/metrics, but the Slack path needs no inbound HTTP. |
| B   | Watch **events** for crash detection | Watch **Pod objects** and inspect `container_statuses`                                                                        | Pod `status` is the source of truth for `OOMKilled` (in `state.terminated.reason`) and `CrashLoopBackOff` (in `state.waiting.reason`). Events are lossy and TTL-expire.                                                                                                                     |
| C   | `granite-code:3b`                    | Default to a **reasoning** model (`granite3.1-dense:2b`/`8b` or `llama3.1:8b`); keep `granite-code` only if hardware is tight | `granite-code` is tuned for code _completion_, not diagnostic reasoning + structured JSON. We need analysis + valid JSON output.                                                                                                                                                            |
| D   | LLM returns free-form patch          | **Force structured JSON output** (Ollama `format: json` / JSON schema) and validate before use                                | Prevents malformed patches reaching the cluster. The LLM output is never trusted blindly.                                                                                                                                                                                                   |
| E   | Apply patch directly                 | Add a **safety layer**: server-side `dry-run` first, allow-list of mutable fields, audit log, and post-patch verification     | This is a tool that mutates production-like infra. Guardrails are non-negotiable.                                                                                                                                                                                                           |
| F   | — (not mentioned)                    | **Loop / storm prevention**: dedup events, cooldown per workload, ignore pods KubeHeal just patched                           | Without this, a crashing pod fires dozens of events → Slack spam → possible patch loops.                                                                                                                                                                                                    |
| G   | —                                    | **Persisted pending-approval state** (SQLite)                                                                                 | If the process restarts between "alert sent" and "approved", we must not lose or double-apply.                                                                                                                                                                                              |

### Proposed component diagram (revised)

```
                         ┌─────────────────────────────┐
                         │      K8s Control Plane       │
                         └──────────────┬──────────────┘
                                        │ Watch API (Pod status changes)
                                        ▼
┌───────────────────────────────────────────────────────────────┐
│                        KubeHeal Process                          │
│                                                                  │
│  ┌────────────┐   logs+spec   ┌────────────┐  diagnosis+patch   │
│  │  Observer  │──────────────▶│   Brain    │───────────────┐    │
│  │ (watcher)  │               │ (Ollama)   │               │    │
│  └────────────┘               └────────────┘               ▼    │
│        │ dedup/cooldown                              ┌───────────┐│
│        ▼                                             │  Approval ││
│  ┌────────────┐                                      │   Store   ││
│  │ Event Queue│                                      │ (SQLite)  ││
│  └────────────┘                                      └─────┬─────┘│
│                                                            │      │
│  ┌──────────────────────────────────────────────────┐     │      │
│  │ Slack Bolt app (Socket Mode)  ◀── approve/reject ─┼─────┘      │
│  │   - sends Block Kit alert                          │            │
│  │   - handles button clicks & text overrides         │            │
│  └───────────────────────┬────────────────────────────┘           │
│                          │ on approve                              │
│                          ▼                                         │
│                  ┌──────────────┐  dry-run → apply → verify        │
│                  │  Remediator  │───────────────────────────┐      │
│                  └──────────────┘                           │      │
└─────────────────────────────────────────────────────────────┼─────┘
                                                               ▼
                                                  ┌─────────────────────────────┐
                                                  │      K8s Control Plane       │
                                                  │   (patched workload rolls)   │
                                                  └─────────────────────────────┘
```

---

## 3. Technology Stack (all free)

| Layer           | Choice                                                                         | Cost | Notes                                                         |
| --------------- | ------------------------------------------------------------------------------ | ---- | ------------------------------------------------------------- |
| Language        | Python 3.10+                                                                   | Free |                                                               |
| Cluster         | **Kind** (or Minikube)                                                         | Free | Kind is lighter & faster to spin up in CI; Minikube fine too. |
| K8s client      | `kubernetes` (official Python client)                                          | Free |                                                               |
| Local LLM       | **Ollama** + `granite3.1-dense` / `llama3.1:8b`                                | Free | Runs on local hardware.                                       |
| LLM client      | `openai` SDK pointed at `http://localhost:11434/v1` **or** `ollama` python lib | Free | Either works; `ollama` lib gives native `format:json`.        |
| Slack interface | **`slack_bolt`** in **Socket Mode**                                            | Free | No ngrok. Slack free workspace is enough.                     |
| Internal API    | FastAPI + Uvicorn (health/metrics only)                                        | Free | Optional if we go full Socket Mode.                           |
| State           | SQLite (stdlib `sqlite3`)                                                      | Free | Pending approvals + audit log.                                |
| Config          | `pydantic-settings` + `.env`                                                   | Free |                                                               |
| Packaging       | `uv` or `pip` + `requirements.txt`                                             | Free |                                                               |
| Tests           | `pytest`                                                                       | Free |                                                               |

> **Cost check:** ngrok removed (Socket Mode). No paid APIs (local LLM). Slack free tier. Everything else OSS. ✅ $0.

---

## 4. Repository Layout (proposed)

```
KubeHeal/
├── PLAN.md                  # this file
├── README.md
├── requirements.txt / pyproject.toml
├── .env.example
├── config.py                # pydantic settings
├── kubeheal/
│   ├── __init__.py
│   ├── main.py              # entrypoint: starts observer + slack app
│   ├── observer.py          # K8s watch loop + failure detection
│   ├── log_fetcher.py       # pull last N log lines (incl. previous container)
│   ├── brain.py             # Ollama prompt + JSON-schema validation
│   ├── remediator.py        # dry-run, apply patch, verify, rollback
│   ├── slack_app.py         # Bolt app, Block Kit builders, action handlers
│   ├── store.py             # SQLite: pending approvals + audit log
│   ├── safety.py            # patch allow-list / validation guardrails
│   └── models.py            # dataclasses/pydantic: Incident, Diagnosis, Patch
├── deploy/
│   ├── crashloop-demo.yaml  # OOMKilled demo (memory limit 10Mi)
│   └── rbac.yaml            # least-privilege ServiceAccount/Role
├── prompts/
│   └── sre_system_prompt.txt
└── tests/
    ├── test_safety.py
    ├── test_brain_parsing.py
    └── fixtures/
```

---

## 5. Phased Implementation Plan

### Phase 0 — Project bootstrap (½ day)

- [x] Create repo layout above; `git init`.
- [x] `requirements.txt`: `kubernetes`, `slack_bolt`, `ollama` (or `openai`), `pydantic-settings`, `fastapi`, `uvicorn`, `pytest`.
- [x] `.env.example` with all config keys (Slack tokens, model name, namespace, cooldown).
- [x] `config.py` loads settings via pydantic.
- [x] README skeleton.

### Phase 1 — Cluster observation & infra (Day 1)

- [x] Spin up local cluster (`kind create cluster` or `minikube start`). — Kind cluster `kubeheal`, namespace `kubeheal-demo`.
- [x] Write `deploy/crashloop-demo.yaml`: a pod that OOMKills (e.g. tight `resources.limits.memory: 10Mi` + a process that allocates more) **and** a second demo that `CrashLoopBackOff`s (bad command/exit 1) for variety.
- [x] Write `deploy/rbac.yaml`: ServiceAccount + Role limited to `get/list/watch pods`, `get pods/log`, and `patch deployments` in one namespace. (Use this even when running locally with kubeconfig — document the least-privilege intent.)
- [x] `observer.py`: `watch.Watch().stream(v1.list_namespaced_pod, ...)`.
- [x] **Failure detection** from `pod.status.container_statuses`:
  - `state.waiting.reason == "CrashLoopBackOff"`
  - `state.terminated.reason == "OOMKilled"`
  - `last_state.terminated.reason == "OOMKilled"` (catch already-restarted)
- [x] `log_fetcher.py`: last 50 lines via `read_namespaced_pod_log(..., tail_lines=50)`; also fetch `previous=True` logs (the crashed container's logs are usually in the _previous_ instance). _Note: client mis-deserializes the log endpoint with `_preload_content=True` (returns the `repr` of bytes) — fixed by reading the raw response and decoding._
- [x] Map a pod back to its **owner** (ReplicaSet → Deployment) so patches target the Deployment, not the ephemeral pod.
- [x] **Dedup + cooldown**: in-memory (then SQLite) keyed by owner workload; ignore repeat events within N minutes.
- [x] Manual test: deploy demo, confirm exactly one detection per workload with logs printed.

**Phase 1 done when:** crashing a demo pod prints a single structured `Incident` (workload, reason, logs) to console. ✅ **DONE & VERIFIED** — both `OOMKilled` and `CrashLoopBackOff` detected with correct workload, spec, and decoded logs.

### Phase 2 — Local AI brain (Day 2)

- [ ] Install Ollama; pull model (`ollama pull granite3.1-dense:2b` or `llama3.1:8b`).
- [ ] `prompts/sre_system_prompt.txt`: strict SRE prompt — _"Analyze logs + current spec. Output JSON only: `{diagnosis, root_cause, confidence, patch:{...}, patch_explanation}`. The patch must be a valid K8s strategic-merge patch for the named Deployment. Do not invent fields."_
- [ ] `brain.py`:
  - Send system prompt + incident (logs, reason, **current resource spec**) to Ollama with `format=json`.
  - **Validate** the returned JSON against a pydantic schema (`Diagnosis`/`Patch`).
  - On invalid JSON → one retry with a "your last output was invalid JSON, fix it" message; then fail gracefully.
- [ ] `safety.py`: enforce a **patch allow-list** — only permit mutations to a known-safe set of fields (e.g. `resources.limits/requests`, `livenessProbe`, `readinessProbe`, env values, replica count within bounds). Reject anything touching `image` to arbitrary registries, `securityContext` escalations, hostPath volumes, etc. **(see open question Q4)**
- [ ] Wire Observer → Brain: on incident, fetch logs, call brain, print diagnosis + validated patch.

**Phase 2 done when:** an OOMKilled demo yields a valid JSON patch that bumps the memory limit, and a deliberately-broken model response is rejected by the validator.

### Phase 3 — Interactive ChatOps via Slack (Day 3)

- [ ] Create a Slack app (manifest provided in README). Scopes: `chat:write`, `commands`, plus enable **Socket Mode** (App-Level Token `xapp-…`) and **Interactivity**.
- [ ] `slack_app.py` (Bolt, Socket Mode):
  - Block Kit message: diagnosis, confidence, a **rendered diff/patch** in a code block, and **Approve / Reject** buttons (+ optional "Edit" that opens a modal or accepts a thread reply).
  - Action handler `approve_patch` → look up pending incident in store → call remediator.
  - Action handler `reject_patch` → mark rejected, update message.
  - **Text override — DEFERRED (Phase 3 stretch, to be done LATER):** _not part of the MVP._ The MVP ships with Approve/Reject buttons only. Once those work end-to-end, this feature will be added later: listen for thread replies / `message` events; pass user text + original incident back to `brain.py` to _rewrite_ the patch, then post the new patch for re-approval.
- [ ] `store.py` (SQLite): `pending_approvals(id, workload, patch_json, status, created_at)` + `audit_log(...)`. Persist on alert; update on action.
- [ ] `remediator.py`:
  - **dry-run first**: `patch_namespaced_deployment(..., dry_run="All")` → if it errors, report back to Slack, don't apply.
  - Apply for real; record in audit log.
  - **Verify recovery**: watch the workload for ~N seconds; report success/failure back to the Slack thread.
  - **Rollback hook**: keep the previous spec; offer a rollback button if recovery fails.
- [ ] `main.py`: start observer (thread/async task) + Slack Socket Mode app together; graceful shutdown.
- [ ] FastAPI (optional): `/healthz`, `/metrics` only.

**Phase 3 done when (MVP):** crashing the demo → Slack alert → click Approve → patch applied via dry-run-then-real → pod recovers → success posted in thread. Reject path also works. (Text-override is **explicitly out of MVP scope — added later** as a stretch.)

### Phase 4 — Hardening & polish (stretch)

- [ ] Cooldown/dedup moved fully into SQLite with TTL.
- [ ] "KubeHeal-applied" annotation on patched workloads to avoid re-triggering on its own changes.
- [ ] Structured logging (`structlog` or stdlib JSON logs).
- [ ] Unit tests: safety allow-list, brain JSON parsing/validation, owner resolution.
- [ ] README with full setup, Slack manifest, demo GIF/screenshots.
- [ ] Dockerfile + optional in-cluster deployment manifest (run KubeHeal _inside_ the cluster with the RBAC SA).

---

## 6. Safety & Guardrails (cross-cutting — do not skip)

1. **Never trust LLM output** — validate JSON schema + field allow-list before any cluster call.
2. **HITL is mandatory** — no patch applies without an explicit human Approve. (Add a config flag `AUTO_APPROVE=false` and keep it false by default.)
3. **Server-side dry-run** before every real apply.
4. **Least-privilege RBAC** — scoped ServiceAccount, single namespace.
5. **Cooldown + dedup** to prevent alert storms and patch loops.
6. **Audit log** of every diagnosis, decision, and applied patch (who approved, when).
7. **Rollback path** when post-patch verification fails.
8. **Scope confinement** — only operate in a configured namespace (never `kube-system`).

---

## 7. Risks & Mitigations

| Risk                                     | Mitigation                                                        |
| ---------------------------------------- | ----------------------------------------------------------------- |
| Local LLM hardware too weak for 8b model | Fall back to 2–3b model; make model name configurable.            |
| LLM produces plausible-but-wrong patch   | HITL approval + dry-run + allow-list + confidence shown in Slack. |
| Slack free workspace limits              | Socket Mode + low message volume stays well within free tier.     |
| Watch stream drops connection            | Auto-reconnect loop with `resourceVersion` bookmarks.             |
| Process restart loses pending approval   | SQLite persistence of pending state.                              |
| Patch loop (agent fights the cluster)    | Cooldown per workload + annotation tagging + max-attempts cap.    |

---

## 8. Definition of Done (MVP)

- One command brings up cluster + demo workloads.
- Running `python -m kubeheal.main` detects a real OOMKilled/CrashLoopBackOff.
- A correct, validated patch is proposed by the local LLM.
- Slack alert with Approve/Reject works end-to-end via Socket Mode (no ngrok).
- Approve → dry-run → apply → verify → report. Pod recovers.
- Audit log records the full incident lifecycle.
- README lets a new dev reproduce it from scratch for free.

---

## 9. Open Questions (please answer — I'll adjust the plan)

> Answer inline under each; defaults in **bold** are what I'll assume if you don't.

**Q1. Cluster tool:** Kind or Minikube? (**Default: Kind** — lighter/faster. You mentioned Minikube — happy to use it if you prefer.)

> _Answer:_ what is Minikube and where will you use it? explain

**Q2. LLM model:** Do you want me to keep `granite-code:3b`, or switch to a reasoning model? What are your machine specs (RAM / GPU / Apple Silicon vs Windows)? This decides model size. (**Default: `granite3.1-dense:2b` for safety on modest hardware; `llama3.1:8b` if you have ≥16GB RAM/GPU.**)

> _Answer:_ where can i check my machine specs? tell me how

**Q3. ngrok vs Socket Mode:** OK to drop ngrok and use Slack Socket Mode (recommended, simpler, free)? Or do you specifically want the FastAPI + ngrok webhook flow as in the brief (maybe for learning purposes)? (**Default: Socket Mode.**)

> _Answer:_ socket mode is good

**Q4. Patch scope / allow-list:** Which mutations should KubeHeal be allowed to make? Options: only `resources` limits/requests (safest), also probes, also env vars, also `replicas`, also `image` tags. (**Default: resources + probes only.**)

> _Answer:_ resources + probes only.

**Q5. Auto-approve:** Should there _ever_ be a fully-autonomous mode (apply without human) for low-risk patches, or is HITL always required? (**Default: HITL always required; auto-approve disabled.**)

> _Answer:_ a human approval is always neccesary

**Q6. Target namespace(s):** Single fixed namespace (e.g. `kubeheal-demo`) or all non-system namespaces? (**Default: single configurable namespace, defaults to `default`.**)

> _Answer:_ what does that mean? where do you use it? explain

**Q7. Text-override (LLM rewrite) bonus:** Is the natural-language "rewrite the patch" feature in-scope for the MVP, or a stretch goal? (**Default: stretch goal in Phase 3, after buttons work.**)

> _Answer:_ what does that mean exactly? explain

**Q8. Run location:** Should KubeHeal eventually run _inside_ the cluster (as a pod with the RBAC SA), or only locally against the cluster from your laptop? (**Default: local for MVP; in-cluster as Phase 4 stretch.**)

> _Answer:_ local for MVP; in-cluster as Phase 4 stretch

**Q9. Slack details:** Do you already have a Slack workspace + permission to create an app, and which channel should alerts go to? (**Default: I'll write a Slack app manifest and you create it in your own workspace.**)

> _Answer:_ I'll write a Slack app manifest and you create it in your own workspace - explain to me how this would work

**Q10. OpenShift:** The brief mentions OpenShift. Is OpenShift compatibility required for the MVP, or is vanilla K8s (Kind/Minikube) enough for now? (**Default: vanilla K8s for MVP; note OpenShift `Route`/`oc` differences as future work.**)

> _Answer:_ vanilla K8s for MVP; note OpenShift `Route`/`oc` differences as future work.

**Q11. LLM client lib:** `ollama` python lib (native `format:json`) or the `openai` SDK pointed at the Ollama endpoint (as in the brief)? (**Default: `ollama` lib for cleaner JSON handling; trivial to swap.**)

> _Answer:_ `ollama` lib for cleaner JSON handling; trivial to swap

---

## 10. Next Action

Once you answer §9 (even partially), I'll:

1. Scaffold the repo (`Phase 0`).
2. Write `deploy/crashloop-demo.yaml` + `rbac.yaml`.
3. Implement the Observer and confirm detection before moving to the Brain.
