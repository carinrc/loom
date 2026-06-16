# Cluster deployment design

**Status: design** (2026-06-15, revision 2). Tracking PR: [#50](https://github.com/carinrc/loom/pull/50). Supersedes inline guidance in `docs/architecture/service-mode.md`. Revision 2 addresses review feedback on PR #50 (see "Changelog" at bottom).

Extends [#49](https://github.com/carinrc/loom/issues/49). Initial deployment target is 2–4× ASUS Ascent GX10 (ARM v9.2-A, GB10 Grace Blackwell, 128 GB unified, 4 TB local NVMe, 200 Gbps ConnectX-7), but **the design is not coupled to that hardware** — Loom is a platform; the deployment shape is selectable at deploy time.

## Status block

| Item | State |
|---|---|
| Hardware target (initial) | 2–4× ASUS Ascent GX10 (ARM v9.2-A, GB10) |
| Hardware target (future) | x86 servers, cloud k8s, hybrid |
| ARM-rebuild benchmarks (SWE-Bench, OSWorld, …) | **deferred** — documented as a gap (Appendix B) |
| Local vLLM / `hf_execution=local-vllm` | **dropping** — users supply OpenAI-compatible endpoints |
| Auth model | per-team token (today's model) — **no per-user IDP** for now |
| Topology | two verbs: `loom service` (single-box) + `loom cluster` (k8s, 2–N nodes); HA Postgres/MinIO via `--storage external` flag |
| HA Postgres / MinIO | **deferred** — `--storage external` skeleton + documented gap |

## Goals

1. The Ascent target works end-to-end. `loom cluster up --nodes hostfile` on a workstation provisions a working cluster on N boxes, prints an admin token, and the CLI/SPA can talk to it.
2. Multi-team users work through per-team tokens (#49's framing) without an IDP. Provider connections are team-scoped, encrypted at rest, and never injected into sandbox containers.
3. The CLI pipeline is the **primary surface**. Every SPA workflow (browse benchmarks, submit batch, monitor, fetch trajectory/ATIF) is available as a CLI command before SPA work resumes.
4. The deployment story does not assume the operator has GPU hardware — Loom orchestrates; users bring their own model endpoints.
5. **Data durability is design-in, not bolted on.** Trajectories survive any single-node failure on day 1.

## Non-goals

- ARM-rebuilding SWE-Bench / OSWorld / skill-* images (tracked separately).
- Per-user IDP (OIDC/SAML).
- Distributed Postgres / MinIO HA implementations (the `--storage external` flag lets operators bring managed equivalents).
- Worker types other than `docker` driver.
- Rolling upgrades. `loom cluster up` is idempotent but reapply is not zero-downtime.

## Topology

Two verbs. Storage is an orthogonal flag.

```
┌─ loom service ───────┐     ┌─ loom cluster ─────────────────┐
│ everything one box   │     │ control node + N worker nodes  │
│ docker-compose       │     │ k8s manifests in deploy/k8s/   │
│ dev, demo, one user  │     │ 2–N boxes; embedded or external│
│                      │     │ Postgres+MinIO                 │
└──────────────────────┘     └────────────────────────────────┘
   IMPLEMENTED                   PARTIAL (deploy/k8s/*)
```

### Storage dimension (orthogonal to verb)

| `--storage` | Postgres | Object store | Compatible with |
|---|---|---|---|
| `embedded` (default) | in-cluster, StatefulSet, single PVC | in-cluster MinIO, single PVC | `service`, `cluster` |
| `external` | managed (Cloud SQL / RDS / self-hosted PG cluster) | S3 / GCS / on-prem object store | `cluster` (opt-in for HA) |

`--storage external` is the **only** path to HA Postgres/MinIO; Loom does not ship its own replicated state. This is intentional — operators with HA requirements already have opinions about their Postgres + S3.

### Topology diagram (cluster, embedded storage — your Ascent layout)

```
┌────────── Control node (ascent-0, tainted) ──────────┐    ┌── Worker node × N ──────────────┐
│  postgres   (StatefulSet, 1 replica, local PVC)       │    │  loom-worker (DaemonSet pod)    │
│  minio      (StatefulSet, 1 replica, local PVC)       │    │  docker.sock (hostPath)         │
│  loom-service / control-plane / llm-gateway / web     │    │  bench-cache  (hostPath, READ-  │
│      (Deployments, ≥2 replicas, spread)               │ ←→ │    THROUGH cache of MinIO)      │
│  loom-worker (DaemonSet pod, OPT-IN via toleration)   │200G│  trajectory-cache (hostPath,    │
│  ingress controller (nginx)                           │bps │    WRITE-THROUGH to MinIO)      │
└───────────────────────────────────────────────────────┘    └─────────────────────────────────┘
```

**Key data invariant:** MinIO is the source-of-truth for benchmarks + trajectories. Per-node hostPaths are *caches*, not authoritative copies. If a worker node dies, its hostPath is gone — the data is intact in MinIO. This is the change from rev 1 of this spec, which incorrectly treated hostPath as canonical.

## Data architecture (canonical vs cached)

| Data | Canonical home | Cache layer |
|---|---|---|
| Benchmark task tree (instructions, tests, environment) | MinIO bucket `benchmarks/<slug>/<version>/` | per-worker `/var/lib/loom/benchmarks/<slug>/<version>/` (read-through, populated on first task touch) |
| Trial trajectory `events.jsonl` | MinIO bucket `trajectories/<trial_id>/` (existing) | per-worker `/var/lib/loom/trajectories/<trial_id>/` (write-through during run; can be evicted after upload completes) |
| ATIF projection JSON | MinIO bucket `atif/<trial_id>/` (existing) | none |
| Postgres rows (trials, batches, workers, …) | Postgres on control node | none |

**Write-through model for trajectories**: the `TrajectoryWriter` continues to append to the local file (low-latency); on every event flush it also enqueues a multipart-upload to MinIO. On trial completion, complete the upload. Node failure during a run loses partial trajectory; cluster failure during a run still loses partial trajectory; trajectory of any *completed* trial is in MinIO. Recovery model: the partial-but-not-completed case is a known gap, equivalent to today's behavior.

**Read-through model for benchmarks**: worker checks `bench-cache/<slug>/<version>/.complete`; if missing, downloads from MinIO and atomically renames `.tmp` → final + writes the marker. Concurrent first-fetches: per-slug-version flock prevents thundering herd. Eviction: simple LRU when `/var/lib/loom/benchmarks/` exceeds an operator-configurable quota.

This is roughly the only sustainable model on a 4-box cluster: 16 benchmarks × potentially-multi-TB datasets would be brutal at 4× duplication, and shared NFS reintroduces locking quirks the spec rev 1 correctly rejected.

## Network stack

`loom cluster` requires a working network layer. The spec ships a default that's enough for Ascent-class clusters; operators with opinions bring their own.

| Concern | Default (Phase 2) | Operator-supplied alternative |
|---|---|---|
| CNI | inherit cluster's (k3s ships flannel; kubeadm ships calico/cilium) | any CNI |
| Ingress | `ingress-nginx` Helm chart, installed by `loom cluster up` if absent | Traefik / Contour / cloud LB — operator passes `--ingress none` and applies their own |
| Service mesh | none | Istio/Linkerd opt-in via `--ingress none` |
| Internal DNS | k8s CoreDNS (default) | unchanged |
| External DNS | operator points wildcard `*.loom.<domain>` at ingress LB IP | required, manual |
| TLS | self-signed cert from a generated CA (private deployments) OR operator-supplied cert (`--tls-cert PATH --tls-key PATH`) OR `cert-manager` + ACME if operator has it | cert-manager + Let's Encrypt for public domains |
| Egress proxy (for SSRF defense, see Secrets §) | required; deployed alongside the gateway | operator-supplied (Squid/Envoy) |

**Self-signed CA flow**: `loom cluster up` generates a CA + server cert if `--tls-cert` not given, writes the CA cert to stdout + a k8s ConfigMap. Operators distribute the CA cert to clients (or set `LOOM_TLS_VERIFY=false` on CLI for dev). Explicit operator action; not silent.

**External DNS is required** — there's no service discovery magic that solves "operator's laptop talks to the cluster's loom-service over HTTPS." The CLI's `loom auth login --server https://...` URL has to resolve. The runbook (Phase 4) walks through wildcard DNS + ingress LB IP for the Ascent setup (typically a static IP on the control node's 10 GbE NIC; metallb in ARP mode for >1 node).

## Image pipeline (multi-arch)

Phase 2 requires ARM-built images. Current CI is amd64-only. Phase 2 prereq:

1. **Buildx multi-arch CI**: `.github/workflows/images.yml` adds `linux/arm64` to the platforms matrix for `loom-worker`, `loom-service`, `loom-control-plane`, `loom-llm-gateway`, `loom-web`.
2. **Registry**: push to GHCR (`ghcr.io/carinrc/loom-*`). Operators can mirror to internal registries via `--registry` flag on `loom cluster up`.
3. **Manifest list**: each image is a multi-arch manifest. `nodeSelector` is NOT used on the worker DaemonSet for arch — k8s picks the right arch from the manifest list per node.
4. **Bootstrap entrypoint**: ships in the existing `loom-service` image (just adds `python -m loom_service.bootstrap`); no new image.

Out of scope for Phase 2: ARM rebuilds of *user* benchmark/task images (SWE-Bench eval, OSWorld VM). Appendix B documents the gap.

## CLI surface

### Deployment verbs

```
loom service {up,down,status,logs}                                    # single-box (existing, unchanged)
loom cluster {up,down,status}  --nodes HOSTFILE
    [--control-node HOST]                                              # default: first in hostfile
    [--kubeconfig PATH]                                                # default: $KUBECONFIG
    [--namespace NS]                                                   # default: loom
    [--storage embedded|external]                                      # default: embedded
    [--postgres-url URL] [--s3-endpoint URL] [--s3-bucket NAME]        # required if storage=external
    [--registry URL]                                                   # default: ghcr.io/carinrc
    [--ingress nginx|none] [--tls-cert PATH --tls-key PATH]
    [--co-locate-workers-on-control]                                   # opt-in; otherwise control node is tainted
```

**Rename decision (revised):** keep `loom service` (single-box). Add `loom cluster` for k8s. No unified `loom deploy` verb family. Reasons: less churn on existing docs/scripts, asymmetric verbs match other tools (`docker run` vs `docker-compose up`), and `service` accurately describes the single-box mode (it IS a service, not a deployment). Reversed from rev 1.

### User-facing CLI (Phase 2 — before cluster work)

`loom auth`, `loom providers`, `loom eval` work against **any deployed Loom** — they don't require `loom cluster`. We ship them first so they're usable against an existing `loom service` install while the cluster work proceeds.

```
loom auth login --server URL [--token T | --token-file PATH]    # else interactive paste
loom auth status
loom auth logout

loom providers create --name N --type {openai-compatible,anthropic,google,custom} \
    --base-url URL --api-key {env:VAR | file:PATH | -}            # never on CLI args
    [--allowed-models LIST]
loom providers list / show NAME / test NAME / models NAME
loom providers update NAME --base-url URL                        # PATCH
loom providers delete NAME

loom eval run --provider N --model M --agent A --benchmark B
    [--task ID | --task-filter JSON] [--backend B] [--name N]
loom eval batch create --provider N --model M --agent A --benchmark B
    [--combinations FILE | --task-filter JSON] [--concurrency N] [--name N]
loom eval batch {list,show,cancel} [--state S]
loom eval trial {list,show} | trajectory ID [--out PATH] | atif ID [--out PATH]
```

`loom run` (existing, local-stateless) stays. The doc distinction:

> `loom run` runs one trial on your machine, no server required.
> `loom eval` submits to a Loom server (`loom auth login` first). Use `eval` for batches, persistence, sharing, ATIF.

### Existing commands kept verbatim

| Command | Status |
|---|---|
| `loom run` | unchanged — local-stateless, no cluster |
| `loom datasets {list,show,install,refresh-catalog,import,publish,register,verify}` | unchanged (modular-D shipped) |
| `loom config {set,show}` | unchanged |
| `loom serve` | unchanged |
| `python -m loom_benchmark_tool ...` | deprecation shim (modular-D) |

## Schema changes

### New tables (migration `0018_provider_connections.py`, down_revision = `"0017"`)

`provider_connections`:

```
id                          UUID PK
team_id                     UUID FK → teams.id  ON DELETE CASCADE
provider_type               str  -- 'openai-compatible' | 'anthropic' | 'google' | 'custom'
display_name                str  -- UNIQUE per (team_id, display_name)
base_url                    str
resolved_egress_ips         list[str] JSONB  -- DNS resolved at validation, re-checked on call
encrypted_api_key_ref       str   -- opaque ref into SecretStore
allowed_models              list[str] | None JSONB  -- null = "all"
status                      str   -- 'pending' | 'valid' | 'invalid' | 'disabled'
last_validated_at           timestamptz | None
last_validation_error       str | None
pricing_source              str   -- 'rate-card' | 'tokens-only' | 'operator-supplied'
pricing_data                dict | None JSONB  -- {input_usd_per_1m, output_usd_per_1m, ...}
created_by                  str   -- "<token-type>:<token-id-suffix>"
created_at, updated_at      timestamptz
```

`provider_models_cache`:

```
provider_connection_id      UUID FK → provider_connections.id  ON DELETE CASCADE
model_id                    str   -- (PK with provider_connection_id)
family                      str | None
context_length              int | None
capabilities                dict JSONB
visible                     bool DEFAULT true  -- operator-toggleable
hidden_reason               str | None  -- 'operator-hidden' | 'missing-upstream' | 'disabled-pricing'
last_seen_at                timestamptz
upstream_present            bool DEFAULT true  -- false after a refresh that didn't see this model
```

### Trial / Batch payload extensions

```
TrialConfig:
  provider_connection_id    UUID | None  -- null = platform default provider (env-keyed, today's behavior)
  provider_model_id         str | None

Batch._CreateBatch:
  provider_connection_id    UUID | None  -- one-of with per-Combination override
  provider_model_id         str | None

Trial DB row:
  provider_connection_id    UUID | None FK ON DELETE SET NULL
```

**Delete semantics:** if a user deletes a provider connection while a batch is in flight, in-flight trials continue with the cached decrypted key (held in Gateway LRU; see Secrets §); new trial claims after deletion fail-fast with `provider_connection_deleted` error. The FK is `SET NULL` so historical rows stay queryable.

## Secrets, SSRF, and the Gateway hot path

### `SecretStore` Protocol

`src/loom/security/secret_store.py`:

```python
class SecretStore(Protocol):
    def put(self, *, namespace: str, key: str, value: str) -> str: ...        # → ref
    def get(self, ref: str) -> str: ...
    def delete(self, ref: str) -> None: ...
    def list_refs(self, *, namespace: str | None = None) -> Iterator[str]: ...
    def rewrap(self, ref: str, *, new_master_key: bytes) -> str: ...           # returns new ref
```

`list_refs` + `rewrap` are mandatory from day 1 to make master-key rotation possible. `loom admin secrets rotate --new-master-key-file PATH` walks every ref, calls `rewrap`, atomically swaps `encrypted_api_key_ref` columns inside a transaction, then bumps `LOOM_SECRET_STORE_MASTER_KEY_VERSION`.

Implementations:
- `local-encrypted` (default for `loom service`): AES-GCM with a key derived from `LOOM_SECRET_STORE_MASTER_KEY`; ciphertext + nonce in `secrets` table.
- `k8s-secret` (default for `loom cluster`): one k8s Secret per ref in the `loom` namespace. Ref = `k8s://<namespace>/<secret-name>`.
- Stubs only (not shipped this rollout): `vault://`, `aws-kms://`, `gcp-kms://`.

### SSRF defense: egress proxy, not allowlist

The previous spec's "block RFC1918 + link-local at validation" is insufficient (IPv6 ULA, IPv6 link-local, `0.0.0.0`, `[::]`, DNS rebinding all bypass it). The shipped defense:

1. At `POST /provider-connections` validation: resolve `base_url`, validate every resolved address is non-private (covers IPv4 + IPv6 + loopback + link-local + ULA), store the resolved IPs in `resolved_egress_ips`.
2. **All gateway outbound traffic goes through an egress proxy** (`loom-egress-proxy`, simple Envoy or Squid container, deployed alongside the Gateway in both `service` and `cluster` modes). The proxy enforces:
   - Destination must match `resolved_egress_ips` for the chosen `provider_connection_id` (defeats DNS rebinding).
   - HTTPS only (TLS-SNI matches base_url host).
   - Per-team rate limit (defense-in-depth against runaway).
3. The Gateway never makes direct outbound HTTP calls to provider URLs after Phase 2; only to the egress proxy.

This is shipped in Phase 2, not deferred. The allowlist-only defense fails too easily to be the only mitigation.

### Gateway hot-path latency

Per-call decrypt + DB lookup adds ~5–20 ms at concurrency 100. Mitigation:

- In-process LRU (`functools.lru_cache` wrapped in a manual TTL): `provider_connection_id → (decrypted_key, expiry, etag)`. TTL 10 min.
- `PATCH /provider-connections/{id}` and `DELETE /provider-connections/{id}` publish an invalidation message on a small in-process pubsub (and the cache key includes the row's `updated_at` for natural invalidation across replicas).
- Cache hit path: ~50 µs. Cache miss: original DB+decrypt cost.

### Cost computation for user-supplied endpoints

User-supplied endpoints (the whole point of #49) don't have a Loom-maintained rate card. Three modes via `provider_connections.pricing_source`:

| `pricing_source` | Behavior |
|---|---|
| `rate-card` | Look up `(provider_type, model_id)` in the seeded rate card. Default for `provider_type ∈ {anthropic, google, openai-compatible-canonical}`. |
| `tokens-only` | Record `input_tokens` + `output_tokens` + `provider_connection_id`; leave `cost_usd = NULL`. SPA/CLI display tokens, not dollars. |
| `operator-supplied` | Use `provider_connections.pricing_data` (`{input_usd_per_1m, output_usd_per_1m}`). Operator sets at create/update. |

`loom providers create` defaults to `tokens-only` unless `--input-usd-per-1m`/`--output-usd-per-1m` are passed (operator-supplied) or `--type=anthropic|google` (rate-card).

## `provider_models_cache` lifecycle

| Event | Action |
|---|---|
| `POST /provider-connections` succeeds | Background refresh: enumerate models, populate cache, `last_seen_at = now()`, `upstream_present = true`. |
| `GET /provider-connections/{id}/models` | If `now() - max(last_seen_at) > 1 h`, background refresh. Always return current cache (no client-side wait for refresh). |
| `loom providers models NAME --refresh` | Synchronous refresh. |
| Background refresh detects model gone | `upstream_present = false`, `hidden_reason = "missing-upstream"`. NOT deleted (audit trail). Re-appearing model flips back. |
| `loom providers models NAME --hide MODEL` | `visible = false`, `hidden_reason = "operator-hidden"`. |
| Refresh fails (network error) | Cache untouched. `last_validation_error` set. Surfaced in `loom providers show`. |

Models marked `upstream_present=false AND visible=true` show in CLI with a `(missing upstream)` annotation but can still be selected (some providers de-list and re-list models without behavior changes).

## K8s manifest changes (`loom cluster`)

Implementation lives in PR per Phase 3. Decisions:

- **`worker.yaml`**: Deployment → DaemonSet. Volume mounts: `docker.sock` (hostPath), `bench-cache` (hostPath, read-through cache only), `trajectory-cache` (hostPath, write-through to MinIO). No `--privileged`; container runs as `loom` user with docker.sock socket access via gid.
- **Control node taint**: `loom cluster up` taints the control node with `loom.io/role=control:NoSchedule`. Worker DaemonSet pods don't tolerate this taint by default. `--co-locate-workers-on-control` flag adds the toleration (degrades on small clusters; documented tradeoff). Postgres+MinIO StatefulSets get the toleration + a `nodeSelector: loom.io/role=control` so they pin to the tainted node.
- **`postgres.yaml`, `minio.yaml`**: keep `replicas: 1`. Add nodeSelector + toleration. Ship a `backup-cronjob.yaml` that `pg_dump`s to `s3://<bucket>/backups/postgres/<ts>.sql.gz` + `mc mirror`s MinIO to a backup bucket. CronJob defaults to disabled with a clear `# enable in operator runbook` comment.
- **`bootstrap-job.yaml`**: one-shot Job. `python -m loom_service.bootstrap` runs `alembic upgrade head`, checks if admin token exists, mints if not. **Output mechanism**: tokens written to stdout (visible via `kubectl logs job/loom-bootstrap`); `loom cluster up` watches the Job, captures stdout, prints to operator, then `kubectl delete job/loom-bootstrap` (Job logs purged automatically). No k8s Secret involved — no etcd plaintext leak. Re-running bootstrap on an already-bootstrapped cluster is a no-op (token rotation is a separate verb, see below).
- **`loom-service`, `control-plane`, `llm-gateway`, `web`**: keep ≥2 replicas, add `topologySpreadConstraints` so they spread across nodes.
- **`egress-proxy.yaml`**: new. Deployment, ≥2 replicas, runs Envoy with a simple HTTP CONNECT + IP-allowlist config rendered from `provider_connections.resolved_egress_ips` (updated via a sidecar that watches Postgres NOTIFY on `provider_connections_changed`).
- **Network policies**: ship a default `NetworkPolicy` set:
  - `loom-worker` pods can reach `egress-proxy` + `control-plane` + `llm-gateway` only.
  - `egress-proxy` can reach `0.0.0.0/0:443` only.
  - Cross-team boundary enforcement: trial sandbox containers are launched by docker.sock (not as k8s pods), so k8s NetworkPolicy doesn't reach them — boundary stays at the Docker network level (existing).

### Token rotation (separate from bootstrap)

```
loom admin token rotate --kind {admin,worker,team:<id>}     # mints new, marks old revoked, prints new
loom admin secrets rotate --new-master-key-file PATH        # rewraps all encrypted_api_key_refs
```

Rotation is its own verb. Bootstrap is fire-once-idempotent.

## Reject-batch-when-no-worker

Confirmed against `src/loom_service/routes/backends.py:44` — capabilities shape is `list[dict]` with optional `backend` key; existing `list_backends` does the same extraction. Add the same check to `POST /batches` (`src/loom_service/routes/batches.py:268+`): query active workers' capabilities, fail-fast 400 if `payload.backend` not in the union. Admin override flag for "I'm provisioning a worker right now": `--force-no-worker-check`. Race window between SELECT and INSERT is acceptable (worst case: batch claims fail and surface the same error).

## Token race fix (`loom service up`)

Confirmed `_up` at `src/loom_cli/service_cmd.py:172` with `_write_env_tokens` at line 215. After `_write_env_tokens` returns, run:

```
docker compose ... up -d --force-recreate --no-deps worker
```

so worker reads the fresh `LOOM_WORKER_TOKEN`. `docker restart` reuses old env; only `up --force-recreate` re-reads `.env`. Skippable with `--no-recreate-worker` for power users.

## Multi-tenancy boundaries

Per-team scoping in the deployment:

| Boundary | Mechanism |
|---|---|
| Provider connections | `team_id` FK; routes filter on `ctx.team_id`; cross-team access returns 404 |
| Trial / batch ownership | existing `team_id` on rows; cross-team reads 404 |
| Sandbox container egress (LLM calls) | egress proxy enforces per-team allowlist of `resolved_egress_ips` keyed by the trial's `provider_connection_id` |
| Sandbox container egress (non-LLM) | docker network policy: sandbox can reach gateway + nothing else (existing) |
| Per-team Postgres tenancy | shared schema with `team_id` columns + row-level access checks (existing); not separate schemas |
| Per-team k8s namespace | NOT shipped — single `loom` namespace, multi-team enforced at row level. Documented as a gap; future work for hostile-tenant clusters. |
| Per-team MinIO bucket | NOT shipped — shared buckets, prefixed by `team_id`. Cross-team bucket read attempts blocked at API layer (route filter), not at MinIO ACL layer. Trade-off: simpler ops, weaker isolation. |

This is appropriate for the "trusted users, untrusted prompts" threat model (Loom's). It is not appropriate for hostile-tenant SaaS — Loom is not that yet.

## Upgrade path: `loom service` → `loom cluster`

For users on single-box `loom service` who outgrow it:

```
# On the source single-box install:
loom admin export --out /tmp/loom-export.tar.gz
    # Bundles: pg_dump of all rows + mc mirror of MinIO buckets + the master-key

# On the new control node:
loom cluster up --nodes hostfile --import /tmp/loom-export.tar.gz
    # Restore postgres, restore MinIO, install master-key, mint NEW admin token,
    # print mapping table (old trial_id → new trial_id is identity; nothing renumbered).
```

Ships in Phase 4 alongside the runbook. The export bundle is the same format as the backup CronJob's output, so disaster recovery and migration share a code path.

## Implementation phases

Reordered from rev 1. Phase 2 ships the user-facing CLI **first** (works against existing `loom service`); cluster manifests come in Phase 3 once users have something to deploy with.

### Phase 0 — this spec, revised (current PR #50)

- This document, `Status: design`.
- Update `docs/architecture/service-mode.md` to point at this doc.
- No code changes.

### Phase 1 — image pipeline + token race fix

Independent prereq for everything else. Ship first because Phase 3 cannot land without ARM images.

- `.github/workflows/images.yml`: add `linux/arm64` to buildx matrix; push multi-arch manifest lists to GHCR for all 5 images.
- `src/loom_cli/service_cmd.py:172+`: `--force-recreate worker` after `_write_env_tokens`.
- Tests: extend `tests/loom_cli/test_service_cmd.py`.

### Phase 2 — `loom auth` + `loom providers` + `loom eval` + SSRF defense

Works against existing `loom service`. Cluster work parallel-tracked but not required.

- Migration `0018_provider_connections.py` (down_revision="0017").
- `src/loom/db/schema.py`: `ProviderConnection`, `ProviderModelCache`.
- `src/loom/security/secret_store.py`: Protocol + `local-encrypted` impl.
- `src/loom_service/routes/provider_connections.py`: 7 routes (create/list/show/update/delete/test/models).
- Gateway: connection resolver, LRU cache, egress-proxy integration.
- Gateway egress is forced through `loom-egress-proxy` (new container; ships in single-box compose too).
- `src/loom_cli/{auth,providers,eval}_cmd.py`.
- Cost-source 3-way mode (`rate-card | tokens-only | operator-supplied`) on the connection.
- `provider_models_cache` background refresh (TTL 1 h on read; sync on `--refresh`).
- Tests: SSRF (DNS rebinding via test resolver), key never echoed in `providers show`, LRU invalidation on PATCH.
- Reject-batch-when-no-worker check on `POST /batches`.

### Phase 3 — `loom cluster up` against Ascent boxes

Depends on Phase 1 (ARM images) and Phase 2 (CLI surface so the cluster is actually usable on day one).

- `deploy/k8s/worker.yaml`: Deployment → DaemonSet with the volume + write-through model.
- `deploy/k8s/{postgres,minio}.yaml`: nodeSelector + tolerations for control taint.
- `deploy/k8s/bootstrap-job.yaml` + `src/loom_service/bootstrap.py` (stdout token; no k8s Secret).
- `deploy/k8s/egress-proxy.yaml`.
- `deploy/k8s/backup-cronjob.yaml` (disabled by default).
- `deploy/k8s/networkpolicies.yaml`.
- `src/loom_cli/cluster_cmd.py`: `loom cluster {up,down,status}`.
  - Reads hostfile, labels nodes (`loom.io/role=control|worker`), taints control node.
  - Optionally installs ingress-nginx via `kubectl apply -f` from a pinned URL.
  - Generates self-signed CA + cert if no `--tls-cert`.
  - Applies manifests in order: namespace → secrets → PVCs → Postgres → MinIO → bootstrap Job → core deployments → DaemonSet → ingress + cert.
  - Watches bootstrap Job, extracts tokens from stdout, prints, deletes the Job.
- Tests:
  - kind smoke test: `loom cluster up --kubeconfig $KIND --nodes fixtures/hostfile`, `loom auth login`, `loom eval batch create`, assert batch reaches `complete`.
  - Idempotency: re-run `loom cluster up` is a no-op (skips bootstrap when admin exists).

### Phase 4 — runbook + upgrade path

- Promote this doc to `Status: shipped`.
- `docs/cluster-deploy-runbook.md`: hostfile format, ingress/TLS prep, first-deploy walkthrough, common ops (rotate admin, drain node, restart workers, enable backup CronJob).
- `loom admin export` + `loom cluster up --import`.
- CI smoke (kind + `loom cluster up` + `loom eval run`).

### Phase 5 — `loom admin token rotate` + `loom admin secrets rotate`

- Token rotation verbs.
- `SecretStore.rewrap` walkthrough.
- Tests: rotation under load (key in flight is decrypted with old master, new connections use new master).

## Risks + mitigations

| Risk | Severity | Mitigation |
|---|---|---|
| ARM image build pipeline blocks Phase 3 | HIGH | Phase 1 ships images first; Phase 3 cannot start without them. Hard prerequisite. |
| Network stack assumptions wrong on Ascent setup | HIGH | Default ingress-nginx + self-signed CA + explicit `--ingress none / --tls-cert` escape hatches. Runbook (Phase 4) walks the typical Ascent layout end-to-end. |
| Trajectory data loss on node failure | HIGH (rev 1 had this) | Fixed in rev 2: trajectories canonical in MinIO, hostPath is write-through cache only. Partial-run trajectory loss remains a gap (same as today's single-box). |
| SSRF via `provider_connections.base_url` | HIGH | Egress proxy with IP-allowlist enforcement (not just validation-time check). Defeats DNS rebinding by validating destination IP every call. |
| API key leakage in logs / trajectory | HIGH | SecretStore returns plaintext only to gateway forwarder; gateway redacts in usage rows; trajectory writer scrubs known secret patterns. |
| Control node death = full data loss | HIGH | `--storage external` for production; backup CronJob ships disabled with clear enable instructions; runbook documents RPO. Documented limitation. |
| K8s burden on operators new to k8s | MEDIUM | `loom cluster up` wraps every `kubectl` call. Runbook walks the operator-supplied artifacts (hostfile, optional cert, registry). |
| Worker DaemonSet on control node fights Postgres for IO | MEDIUM | Control node tainted by default; `--co-locate-workers-on-control` opt-in flag. Documented tradeoff. |
| Bootstrap Job admin token in pod logs | MEDIUM | Job deleted immediately after `loom cluster up` reads stdout. journald node-local; documented in runbook. |
| `loom eval` and `loom run` confuse users | MEDIUM | Clear doc cross-link; `loom run` help mentions "no server needed"; `loom eval` help mentions "needs `loom auth login`." |
| Gateway latency from per-call decrypt | LOW | In-process LRU cache, TTL 10 min, invalidation on PATCH. |
| Master-key rotation breaks existing connections | LOW | `SecretStore.rewrap` exists from day 1; Phase 5 ships the rotation verb. |
| ingress-nginx URL pin drifts | LOW | Pinned by SHA; refreshed when we bump it intentionally. |
| Single MinIO instance bottlenecks 4 workers at 200 Gbps | LOW | Distributed-mode MinIO via `--storage external`. Embedded single-instance is sufficient for typical workload sizes. |

## Open questions

1. **Self-signed CA vs operator-cert-required for Phase 3**: ship self-signed by default and let operators bring real certs, or refuse to start without `--tls-cert`? **Recommendation:** ship self-signed by default with a loud warning. Lower friction for the Ascent target.
2. **Ingress controller pinning policy**: bump on every release, or pin and bump quarterly? **Recommendation:** pin by SHA, bump quarterly + on security advisories.
3. **`loom-egress-proxy` choice**: Envoy or Squid? **Recommendation:** Envoy. We already have YAML for it elsewhere, and the dynamic-config story for IP allowlists is cleaner.
4. **`provider_models_cache` background refresh frequency**: 1 h read-triggered (current spec) or also a background CronJob every 6 h? **Recommendation:** read-triggered only for v1; revisit if users complain about stale caches.
5. **`loom service` future**: keep indefinitely as the dev/demo path, or merge into `loom cluster --single-node` once cluster is solid? **Recommendation:** keep indefinitely. The compose stack has 2 years of muscle memory; killing it churns docs/CI/CLAUDE.md.

## Appendix A — `loom cluster up` operator walkthrough

```bash
# 1. Hostfile (one-time).
cat > hostfile.txt <<EOF
control:  ascent-0.lab.local
worker:   ascent-1.lab.local
worker:   ascent-2.lab.local
worker:   ascent-3.lab.local
EOF

# 2. (Optional) bring your own TLS cert + ingress decisions.
#    Otherwise loom mints a self-signed CA.

# 3. Deploy.
loom cluster up \
    --nodes hostfile.txt \
    --kubeconfig ~/.kube/config-ascent \
    --registry ghcr.io/carinrc \
    --storage embedded
# → labels nodes (control: ascent-0, workers: ascent-1/2/3)
# → applies manifests (postgres, minio, egress-proxy, bootstrap-job, …, worker DaemonSet)
# → waits for bootstrap-job (~45 s)
# ✓ cluster ready
#
# Admin token (paste into `loom auth login`):
#   loom_admin_XXXX
# CA cert (distribute to clients):
#   /tmp/loom-ca-XXXX.crt

# 4. Connect.
loom auth login --server https://loom.ascent.lab.local
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
| AIME-22/23/24/25 | ✅ | ✅ |
| GAIA | ✅ (needs HF auth) | ✅ |
| WebArena | ⚠️ requires Playwright ARM rebuild | ✅ |
| SWE-Bench / SWE-Bench Verified / SWE-Bench Multimodal | ❌ (x86-only eval images) | ✅ |
| OSWorld | ❌ (x86 VM images) | ✅ |
| skillflow, skilllearnbench | ❌ (per-task Dockerfiles, not yet ARM-built) | ⚠️ adapter rewrite pending |

To run x86-only benchmarks on a mixed cluster: rack one x86 worker, label it `loom.io/arch=amd64`. Worker DaemonSet schedules pods on every arch; per-task scheduler matches `task.environment.cpu_arch` to a tolerating worker. Out of scope for the initial Ascent deploy.

## Changelog

- **2026-06-15 rev 2**: Addresses PR #50 review concerns:
  1. Trajectories canonical in MinIO; hostPath downgraded to write-through cache.
  2. Bench-cache rationale rewritten as read-through, not "each box pays its own."
  3. Strategy C dropped; folded into `loom cluster --storage external`.
  4. Network stack section added (ingress, TLS, DNS, egress proxy).
  5. Image pipeline section added (multi-arch CI as Phase 1 prereq).
  6. Phases reordered: 1=images+token-race, 2=CLI surface+SSRF, 3=cluster manifests.
  7. Control node tainted by default; opt-in `--co-locate-workers-on-control`.
  8. `SecretStore.list_refs` + `rewrap` added to Protocol; rotation verb in Phase 5.
  9. SSRF: egress proxy with IP-allowlist enforcement; not allowlist-only.
  10. Gateway LRU cache + invalidation specified.
  11. Cost: 3-way `pricing_source` enum on connection.
  12. `provider_models_cache` refresh contract specified.
  13. Provider-delete vs in-flight trial: SET NULL + fail-fast new claims.
  14. Reject-batch capabilities shape verified against `routes/backends.py:44`.
  15. Rename reversed: keep `loom service`, add `loom cluster`. No `loom deploy` family.
  16. Bootstrap tokens via stdout + Job deletion; no k8s Secret leak.
  17. `--rotate` removed from bootstrap; rotation is a separate verb.
  18. Multi-tenancy boundaries enumerated; per-namespace/bucket isolation documented as gap.
  19. Upgrade path `loom service → loom cluster` via `admin export` / `cluster up --import`.
  20. YAML/code snippets trimmed; full manifests live in implementation PRs.
- **2026-06-15 rev 1**: Initial spec.

## See also

- [#49](https://github.com/carinrc/loom/issues/49) — Production cluster deployment with user-supplied model provider gateway
- [service-mode.md](service-mode.md) — current single-host architecture
- [drf-scheduling.md](drf-scheduling.md) — how the claim path matches workers to trials
- [overview.md](overview.md)
