# Cluster deployment design

**Status: design** (2026-06-15, revision 3). Tracking PR: [#50](https://github.com/carinrc/loom/pull/50). Supersedes inline guidance in `docs/architecture/service-mode.md`.

Extends [#49](https://github.com/carinrc/loom/issues/49). Initial deployment target is 2–4× ASUS Ascent GX10 (ARM v9.2-A, GB10 Grace Blackwell, 128 GB unified, 4 TB local NVMe, 200 Gbps ConnectX-7), but **the design is not coupled to that hardware** — Loom is a platform; the deployment shape is selectable at deploy time.

## Table of contents

1. [Status block](#status-block)
2. [Goals](#goals) · [Non-goals](#non-goals)
3. [Topology](#topology) · [Storage dimension](#storage-dimension-orthogonal-to-verb)
4. [Data architecture (canonical vs cached)](#data-architecture-canonical-vs-cached)
5. [Network stack](#network-stack)
6. [Image pipeline (multi-arch)](#image-pipeline-multi-arch)
7. [CLI surface](#cli-surface)
8. [Schema changes](#schema-changes)
9. [Secrets, SSRF, and the Gateway hot path](#secrets-ssrf-and-the-gateway-hot-path)
10. [Worker concurrency model](#worker-concurrency-model)
11. [`provider_models_cache` lifecycle](#provider_models_cache-lifecycle)
12. [K8s manifest changes](#k8s-manifest-changes-loom-cluster)
13. [Reject-batch-when-no-worker](#reject-batch-when-no-worker)
14. [Token race fix](#token-race-fix-loom-service-up)
15. [Multi-tenancy boundaries](#multi-tenancy-boundaries)
16. [Upgrade path](#upgrade-path-loom-service--loom-cluster)
17. [Implementation phases](#implementation-phases)
18. [Risks + mitigations](#risks--mitigations) · [Open questions](#open-questions)
19. [Appendix A — operator walkthrough](#appendix-a--loom-cluster-up-operator-walkthrough)
20. [Appendix B — supported-benchmark matrix](#appendix-b--supported-benchmark-matrix-per-arch-initial)
21. [Changelog](#changelog)

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
5. **Data durability is design-in, not bolted on.** Trajectories of *completed* trials survive any single-node failure on day 1; in-flight trajectories are uploaded chunk-by-chunk so failure during a run loses at most the last unflushed batch (~1 s of events).

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

**Write-through model for trajectories**: the `TrajectoryWriter` appends to a local file (low-latency for the agent loop) and flushes batches of events to MinIO via multipart upload every `LOOM_TRAJECTORY_FLUSH_INTERVAL_MS` (default 1000 ms). Each flush completes a part; the multipart upload is finalized on trial completion. Node failure mid-run loses at most one unflushed batch (~1 s worth of events); the rest is in MinIO and is recoverable via `mc complete-multipart-upload` from the operator runbook. Trajectory of any *completed* trial is fully in MinIO with no operator action required.

**Read-through model for benchmarks**: worker checks `bench-cache/<slug>/<version>/.complete`; if missing, downloads from MinIO and atomically renames `.tmp` → final + writes the marker. Concurrent first-fetches: per-slug-version `flock(2)` prevents thundering herd. Eviction: LRU when `/var/lib/loom/benchmarks/` exceeds an operator-configurable quota (`LOOM_BENCH_CACHE_QUOTA_GB`, default 200). An **in-use registry** (`/var/lib/loom/benchmarks/.in-use/<slug>__<version>` symlinks, refcounted via flock by the worker per running trial) protects mounted slugs from eviction; the eviction loop skips entries with a live in-use symlink. Stale in-use entries from killed workers are reaped on worker startup (`fuser`-style check via `/proc/*/cwd`).

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
| TLS (ingress termination) | self-signed cert from a generated CA (private deployments) OR operator-supplied (`--tls-cert PATH --tls-key PATH`) OR `cert-manager` + ACME | cert-manager + Let's Encrypt for public domains |
| TLS (egress validation) | egress proxy validates upstream provider certs via system CA bundle; operator can add custom CAs via `--egress-ca-bundle PATH` for self-hosted providers | — |
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
loom auth login --server URL --token {env:VAR | file:PATH | -}   # never on CLI args; "-" reads stdin
loom auth status                                                  # for CI: passes if logged in, fails otherwise
loom auth logout

loom providers create --name N --type {openai-compatible,anthropic,google,custom} \
    --base-url URL --api-key {env:VAR | file:PATH | -}            # never on CLI args
    [--allowed-models LIST] [--input-usd-per-1m FLOAT --output-usd-per-1m FLOAT]
loom providers list / show NAME / test NAME / models NAME [--refresh]
loom providers update NAME --base-url URL                        # PATCH
loom providers delete NAME

loom eval run --provider N --model M --agent A --benchmark B
    [--task ID | --task-filter JSON] [--backend B] [--name N]
loom eval batch create --provider N --model M --agent A --benchmark B
    [--combinations FILE | --task-filter JSON] [--concurrency N] [--name N]
loom eval batch {list,show,cancel} [--state S]
loom eval trial {list,show} | trajectory ID [--out PATH] | atif ID [--out PATH]
```

**Argv hygiene rule (uniform):** every secret-bearing flag (`--token`, `--api-key`) accepts ONLY `env:VAR`, `file:PATH`, or `-` (stdin). Literal values are rejected at argparse-time with a clear error pointing at the indirection forms. Reasons: shell history, `ps` listings, CI log capture all leak argv.

**Non-interactive use (CI):**

```bash
# In a CI job:
echo "$LOOM_TOKEN" | loom auth login --server "$LOOM_SERVER" --token -
loom auth status || exit 1
loom eval batch create --provider prod --model gpt-4o \
    --agent claude-code-inbox --benchmark humaneval \
    --task-filter '{"subset_kind":"random_n","n":3,"seed":42}' --name "ci-$GITHUB_SHA"
```

`--combinations FILE` format (JSON):

```json
[
  {"model": "gpt-4o",       "agent": "claude-code-inbox", "n": 3},
  {"model": "claude-opus-4-7", "agent": "swe-agent",      "n": 3,
   "provider_connection_id": "uuid-...", "task_filter": {"subset_kind": "all"}}
]
```

Each entry overrides the corresponding `loom eval batch create` flag for that combination. `provider_connection_id` / `provider_model_id` per entry allow cross-provider sweeps within one batch.

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

`secrets` (backing the `local-encrypted` SecretStore impl):

```
ref                         text PK            -- "loom://<namespace>/<uuid>"
ciphertext                  bytea NOT NULL
nonce                       bytea NOT NULL     -- 12-byte AES-GCM nonce
master_key_version          int NOT NULL       -- bumped on rotation; rewrap walks all rows
created_at                  timestamptz NOT NULL DEFAULT now()
```

`provider_connections`:

```
id                          UUID PK
team_id                     UUID FK → teams.id  ON DELETE CASCADE
provider_type               text -- 'openai-compatible' | 'anthropic' | 'google' | 'custom'
display_name                text -- UNIQUE per (team_id, display_name)
base_url                    text
upstream_host               text -- parsed from base_url; the host the egress proxy validates SNI against
resolved_egress_ips         inet[] -- native Postgres array; populated by re-resolver background job
egress_ips_refreshed_at     timestamptz
egress_ips_ttl_seconds      int NOT NULL DEFAULT 300   -- min of (DNS TTL, this value)
encrypted_api_key_ref       text -- ref into `secrets` table for local-encrypted; "k8s://ns/name" for k8s-secret
allowed_models              text[] | NULL    -- NULL = "all that upstream returns"
status                      text -- 'pending' | 'valid' | 'invalid' | 'disabled'
last_validated_at           timestamptz | NULL
last_validation_error       text | NULL
pricing_source              text -- 'rate-card' | 'tokens-only' | 'operator-supplied'
pricing_data                jsonb | NULL    -- {input_usd_per_1m, output_usd_per_1m, ...}
created_by                  text -- "<token-type>:<token-id-suffix>"
created_at, updated_at      timestamptz
```

`provider_models_cache`:

```
provider_connection_id      UUID FK → provider_connections.id  ON DELETE CASCADE
model_id                    text  -- (PK with provider_connection_id)
family                      text | NULL
context_length              int | NULL
capabilities                jsonb
visible                     bool DEFAULT true       -- operator-toggleable
hidden_reason               text | NULL             -- 'operator-hidden' | 'missing-upstream'
last_seen_at                timestamptz
upstream_present            bool DEFAULT true       -- false after a refresh that didn't see this model
```

Note: the spec previously listed `'disabled-pricing'` as a `hidden_reason` value. Removed — rate-card miss is a pricing-display issue, not a model-visibility issue; show the model with `(no pricing)` annotation instead of hiding it.

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

`list_refs` + `rewrap` are mandatory from day 1 to make master-key rotation possible.

#### Implementations and which mode ships which

Both impls ship in **Phase 2**, not Phase 3. Cluster mode needs both: `local-encrypted` for the data path (one master key in a single k8s Secret + ciphertext in Postgres scales linearly), `k8s-secret` for operator-supplied bootstrap credentials.

| Impl | Default in | Storage | `rewrap()` semantics |
|---|---|---|---|
| `local-encrypted` | `loom service` AND `loom cluster` (data path) | AES-GCM, key from `LOOM_SECRET_STORE_MASTER_KEY`; ciphertext + nonce + `master_key_version` in the `secrets` table | re-encrypts with new key, bumps `master_key_version`; old key kept available during cutover via `LOOM_SECRET_STORE_PREV_MASTER_KEY` |
| `k8s-secret` | bootstrap-supplied secrets only (Postgres password, MinIO creds, master key itself) | one k8s Secret per ref in `loom` ns | no-op (the cluster's etcd EncryptionConfiguration is the layer responsible for at-rest encryption) — explicitly documented; `rewrap()` returns the same ref |
| `vault://` / `aws-kms://` / `gcp-kms://` | — | — | stubbed; raise `NotImplementedError` with a clear pointer |

The two-impl split clarifies "who encrypts what." User-provided API keys live in `local-encrypted` (millions × small, custom-encrypted). Bootstrap infra credentials live in `k8s-secret` (a handful, managed by the operator's k8s practices).

`loom admin secrets rotate --new-master-key-file PATH` walks every `local-encrypted` ref, calls `rewrap`, atomically swaps `encrypted_api_key_ref` columns inside a transaction, then bumps `LOOM_SECRET_STORE_MASTER_KEY_VERSION` and clears `LOOM_SECRET_STORE_PREV_MASTER_KEY` after a configurable cutover window (default 60 s).

### Sandbox-to-egress-proxy traffic path

This is the security pivot. Trial sandbox containers are spawned by the worker via docker.sock onto a Docker bridge network — they are NOT k8s pods. Without explicit routing, a sandbox can dial the public internet directly through the Docker bridge → host → upstream, completely bypassing the egress proxy. That defeats SSRF defense.

The shipped path:

```
┌─ sandbox container ───────────────┐
│  env: HTTPS_PROXY=http://step-jwt │   sandbox is given a STEP-SCOPED proxy URL with an
│       :PORT@loom-llm-gateway      │   inline credential. Agent sees only the upstream
│  → sees only the gateway as the   │   API as if it were direct (OpenAI SDK respects
│    LLM endpoint                   │   HTTPS_PROXY).
└────────────────┬──────────────────┘
                 │ HTTPS CONNECT to gateway
                 ▼
┌─ loom-llm-gateway (k8s Service) ──┐
│  validates step JWT, resolves     │   gateway is the trust boundary: decrypts API
│  provider_connection_id, decrypts │   key, knows team_id, can rate-limit per team.
│  api key, forwards via egress     │
│  proxy                            │
└────────────────┬──────────────────┘
                 │ HTTPS CONNECT
                 ▼
┌─ loom-egress-proxy (k8s Service) ─┐
│  Envoy with per-conn IP allowlist │   enforces destination IP ∈ resolved_egress_ips
│  matched on TLS SNI               │   for the requested base_url host. Per-team rate.
└────────────────┬──────────────────┘
                 │ HTTPS to provider
                 ▼
            OpenAI / Anthropic / …
```

Three concrete changes to make this work:

1. **Worker injects gateway address into the sandbox container.** Today the worker already passes `LOOM_GATEWAY_URL` into the sandbox env (used by `loom-agent-runtime`). Phase 2: also pass an HTTPS_PROXY-style indirection so non-Loom agents using stock provider SDKs (OpenAI, Anthropic) automatically go through the gateway.
2. **Docker network policy on the sandbox bridge: outbound default-DENY.** The sandbox can reach the gateway (via host gateway IP) and nothing else. Implemented via Docker's `--network` with a custom bridge that has no internet route by default, plus a `--add-host` for the gateway. On worker nodes that's a single iptables rule managed by the worker on container create. Without this, a malicious agent could ignore `HTTPS_PROXY` and dial out directly. **This is the single most important Phase 2 deliverable for SSRF defense.**
3. **Gateway authenticates sandbox→gateway calls via a step-scoped JWT** (existing in service mode; extend to cluster). The JWT carries `(trial_id, team_id, provider_connection_id, step_id)`; signed by control-plane at trial start; verified by gateway. Egress proxy trusts the gateway's per-connection routing — it doesn't independently authenticate sandboxes.

The egress-proxy's role is **destination IP enforcement and rate-limiting**, not authentication. Authentication lives at the gateway; the egress proxy is the second layer.

### `resolved_egress_ips` re-resolution

Providers rotate IPs frequently (Anycast, ELB churn). Stale `resolved_egress_ips` would break legitimate traffic the moment OpenAI shuffles its load balancer.

The shipped re-resolver (background job in the gateway):

- Resolves `upstream_host` for every `provider_connection` row, every `min(dns_ttl, egress_ips_ttl_seconds)` (default floor 300 s).
- Computes the union of (current `resolved_egress_ips`, newly resolved set). Writes the union to `resolved_egress_ips` with a `last_seen` per IP in a sidecar JSONB column. Drops IPs not seen for 24 h.
- Update is atomic: a single UPDATE row + Postgres `NOTIFY provider_egress_changed, '<id>'`.
- Egress proxy and gateway both subscribe to the NOTIFY (see "Cache + config update durability" below for the fallback when NOTIFY drops).

This means valid providers stay reachable across IP churn and the allowlist still defeats DNS rebinding (rebinding an attacker-controlled host produces an IP NOT in the union; rejected).

### SSRF defense: layered

The previous spec's allowlist-only defense (RFC1918 + link-local at validation) is insufficient (IPv6 ULA, IPv6 link-local, `0.0.0.0`, `[::]`, DNS rebinding bypass it). The shipped defense, in layers:

1. **Sandbox network isolation** (above): sandbox can't reach anywhere except the gateway. Defeats agent-driven SSRF entirely if (2)–(4) fail.
2. **`POST /provider-connections` validation**: resolve `base_url`, validate every resolved address is non-private (IPv4 RFC1918 + loopback + link-local; IPv6 loopback `::1` + link-local `fe80::/10` + ULA `fc00::/7`; `0.0.0.0`, `[::]`, `0.0.0.0/8`), store the resolved IPs.
3. **Egress proxy IP allowlist** (above): destination IP must match the connection's `resolved_egress_ips` *at call time*.
4. **TLS SNI match**: the egress proxy requires SNI to match `upstream_host`; defeats IP-allowlist bypass via host-header confusion against a shared LB.

Layers (1) and (3) are the load-bearing ones. (2) is a UX layer (reject obvious mistakes at create time). (4) is defense-in-depth.

### Cache + config update durability (multi-replica)

Three caches matter: Gateway's decrypted-key LRU (≥2 gateway replicas), egress-proxy's IP-allowlist (≥2 egress replicas), and gateway's provider-connection metadata cache (same replica set). All must agree quickly when a user PATCHes a connection or rotates credentials.

The shipped pattern:

- **Cache key includes `provider_connections.updated_at`**. Every Gateway call does a cheap indexed `SELECT id, updated_at FROM provider_connections WHERE id = $1` (single primary-key lookup, ~100 µs at p99). If `updated_at` matches the cache entry's, hit; otherwise miss → DB fetch + decrypt → repopulate. This makes stale-cache impossible regardless of pubsub reliability; the only cost is the indexed lookup per call.
- **NOTIFY is an optimization, not the correctness layer**. Gateway and egress-proxy both subscribe to `provider_connections_changed` and `provider_egress_changed`. On NOTIFY they invalidate proactively (eliminates the one-call window where another replica writes and this one still has the cached value).
- **NOTIFY connection loss recovery**: a separate goroutine/task on each replica polls `SELECT max(updated_at) FROM provider_connections` every 30 s. If max moved more than the local NOTIFY cursor expects, force-invalidate the whole cache. Belt-and-suspenders for LISTEN's well-known fragility.
- **Egress proxy reload**: Envoy reloads its IP-allowlist via xDS (an in-process control server reads Postgres on NOTIFY + every 30 s polling, serves EDS endpoints to Envoy). No SIGHUP, no restart. Latency: NOTIFY-driven update p99 < 500 ms; polling-driven update p99 < 30 s. Documented in runbook.

This is more moving parts than rev 2's "in-process pubsub + 10-min TTL" but it's correct under partition. The 10-min TTL is removed: TTL is "until `updated_at` changes."

### Gateway hot-path latency

- Hot path: indexed `SELECT updated_at` (~100 µs) + LRU hit on decrypted key (~5 µs). Adds ~100 µs over no-cache baseline; acceptable. The provider call itself is 100s of ms.
- Cold path: full row fetch + AES-GCM decrypt (~1 ms). Happens on cache miss or post-PATCH.
- Practical concurrency limit at p99 < 1 ms cache hit: ~10k req/s per gateway replica, well above expected load.

### Cost computation for user-supplied endpoints

User-supplied endpoints don't have a Loom-maintained rate card. Three modes via `provider_connections.pricing_source`:

| `pricing_source` | Behavior |
|---|---|
| `rate-card` | Look up `(provider_type, model_id)` in the seeded rate card. **Eligible only for `provider_type ∈ {anthropic, google}`** — these have stable model names. |
| `tokens-only` | Record `input_tokens` + `output_tokens` + `provider_connection_id`; leave `cost_usd = NULL`. SPA/CLI display tokens, no dollars. |
| `operator-supplied` | Use `provider_connections.pricing_data` (`{input_usd_per_1m, output_usd_per_1m}`). Operator sets at create/update. |

**`provider_type = 'openai-compatible'` defaults to `tokens-only`**, never to `rate-card` — the URL-based "is this the canonical OpenAI endpoint?" heuristic is brittle (proxies, VPC endpoints, mirrors all break it). Operator opt-in to `operator-supplied` via `--input-usd-per-1m`/`--output-usd-per-1m` for endpoints with known pricing. This is the single cost source rule reversed from rev 2.

`provider_type = 'custom'` is `tokens-only` only (no rate-card lookup attempted).

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

## Worker concurrency model

A `loom-worker` DaemonSet pod is **one process per node**, not one trial per node. The process handles N concurrent trials via the existing asyncio Semaphore, gated by `LOOM_WORKER_CONCURRENCY`. Defaults are CPU- and memory-aware:

```
LOOM_WORKER_CONCURRENCY = min(
    cpu_cores // 2,         # half the cores per concurrent trial (docker sandbox + agent loop)
    memory_gb // 8,         # rough budget per trial
    32,                     # hard cap
)
```

On an Ascent box (~20 ARM cores, 128 GB unified): default concurrency 10. Operators override with `loom cluster up --worker-concurrency N` (sets a ConfigMap value the DaemonSet reads) or `LOOM_WORKER_CONCURRENCY` per-node.

Why one DaemonSet pod, not N replicas:
- One docker.sock host mount per node; one process owns it cleanly.
- Bench-cache + trajectory-cache use file-level locking; intra-process locks are simpler than cross-process flock dances.
- DRF claim API is per-worker-id; N pods on one node would each register a separate worker, fragmenting the scheduler's view of node capacity.

DRF advertisement (`workers.capabilities.max_concurrent`) is set to `LOOM_WORKER_CONCURRENCY`, not 1. Schedulers see one fat worker per node.

## K8s manifest changes (`loom cluster`)

Implementation lives in PR per Phase 3. Decisions:

- **`worker.yaml`**: Deployment → DaemonSet, one pod per node, `LOOM_WORKER_CONCURRENCY` from ConfigMap. Volume mounts: `docker.sock` (hostPath), `bench-cache` (hostPath, read-through cache only), `trajectory-cache` (hostPath, write-through to MinIO). No `--privileged`; container runs as `loom` user with docker.sock access via gid 999.
- **Control node taint**: `loom cluster up` taints the control node with `loom.io/role=control:NoSchedule`. Worker DaemonSet pods don't tolerate this taint by default. `--co-locate-workers-on-control` flag adds the toleration (degrades on small clusters; documented tradeoff). Postgres+MinIO StatefulSets get the toleration + a `nodeSelector: loom.io/role=control` so they pin to the tainted node.
- **`postgres.yaml`, `minio.yaml`**: keep `replicas: 1`. Add nodeSelector + toleration. Ship a `backup-cronjob.yaml` that `pg_dump`s to `s3://<bucket>/backups/postgres/<ts>.sql.gz` + `mc mirror`s MinIO to a backup bucket. CronJob defaults to disabled with a clear `# enable in operator runbook` comment.
- **`bootstrap-job.yaml`**: one-shot Job. `python -m loom_service.bootstrap` runs `alembic upgrade head`, checks if admin token exists, mints if not. **Output mechanism**: tokens written to stdout (visible via `kubectl logs job/loom-bootstrap`); `loom cluster up` watches the Job, captures stdout, prints to operator, then `kubectl delete job/loom-bootstrap` (Job logs purged automatically). No k8s Secret involved — no etcd plaintext leak. Re-running bootstrap on an already-bootstrapped cluster is a no-op (token rotation is a separate verb, see below).
- **`loom-service`, `control-plane`, `llm-gateway`, `web`**: keep ≥2 replicas, add `topologySpreadConstraints` so they spread across nodes.
- **`egress-proxy.yaml`**: new. Deployment, ≥2 replicas in cluster mode (single container in single-box compose — see note below). Envoy with an in-process xDS control plane (Python sidecar) that reads `provider_connections` + `provider_connections.resolved_egress_ips` from Postgres on `NOTIFY provider_egress_changed` and on a 30 s poll fallback.
- **Egress proxy replica count differs by deployment mode**:
  - `loom cluster` (k8s): Deployment, ≥2 replicas, `topologySpreadConstraints` across worker nodes.
  - `loom service` (compose): single container alongside the gateway.
  - Both modes expose port 8443 (HTTPS CONNECT). Gateway dials it via service DNS in cluster mode, `loom-egress-proxy:8443` in compose.
- **Compose service definition** (deliverable in Phase 2): `docker/compose.yaml` gains a `loom-egress-proxy` service. `LOOM_EGRESS_PROXY_DISABLE=1` removes it from the active profile and falls back to the gateway making direct outbound calls (single-box dev convenience; documented as a security regression in the comment block above the env var).
- **Network policies** (k8s mode):
  - `loom-worker` pod can reach `loom-llm-gateway` + `loom-control-plane` only.
  - `loom-llm-gateway` can reach `loom-egress-proxy` + `postgres` only.
  - `loom-egress-proxy` can reach `0.0.0.0/0:443` only.
  - **Sandbox container egress is NOT enforced by k8s NetworkPolicy.** Sandboxes are docker.sock-spawned bridge containers, outside the k8s network plane. Their egress is enforced by:
    - Default-DENY iptables rule on the sandbox bridge (worker installs at container create; allows only the host-routed gateway endpoint).
    - `HTTPS_PROXY` env var pointing at the gateway (used by stock provider SDKs).
    - A malicious agent ignoring `HTTPS_PROXY` is blocked at the iptables layer. Both layers are required; either one alone is insufficient.

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
loom admin export --out /tmp/loom-export.tar.gz.age --passphrase {env:VAR | file:PATH | -}
    # Bundles: pg_dump of all rows + mc mirror of MinIO buckets + a wrapped master-key.
    # Tarball is encrypted with age (https://age-encryption.org) using the
    # operator-supplied passphrase. The master key is NEVER in the tarball plaintext;
    # it's wrapped with a passphrase-derived KEK at export and unwrapped at import.

# On the new control node:
loom cluster up --nodes hostfile --import /tmp/loom-export.tar.gz.age \
    --import-passphrase {env:VAR | file:PATH | -}
    # Verify passphrase decrypts the bundle; restore postgres + MinIO; unwrap and install
    # master-key into k8s Secret loom-secrets/master-key; mint NEW admin token; print
    # mapping table (trial_id is preserved; team_id is preserved).
```

Ships in Phase 4 alongside the runbook. The encrypted export bundle is the same format as the backup CronJob's output (which also writes `.tar.gz.age`), so disaster recovery and migration share a code path. The passphrase is the operator's responsibility — Loom never stores it; loss of passphrase means loss of the encrypted backup.

Operator-supplied master-key alternative: `--master-key-file PATH` instead of `--passphrase` at export time wraps with the operator's own KEK (file contents); useful for ops that already have a master-key escrow process.

## Implementation phases

Reordered from rev 1. Phase 2 ships the user-facing CLI **first** (works against existing `loom service`); cluster manifests come in Phase 3 once users have something to deploy with.

### Phase 0 — this spec, revised (current PR #50)

- This document, `Status: design`.
- Update `docs/architecture/service-mode.md` to point at this doc.
- No code changes.

### Phase 1 — image pipeline + token race fix

Independent prereq for everything else. Ship first because Phase 3 cannot land without ARM images.

- `.github/workflows/images.yml`: add `linux/arm64` to buildx matrix; push multi-arch manifest lists to GHCR for all 5 images (worker, service, control-plane, llm-gateway, web).
- `src/loom_cli/service_cmd.py:172+`: `--force-recreate worker` after `_write_env_tokens`.
- Tests:
  - `tests/loom_cli/test_service_cmd.py`: assert the `--force-recreate worker` call follows `_write_env_tokens`.
  - CI: build smoke — workflow job that builds the `arm64` manifest, pulls it via `docker run --platform linux/arm64 ... --version`, asserts non-zero output. Catches missing arch-conditional deps (sentencepiece wheels, etc).
  - CI: lint the workflow YAML with actionlint to catch matrix typos.

### Phase 2 — `loom auth` + `loom providers` + `loom eval` + SSRF defense

Works against existing `loom service`. Cluster work parallel-tracked but not required.

Schema + secret store:
- Migration `0018_provider_connections.py` (down_revision="0017") creates `secrets`, `provider_connections`, `provider_models_cache`.
- `src/loom/db/schema.py`: SQLAlchemy models for all three tables.
- `src/loom/security/secret_store.py`: Protocol + `local-encrypted` impl + `k8s-secret` impl (both ship together — see Secrets §).

API + routes:
- `src/loom_service/routes/provider_connections.py`: 7 routes (create/list/show/update/delete/test/models).
- Gateway: connection resolver, indexed-`updated_at` cache, NOTIFY subscription + 30 s polling fallback.
- Gateway egress is forced through `loom-egress-proxy` unless `LOOM_EGRESS_PROXY_DISABLE=1` (single-box dev opt-out).
- `resolved_egress_ips` re-resolver background task on the gateway (Postgres-level NOTIFY on update).

Egress proxy:
- New container `loom-egress-proxy`: Envoy + Python xDS sidecar.
- `docker/compose.yaml`: add `loom-egress-proxy` service (Phase 2 deliverable). Gateway env updated to dial it.
- `LOOM_EGRESS_PROXY_DISABLE=1` documented in `docs/architecture/service-mode.md` as a security regression for dev.

CLI:
- `src/loom_cli/{auth,providers,eval}_cmd.py`.
- All secret-bearing flags reject literal values at argparse-time; accept only `env:`, `file:`, `-`.

Sandbox network isolation:
- Worker change in `src/loom_worker/sandbox.py` (or equivalent): create sandbox bridge with default-DENY outbound, add iptables rule allowing only the gateway endpoint, inject `HTTPS_PROXY` env var. Tested with a synthetic agent that bypasses `HTTPS_PROXY` — must be blocked at the iptables layer.

Behavior:
- Cost-source 3-way mode (`rate-card` only for anthropic/google; `tokens-only` default for openai-compatible/custom).
- `provider_models_cache` refresh contract (1 h read-triggered; `--refresh` sync).
- Reject-batch-when-no-worker check on `POST /batches`.

Tests:
- SSRF: DNS rebinding via test resolver; sandbox-bypass attempt blocked at iptables; rebound IP rejected at egress proxy.
- Cache correctness: PATCH on replica A invalidates cache on replica B within one `SELECT updated_at` cycle.
- Key never echoed in any `providers` response; trajectory writer scrubs known secret prefixes.
- Multi-arch egress proxy image (ARM + amd64).

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
| Sandbox container bypasses gateway and dials providers directly | HIGH | Two-layer: iptables default-DENY on sandbox bridge + `HTTPS_PROXY` injection. Either alone is bypassable; both together close the path. Phase 2 ships an explicit bypass test. |
| `resolved_egress_ips` goes stale, breaks legitimate provider traffic | HIGH | Background re-resolver (TTL = min(DNS TTL, 300 s)), union with last-24h IPs, NOTIFY + poll fallback to keep proxy live. |
| Cache invalidation across gateway/egress replicas drops a NOTIFY | HIGH | `updated_at` is the authoritative version stamp on every call; NOTIFY is an optimization, polling is the safety net. Stale-cache cannot happen except inside one indexed lookup. |
| ARM image build pipeline blocks Phase 3 | HIGH | Phase 1 ships images first; Phase 3 cannot start without them. Hard prerequisite. |
| Network stack assumptions wrong on Ascent setup | HIGH | Default ingress-nginx + self-signed CA + explicit `--ingress none / --tls-cert` escape hatches. Runbook (Phase 4) walks the typical Ascent layout end-to-end. |
| Trajectory data loss on node failure | LOW (was HIGH in rev 1) | Trajectories canonical in MinIO; hostPath is a write-through cache; per-flush multipart-upload (1 s window). Completed-trial trajectories always recoverable; in-flight trial loses at most 1 s of events. |
| SSRF via `provider_connections.base_url` | HIGH | Egress proxy with IP-allowlist enforcement (not just validation-time check). Defeats DNS rebinding by validating destination IP every call. |
| API key leakage in logs / trajectory | HIGH | SecretStore returns plaintext only to gateway forwarder; gateway redacts in usage rows; trajectory writer scrubs known secret patterns. |
| Control node death = full data loss | HIGH | `--storage external` for production; backup CronJob ships disabled with clear enable instructions; runbook documents RPO. Documented limitation. |
| K8s burden on operators new to k8s | MEDIUM | `loom cluster up` wraps every `kubectl` call. Runbook walks the operator-supplied artifacts (hostfile, optional cert, registry). |
| Worker DaemonSet on control node fights Postgres for IO | MEDIUM | Control node tainted by default; `--co-locate-workers-on-control` opt-in flag. Documented tradeoff. |
| Bootstrap Job admin token in pod logs | MEDIUM | Job deleted immediately after `loom cluster up` reads stdout. journald node-local; documented in runbook. |
| `loom eval` and `loom run` confuse users | MEDIUM | Clear doc cross-link; `loom run` help mentions "no server needed"; `loom eval` help mentions "needs `loom auth login`." |
| Gateway latency from per-call decrypt | LOW | Cache keyed by `(connection_id, updated_at)`; hot path is indexed `SELECT updated_at` (~100 µs) + LRU hit (~5 µs). Cache is correct under partition; rotates immediately on PATCH. |
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

- **2026-06-15 rev 3**: Addresses 24 PR #50 review-of-rev-2 concerns. Severe (1–7), material (8–17), nits (18–24):
  1. **Sandbox → egress-proxy path defined.** Two-layer enforcement: iptables default-DENY on sandbox bridge + `HTTPS_PROXY` injection. Whole new "Sandbox-to-egress-proxy traffic path" section with the diagram.
  2. **Multi-replica cache correctness.** Cache keyed by `(id, updated_at)`; every call does an indexed lookup (~100 µs); NOTIFY is an optimization, polling is the safety net. The TTL-based LRU in rev 2 was incorrect under partition; replaced.
  3. **NOTIFY + 30 s polling fallback** for both gateway cache and egress-proxy IP allowlist. LISTEN drops no longer cause stale state.
  4. **Upgrade tarball encrypted** with operator-supplied passphrase via age; master key wrapped, never plaintext. `--master-key-file` alternative for KEK-escrow ops.
  5. **`secrets` table** added to migration 0018 schema (`ref`, `ciphertext`, `nonce`, `master_key_version`).
  6. **Worker concurrency model** spelled out: one DaemonSet pod per node, `LOOM_WORKER_CONCURRENCY` env var, default formula based on CPU/RAM. DRF advertises max_concurrent, not 1.
  7. **`resolved_egress_ips` re-resolver**: background job, TTL = min(DNS TTL, 300 s), union with last-24h IPs.
  8. **Both SecretStore impls ship in Phase 2.** `local-encrypted` for user-API-key data path; `k8s-secret` for bootstrap creds. `k8s-secret.rewrap()` documented as no-op.
  9. **Cost rate-card limited to anthropic/google only.** openai-compatible defaults to `tokens-only`; rate-card-by-URL-sniffing dropped.
  10. NetworkPolicy section reworded; sandbox boundary mechanism (iptables + HTTPS_PROXY) made explicit instead of hand-waved.
  11. Goal #5 reworded to match data architecture invariant (completed trials always recoverable; in-flight loses ≤1 s).
  12. Argv-secret rule made uniform: `--token` and `--api-key` both accept only `env:`, `file:`, `-`. Literal values rejected at argparse-time.
  13. `--combinations FILE` JSON schema documented with example.
  14. `LOOM_EGRESS_PROXY_DISABLE=1` escape hatch for single-box dev, documented as a security regression.
  15. `resolved_egress_ips` schema changed from JSONB to native `inet[]`.
  16. TLS termination (ingress) vs TLS validation (egress) split into separate rows in the Network stack table.
  17. `disabled-pricing` removed from `hidden_reason` enum (it was a display issue, not a visibility issue).
  18. Bench-cache LRU eviction skips in-use slugs via a flock-refcounted `.in-use/` symlink registry; stale entries reaped on worker startup.
  19. `docker/compose.yaml` egress-proxy service added as an explicit Phase 2 deliverable.
  20. Egress-proxy replica count documented as differing by mode (compose: 1; k8s: ≥2 + topology spread).
  21. Phase 1 test plan expanded: arm64 build smoke + actionlint workflow lint.
  22. Risks table adds: sandbox-bypass-attempt, `resolved_egress_ips` staleness, NOTIFY-drop. Trajectory + cache risks updated to LOW.
  23. Non-interactive `loom auth login` flow documented (CI example included).
  24. Table of contents added.
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
