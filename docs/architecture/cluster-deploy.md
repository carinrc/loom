# Cluster deployment design

**Status: design** (2026-06-15). Tracking PR: TBD. Supersedes inline guidance in `docs/architecture/service-mode.md`.

This spec extends [#49](https://github.com/carinrc/loom/issues/49) (Production cluster deployment with user-supplied model provider gateway) for the specific deployment target the team is committing to: a small group of high-end machines (initial target: 2–4 ASUS Ascent GX10 boxes, ARM v9.2-A, NVIDIA GB10 Grace Blackwell, 128 GB unified memory, 4 TB local NVMe, 200 Gbps ConnectX-7 interconnect, 10 GbE LAN), but the design must not be coupled to that hardware. Loom is a platform; the deployment shape should be selectable.

## Status block

| Item | State |
|---|---|
| Hardware target (initial) | 2–4× ASUS Ascent GX10 (ARM v9.2-A, GB10) |
| Hardware target (future) | x86 servers, cloud k8s, hybrid |
| ARM-rebuild benchmarks (SWE-Bench, OSWorld, …) | **deferred** — out of scope for this spec; documented as a gap |
| Local vLLM / `hf_execution=local-vllm` | **dropping** — users supply OpenAI-compatible endpoints (matches #49) |
| Auth model | per-team token (today's model) — **no per-user IDP** for now |
| Topology | three selectable strategies (A/B/C); user picks at deploy time |
| HA Postgres / MinIO | **deferred** — Strategy C skeleton + documented gap |

## Goals

1. One CLI verb family (`loom deploy {local,cluster,distributed}`) covers every deployment shape. The current `loom service up/down/status` becomes `loom deploy local up/down/status` (kept as alias).
2. The Ascent target works end-to-end: `loom deploy cluster up --nodes <hostfile>` on a workstation provisions a working cluster on the 4 boxes, prints the admin token, and the CLI / SPA / `loom eval` commands can talk to it.
3. Multi-team users work through per-team tokens (#49's framing) without an IDP. Provider connections are team-scoped, encrypted at rest, and never injected into sandbox containers.
4. The CLI pipeline is the **primary surface** for the foreseeable future. The SPA is paused. Every workflow that users have via the SPA today (browse benchmarks, submit a batch, monitor, fetch trajectory/ATIF) must be available as a CLI command before the SPA work resumes.
5. The deployment story does not assume the operator has GPU hardware — Loom orchestrates; users bring their own model endpoints.

## Non-goals

- ARM-rebuilding SWE-Bench / OSWorld / skill-* images. Tracked as a separate sub-project; for now those benchmarks are off the menu on ARM nodes.
- Per-user identity (OIDC / SAML / Active Directory). Hard line per the topology decision.
- Distributed Postgres / MinIO HA. The Strategy C skeleton documents the gap; do not block any other phase on it.
- Worker types other than `docker` driver. Daytona / Modal / fake stay in tree but are not part of this rollout.
- Hot-reload of the cluster across `loom deploy cluster up` cycles (the bootstrap is idempotent but not in-place rolling).

## Topology taxonomy

Three selectable strategies. Each is a complete answer to "where does Loom run." Storage is an **orthogonal flag**.

```
┌─ A: Single-Box ───┐     ┌─ B: Cluster (single-storage) ─┐     ┌─ C: Distributed (HA) ──┐
│ everything on one │     │ control box + N workers       │     │ k8s + HA components    │
│ machine           │     │ embedded Postgres + MinIO     │     │ Postgres replication   │
│ docker-compose    │     │ on control box; workers       │     │ MinIO distributed mode │
│                   │     │ are pure compute              │     │ multi-replica everywhere│
│ Use: dev, demo,   │     │ Use: 2–10 box deployments     │     │ Use: large prod,        │
│ one user, one host│     │ (your Ascent cluster)         │     │ uptime-critical         │
└───────────────────┘     └───────────────────────────────┘     └─────────────────────────┘
   IMPLEMENTED                  PARTIAL (`deploy/k8s/*`)              FUTURE (skeleton only)
```

**Storage dimension** (independent of strategy):

| `--storage` | Postgres | MinIO | Compatible with |
|---|---|---|---|
| `embedded` (default for A & B) | in-cluster, single PVC | in-cluster, single PVC | A, B |
| `external` (required for C) | managed (Cloud SQL / RDS / Aurora / self-hosted PG cluster) | S3 / GCS / Azure Blob / on-prem object store | B (opt-in), C (required) |

Ascent cluster install: `loom deploy cluster up --nodes hostfile.txt --storage embedded`. Future prod: `loom deploy distributed up --storage external --postgres-url ... --s3-bucket ...`.

### Topology diagram (Strategy B, your Ascent cluster)

```
┌─────────────── Box 1 (control box) ───────────────┐    ┌── Box 2 / 3 / 4 (pure workers) ──┐
│  postgres (StatefulSet, 1 replica, local PVC)      │    │  loom-worker (DaemonSet pod)     │
│  minio    (StatefulSet, 1 replica, local PVC)      │ ←→ │  docker.sock                     │
│  loom-service (Deployment, 2 replicas)             │200G│  /tmp tempdirs (local NVMe)      │
│  loom-control-plane (Deployment, 2 replicas)       │bps │  /var/lib/loom/trajectories      │
│  loom-llm-gateway (Deployment, 2 replicas)         │    │  (local NVMe, transient)         │
│  loom-web (Deployment, 2 replicas)                 │    │  HF cache (local NVMe)           │
│  loom-worker (DaemonSet pod — control box is also  │    └──────────────────────────────────┘
│              a worker)                             │
│  docker.sock                                       │
│  /tmp tempdirs (local NVMe)                        │
│  /var/lib/loom/trajectories (local)                │
│  HF cache (local NVMe)                             │
└────────────────────────────────────────────────────┘
```

Rationale for the "no NFS" choice on this hardware: 4 TB local NVMe per box + 200 Gbps interconnect. NFS would be slower than reading objects from MinIO over the interconnect, and we'd take on NFS failure modes (stale handles, locking quirks) for no win.

## CLI surface

### New verb family

```
loom deploy local        {up,down,status}  [--compose-file PATH] [--env-file PATH]
loom deploy cluster      {up,down,status}  --nodes HOSTFILE [--control-node HOST] [--kubeconfig PATH] [--storage embedded|external] [--registry URL] [--namespace NS]
loom deploy distributed  {up,down,status}  --kubeconfig PATH --storage external [--postgres-url URL] [--s3-bucket NAME] [--s3-endpoint URL]
```

`loom service` stays as an alias of `loom deploy local` (deprecation warning printed in v1+, no functional change).

### User-facing CLI from #49 (Phase 3 of this spec)

```
loom auth login --server https://loom.example.com           # interactive token paste OR --token / env
loom auth status                                             # whoami / current server / token type
loom auth logout

loom providers create --name NAME --type openai-compatible \
    --base-url URL --api-key env:VAR [--model M [...]] [--allowed-models LIST]
loom providers list
loom providers show NAME
loom providers test NAME                                     # POST /provider-connections/{id}/test
loom providers models NAME                                   # GET  /provider-connections/{id}/models
loom providers update NAME --base-url URL                    # PATCH
loom providers delete NAME

loom eval run --provider NAME --model M --agent A --benchmark B \
    [--task ID | --task-filter JSON] [--backend B] [--name N]
loom eval batch create --provider NAME --model M --agent A --benchmark B \
    [--combinations FILE] [--task-filter JSON] [--concurrency N] [--name N]
loom eval batch list [--state S]
loom eval batch show ID
loom eval batch cancel ID
loom eval trial list --batch ID
loom eval trial show ID
loom eval trial trajectory ID [--out PATH]
loom eval trial atif ID [--out PATH]
```

`loom eval` talks to the cluster's loom-service. `loom run` (existing) is the **local-stateless** path (no cluster needed). Two verbs because the workflows are genuinely different — one launches one trial on the local machine, the other submits to a service.

### Existing commands kept verbatim

| Command | Status |
|---|---|
| `loom run` | unchanged — local-stateless, no cluster |
| `loom datasets {list,show,install,refresh-catalog,import,publish,register,verify}` | unchanged (modular-D shipped) |
| `loom config {set,show}` | unchanged |
| `loom serve` | unchanged |
| `loom service {up,down,status}` | alias of `loom deploy local {up,down,status}` |
| `python -m loom_benchmark_tool ...` | deprecation shim (modular-D) |

## Schema changes

### New tables: `provider_connections` + `provider_models_cache`

Lifted from #49 verbatim. Migration `0018_provider_connections.py` (down_revision = `"0017"` per the latest existing revision).

```python
# provider_connections
id: UUID PK
team_id: UUID FK → teams.id  ON DELETE CASCADE
provider_type: str  # 'openai-compatible' | 'anthropic' | 'google' | 'custom'
display_name: str  # unique per (team_id, display_name)
base_url: str
encrypted_api_key_ref: str   # opaque reference; concrete shape is the secrets backend's
allowed_models: list[str]    # JSONB; null = "all the provider returns"
status: str   # 'pending' | 'valid' | 'invalid' | 'disabled'
last_validated_at: datetime | None
last_validation_error: str | None
created_by: str  # subject (token type + id prefix) for audit
created_at, updated_at: timestamptz

# provider_models_cache
provider_connection_id: UUID FK → provider_connections.id ON DELETE CASCADE
model_id: str   # composite PK with provider_connection_id
family: str | None
context_length: int | None
capabilities: dict   # JSONB, free-form
visible: bool          # default true; operator-toggleable
hidden_reason: str | None
last_seen_at: timestamptz
```

### Secrets backend abstraction

Single `SecretStore` Protocol in `src/loom/security/secret_store.py`:

```python
class SecretStore(Protocol):
    def put(self, *, namespace: str, key: str, value: str) -> str: ...   # returns ref
    def get(self, ref: str) -> str: ...
    def delete(self, ref: str) -> None: ...
```

Implementations (selected via `LOOM_SECRET_STORE`):
- `local-encrypted` (default for Strategy A / B-embedded): AEAD with a key derived from a single operator-supplied master secret (`LOOM_SECRET_STORE_MASTER_KEY`); ciphertext stored in `secrets` table.
- `k8s-secret`: writes a k8s Secret in the loom namespace; ref = `k8s://namespace/secret-name`.
- `vault` / `aws-kms` / `gcp-kms`: stubbed Protocol; not implemented in this spec.

### TrialConfig / Batch payload extension

```
TrialConfig (existing) gains:
  provider_connection_id: UUID | None   # null = use the platform default provider (env)
  provider_model_id: str | None

Batch._CreateBatch gains:
  provider_connection_id: UUID | None
  provider_model_id: str | None
  # OR per-Combination override (one-of, route validates)
```

When non-null, the gateway resolves at call time: looks up the connection by id + team_id (enforces team scoping), decrypts the API key via the secrets backend, forwards the call, writes the usage row.

## K8s manifest changes (Strategy B)

### `deploy/k8s/worker.yaml`: Deployment → DaemonSet

Today: `Deployment` with `replicas: 3` (can stack 3 workers on one node). Change to `DaemonSet` (one worker pod per node). Add nodeSelector for "node has docker.sock" if mixed-arch clusters become a thing.

```yaml
apiVersion: apps/v1
kind: DaemonSet
metadata: {name: loom-worker, namespace: loom}
spec:
  selector: {matchLabels: {app: loom-worker}}
  template:
    spec:
      containers:
        - name: worker
          image: ${REGISTRY}/loom-worker:${VERSION}
          env:
            - {name: LOOM_WORKER_CONTROL_PLANE_URL,  value: "http://control-plane:8080"}
            - {name: LOOM_WORKER_GATEWAY_URL,         value: "http://llm-gateway:9100"}
            - name: LOOM_WORKER_TOKEN
              valueFrom: {secretKeyRef: {name: loom-secrets, key: worker-token}}
            - {name: LOOM_WORKER_BENCHMARK_CACHE,     value: "/var/lib/loom/benchmarks"}
            - {name: LOOM_WORKER_TRAJECTORY_CACHE,    value: "/var/lib/loom/trajectories"}
          volumeMounts:
            - {name: docker-sock, mountPath: /var/run/docker.sock}
            - {name: bench-cache, mountPath: /var/lib/loom/benchmarks}
            - {name: trajectory-cache, mountPath: /var/lib/loom/trajectories}
      volumes:
        - name: docker-sock      {hostPath: {path: /var/run/docker.sock}}
        - name: bench-cache      {hostPath: {path: /var/lib/loom/benchmarks, type: DirectoryOrCreate}}
        - name: trajectory-cache {hostPath: {path: /var/lib/loom/trajectories, type: DirectoryOrCreate}}
```

`hostPath` for `bench-cache` + `trajectory-cache` keeps them on local NVMe per node (the rationale above). Each box pays its own first-snapshot cost; after that, all subsequent trials on that box read from the local NVMe.

### New `deploy/k8s/bootstrap-job.yaml`

One-shot k8s `Job` that:

1. Runs `alembic upgrade head` against the Postgres in the cluster.
2. Checks if any `admin` token exists. If yes, no-op (idempotent).
3. If no, mints a fresh admin + worker token, writes them to a k8s Secret (`loom-bootstrap-tokens`) the operator reads with `kubectl get secret`.
4. Logs the admin token id (not value) for audit.

```yaml
apiVersion: batch/v1
kind: Job
metadata: {name: loom-bootstrap, namespace: loom}
spec:
  backoffLimit: 3
  template:
    spec:
      restartPolicy: OnFailure
      containers:
        - name: bootstrap
          image: ${REGISTRY}/loom-service:${VERSION}
          command: ["python", "-m", "loom_service.bootstrap"]
          env:
            - name: LOOM_SVC_DB_URL
              valueFrom: {secretKeyRef: {name: loom-secrets, key: postgres-url}}
            - {name: LOOM_BOOTSTRAP_SECRET_NAME, value: "loom-bootstrap-tokens"}
            - {name: LOOM_BOOTSTRAP_NAMESPACE,   value: "loom"}
```

The Job is gated on the postgres StatefulSet being ready (`initContainer` doing `pg_isready` is enough; no need for k8s native dependencies).

### Other manifest tweaks

- `loom-service.yaml`, `control-plane.yaml`, `llm-gateway.yaml`: already 2 replicas. Add `topologySpreadConstraints` so they spread across nodes.
- `postgres.yaml`, `minio.yaml`: keep `replicas: 1`. Add `nodeSelector: loom.io/role: control` so they pin to the designated control box.
- New label convention: every node has `loom.io/role` ∈ `{control, worker}`. `loom deploy cluster up` labels the control node automatically.

## Reject-batch-when-no-worker

Today: `POST /batches` with `backend: "modal"` (or any backend no live worker advertises) succeeds; the batch sits forever because no worker can claim its trials. The SPA already shows `available: false` on the dropdown but the route doesn't enforce it.

Add to `src/loom_service/routes/batches.py:269+` (`create_batch`):

```python
# Reject upfront if no live worker advertises the chosen backend.
# Mirrors the rule the SPA already shows on /batches/new.
live_workers = (await s.execute(
    select(Worker.capabilities)
    .where(Worker.status == "active")
)).scalars().all()
advertised = {
    cap.get("backend", "docker")
    for caps_list in live_workers
    if isinstance(caps_list, list)
    for cap in caps_list
    if isinstance(cap, dict)
}
if payload.backend not in advertised:
    raise HTTPException(
        status_code=400,
        detail=(
            f"no live worker advertises backend={payload.backend!r}. "
            f"Available right now: {sorted(advertised) or '(none)'}"
        ),
    )
```

Edge case: admin tokens may want to schedule "ahead of time" against a backend they're about to provision. Add `--force-no-worker-check` to the CLI; route gates on `is_admin(ctx)` for that override.

## Token race fix (option a)

Today `loom service up`:
1. `docker compose up` — containers boot with **stale** `LOOM_*_TOKEN` from `.env`
2. `alembic upgrade head`
3. `seed_test_data.py` runs; produces fresh tokens; `_write_env_tokens` updates `.env` (PR-35)
4. Worker has already crashed in step 1 with the old token; keeps restarting

Fix in `src/loom_cli/service_cmd.py:172+` (`_up`): after step 3, recreate the worker container so it picks up the new `.env`:

```python
# Recreate worker so it reads the fresh LOOM_WORKER_TOKEN that
# `_write_env_tokens` just wrote. `docker restart` reuses the old
# env vars; only `up --force-recreate` re-reads `.env`.
_run([
    *_compose_args(compose_file, env_file),
    "up", "-d", "--force-recreate", "--no-deps", "worker",
], check=False)
```

Same fix lives in `loom deploy local up` after the refactor.

## Implementation phases — round-by-round

Each phase = one PR (or a small stack). Phase ordering enforces dependencies; phases marked **independent** can ship in parallel.

### Phase 0 — this spec (ship as a PR for review)

- New file: `docs/architecture/cluster-deploy.md` (this document) with "Status: design"
- Update `docs/architecture/service-mode.md` to point at this doc
- No code changes

### Phase 1 — `loom deploy` skeleton + token race fix

- New: `src/loom_cli/deploy_cmd.py` exporting `dispatch(argv)` (mirrors `datasets_cmd.dispatch`)
- New: `loom deploy local {up,down,status}` — calls existing `service_cmd._up/_down/_status` internally
- New: `loom deploy cluster {up,down,status}` — **stubbed** (prints "not implemented; see Phase 2")
- New: `loom deploy distributed {up,down,status}` — **stubbed** (Phase 5)
- Modify: `src/loom_cli/__main__.py:main` — route `argv[0] == "deploy"` to `deploy_cmd.dispatch`
- Modify: `service_cmd._up` — add the `--force-recreate worker` recreate after seed completes (token race fix `(a)`)
- Tests:
  - `tests/loom_cli/test_deploy_cmd.py` — argparse, dispatch table, stubs raise SystemExit(2) for unimplemented strategies
  - `tests/loom_cli/test_service_cmd.py` — extend to assert the worker-recreate call happens after `_write_env_tokens`
- Backward compat: `loom service up` still works; prints no warning in this phase (deprecation banner is Phase 5)

### Phase 2 — `loom deploy cluster up` against Ascent boxes

**Independent of Phase 3.**

- Modify: `deploy/k8s/worker.yaml` — Deployment → DaemonSet; add benchmark_cache + trajectory_cache hostPaths
- Modify: `deploy/k8s/postgres.yaml`, `minio.yaml` — `nodeSelector: loom.io/role: control`
- New: `deploy/k8s/bootstrap-job.yaml`
- New: `src/loom_service/bootstrap.py` — entry point for the Job (alembic + token mint + write to k8s Secret)
- Modify: `src/loom_service/routes/batches.py` — reject submit when no live worker advertises the chosen backend
- New: `src/loom_cli/deploy_cluster.py` — implementation of `loom deploy cluster up`:
  - Read `--nodes hostfile.txt`; default control-node = first
  - Label nodes via `kubectl label`
  - Apply k8s manifests in order (Secrets → PVCs → Postgres → MinIO → bootstrap Job → service/CP/gateway/web → worker DaemonSet)
  - Wait for bootstrap Job to complete
  - Extract admin token from `loom-bootstrap-tokens` Secret, print to stdout
- Tests:
  - Integration: bring up a kind cluster in CI, `loom deploy cluster up --kubeconfig $KIND_KUBECONFIG --nodes <fake hostfile>`, assert admin token is printed + a smoke `POST /batches` works
  - Unit: bootstrap.py is idempotent (re-run is a no-op)
- Docs: `docs/operator-runbook.md` adds a "cluster deploy" section

### Phase 3 — `loom auth` + `loom providers` + `loom eval`

**Independent of Phase 2.**

- Migration: `0018_provider_connections.py` (down_revision = "0017")
- New schema in `src/loom/db/schema.py`: `ProviderConnection`, `ProviderModelCache`
- New: `src/loom/security/secret_store.py` — Protocol + 2 impls (`local-encrypted`, `k8s-secret`)
- New routes in `src/loom_service/routes/provider_connections.py`:
  - `POST   /api/v1/provider-connections` (create, encrypt + store secret)
  - `GET    /api/v1/provider-connections` (list team's)
  - `GET    /api/v1/provider-connections/{id}` (show)
  - `PATCH  /api/v1/provider-connections/{id}` (update)
  - `DELETE /api/v1/provider-connections/{id}`
  - `POST   /api/v1/provider-connections/{id}/test` (validation)
  - `GET    /api/v1/provider-connections/{id}/models` (refresh + return cache)
- Modify: `loom_llm_gateway` — resolve provider connection at call time when `TrialConfig.provider_connection_id` is set; decrypt key via SecretStore; record usage
- Modify: `src/loom/models/trial.py`, `src/loom_service/routes/batches.py` — accept `provider_connection_id` + `provider_model_id`
- New CLI:
  - `src/loom_cli/auth_cmd.py` — `loom auth {login,status,logout}`. Login: interactive token paste OR `--token` OR `LOOM_TOKEN` env. Writes to `~/.config/loom/config.toml`.
  - `src/loom_cli/providers_cmd.py` — wraps the 7 routes above
  - `src/loom_cli/eval_cmd.py` — `loom eval {run, batch create/list/show/cancel, trial list/show/trajectory/atif}` — wraps the `POST /trials`, `POST /batches`, etc. routes
- Tests:
  - Integration: spin up the service, exercise the full `loom auth login → providers create → eval batch create → eval batch show` flow
  - Security: `loom providers show` MUST NOT echo the API key; route response MUST NOT echo it
  - SSRF: `base_url` validation rejects `localhost`, `169.254.169.254`, `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`, link-local

### Phase 4 — operator runbook + smoke test

**After Phase 2 + Phase 3.**

- Promote `cluster-deploy.md` from "Status: design" → "Status: shipped"
- Write `docs/cluster-deploy-runbook.md`: hostfile format, secrets prep, first-deploy walk-through, common ops (rotate admin token, drain a node, restart workers)
- CI smoke: kind cluster + `loom deploy cluster up` + sample `loom eval run` against a fake provider

### Phase 5 — `loom deploy distributed` skeleton

**Independent; can ship anytime after Phase 2.**

- Modify: `src/loom_cli/deploy_cmd.py` — `loom deploy distributed up` implementation that requires `--storage external`, accepts `--postgres-url`, `--s3-bucket`, `--s3-endpoint`
- New: `deploy/k8s/distributed/*.yaml` — manifests with the Postgres + MinIO StatefulSets removed (uses external)
- Document HA gaps explicitly in `cluster-deploy.md` as a follow-up roadmap

### Phase 6 — deprecate `loom service`

After Phase 1 has been in `dev` for at least one release.

- `loom service up` prints "deprecated: use `loom deploy local up`" to stderr (still functional)
- Drop in v2

## Risks + mitigations

| Risk | Severity | Mitigation |
|---|---|---|
| ARM rebuilds for benchmarks block usability | HIGH | Out of scope per the user's call; document the supported-benchmark matrix per arch. Strategy B docs explicitly list "pure-Python benchmarks only on ARM workers." |
| K8s adoption burden on operators new to k8s | MEDIUM | `loom deploy cluster up` wraps `kubectl apply` so operators don't need to know k8s details. Runbook in Phase 4 walks through node labelling + Secrets prep. |
| Bootstrap Job races against StatefulSet readiness | MEDIUM | `initContainer` runs `pg_isready` against the postgres service before alembic. |
| Worker DaemonSet stacks on the control node + interferes with Postgres | MEDIUM | DaemonSet has no anti-affinity by default. Recommendation: don't run workloads on the control node initially. If needed, add `tolerations` + `nodeSelector` to exclude control-role nodes from the worker DaemonSet (1-line manifest tweak). |
| SSRF via `provider_connections.base_url` | HIGH | Route-level allowlist matching #49's security controls. Egress-proxy mode for prod (Phase 5 tracker). |
| API key leakage in logs / trajectory | HIGH | Three-layer: SecretStore never returns raw key to anything but the Gateway forwarder; Gateway redacts in usage records; trajectory writer scrubs known secret-prefix patterns. |
| Token race fix (a) adds 3 s to every `loom deploy local up` | LOW | Acceptable; logs the recreate event. Skippable with `--no-recreate-worker` flag for power users. |
| `loom eval` and `loom run` diverge in surprising ways | MEDIUM | Document explicitly in CLI help: "`loom run` is local-stateless (one trial, no cluster); `loom eval` talks to a deployed Loom cluster (batches, persistence, sharing)." Cross-link in docs. |
| Strategy C never ships and Strategy B's single-storage failure mode is the only thing operators ever see | MEDIUM | Document the failure mode + RPO in Phase 4 runbook. Make Strategy B's backup story explicit (`kubectl exec ... pg_dump` cron + `mc mirror` for MinIO). |

## Open questions for review

1. **`loom deploy local` vs. keeping `loom service`**: the rename is mostly cosmetic (loom service is an alias). Worth doing if we believe the user mental model improves; not worth it if it churns existing scripts. **Recommendation:** rename, since the rest of the family is `loom deploy {cluster,distributed}`.
2. **Bootstrap Job idempotency contract**: should it overwrite the existing admin token Secret on re-run, or refuse? **Recommendation:** refuse by default, with `--rotate` flag for explicit rotation.
3. **`loom eval` shape — verbs**: `loom eval batch create` vs. `loom eval submit`. **Recommendation:** match #49's example: `loom eval run` (single) and `loom eval batch create` (multi). Even though it's verbose, it's discoverable.
4. **Where to enforce per-team quotas on provider calls**: today, `TeamQuota` has `fair_share_weight` for trial scheduling, not for gateway tokens. **Recommendation:** add `daily_token_budget` + `monthly_cost_budget_usd` fields to `TeamQuota` in Phase 3; Gateway checks before forwarding.
5. **Provider connection: scope of `created_by`**: log the token-hash prefix for audit? The token type (`team` / `admin`)? Both? **Recommendation:** both, comma-joined: `created_by = "admin:7a3f...:9b8e"` (type:prefix:token-id-suffix).
6. **Single secrets master key location for Strategy A**: today `.env` would hold `LOOM_SECRET_STORE_MASTER_KEY`. For Strategy B, k8s Secret. Should the CLI generate one if missing? **Recommendation:** yes, `loom deploy local up` mints if absent, writes to `.env`.
7. **CI bandwidth**: kind cluster startup in CI for Phase 2 smoke is ~2 min. Acceptable on the existing `integration` job? **Recommendation:** gate behind the `ci:integration` label so PRs not touching deploy code don't pay the cost.

## Appendix A — `loom deploy cluster up` reference walkthrough

Operator workflow on a fresh set of 4 Ascent boxes:

```bash
# Once per cluster (or any time secrets rotate):
cat > hostfile.txt <<EOF
control:  ascent-0.lab.local
worker:   ascent-1.lab.local
worker:   ascent-2.lab.local
worker:   ascent-3.lab.local
EOF

# Optional: prep your own Secrets (otherwise loom deploy mints defaults).
kubectl create namespace loom
kubectl create secret generic loom-secrets -n loom \
    --from-literal=postgres-user=loom \
    --from-literal=postgres-password=$(openssl rand -hex 16) \
    --from-literal=minio-access-key=$(openssl rand -hex 12) \
    --from-literal=minio-secret-key=$(openssl rand -hex 24)

# Then:
loom deploy cluster up \
    --nodes hostfile.txt \
    --kubeconfig ~/.kube/config-ascent \
    --registry ghcr.io/myorg \
    --storage embedded

# Output:
# → labeling nodes (control: ascent-0, workers: ascent-1/2/3)
# → applying manifests (postgres, minio, bootstrap-job, …, worker DaemonSet)
# → waiting for bootstrap-job to complete (45 s)
# ✓ cluster ready
#
# Admin token (paste into `loom auth login --server https://...`):
#   loom_admin_XXXXXXXXXXXXXXXXXXXXXXX
#
# Endpoints:
#   SPA:        https://loom.ascent.lab.local
#   API:        https://loom.ascent.lab.local/api/v1
#   Gateway:    https://gateway.ascent.lab.local (internal)
```

After this:

```bash
loom auth login --server https://loom.ascent.lab.local
# (paste the admin token)

loom providers create --name openai-prod \
    --type openai-compatible \
    --base-url https://api.openai.com/v1 \
    --api-key env:OPENAI_API_KEY \
    --model gpt-4o

loom eval run \
    --provider openai-prod --model gpt-4o \
    --agent claude-code-inbox --backend docker \
    --benchmark humaneval --task-filter '{"subset_kind":"random_n","n":3,"seed":42}'
```

## Appendix B — Supported-benchmark matrix per arch (initial)

| Benchmark | ARM (Ascent) | x86 |
|---|---|---|
| HumanEval, MBPP, LiveCodeBench, BFCL | ✅ | ✅ |
| AIME-22, AIME-23, AIME-24, AIME-25 | ✅ | ✅ |
| GAIA | ✅ (needs HF auth) | ✅ |
| WebArena | ⚠️ requires Playwright ARM rebuild | ✅ |
| SWE-Bench / SWE-Bench Verified / SWE-Bench Multimodal | ❌ (x86-only eval images) | ✅ |
| OSWorld | ❌ (x86 VM images) | ✅ |
| skillflow, skilllearnbench | ❌ (per-task Dockerfiles, not yet ARM-built) | ⚠️ adapter rewrite pending |

To run x86-only benchmarks: rack one x86 worker, label it `loom.io/arch: amd64`, and the worker DaemonSet's `nodeSelector` matches by arch. Strategy B supports mixed-arch clusters (k8s spreads workers across architectures naturally). Out of scope for the initial Ascent deploy.

## See also

- [#49](https://github.com/carinrc/issues/49) — Production cluster deployment with user-supplied model provider gateway
- [service-mode.md](service-mode.md) — current single-host architecture
- [drf-scheduling.md](drf-scheduling.md) — how the claim path matches workers to trials
- [overview.md](overview.md)
