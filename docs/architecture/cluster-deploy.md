# Cluster deployment design

**Status: design** (2026-06-15, revision 11). Tracking PR: [#50](https://github.com/carinrc/loom/pull/50). Supersedes inline guidance in `docs/architecture/service-mode.md`.

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
- **CRI-native sandbox execution.** The design uses `docker.sock` on every worker node to spawn sandbox containers + manage per-trial Docker bridges. This is a hard prerequisite (see [Prerequisites](#prerequisites)); a CRI-native rewrite (sandbox-pods via the kubelet's containerd socket + per-pod NetworkPolicies) is a separate future effort.

## Prerequisites

`loom cluster` requires the following on every worker node, in addition to a working Kubernetes cluster:

| Prerequisite | Why | Verification |
|---|---|---|
| **Docker engine installed** (alongside the CRI k8s uses) | Worker spawns sandbox containers + manages per-trial bridges via `docker.sock`. On containerd-only nodes (kubeadm 1.24+, k3s, EKS, GKE Autopilot — i.e., most modern k8s) Docker is NOT installed by default; operator must install it as a separate package. The k8s CRI (containerd) and the operator-installed Docker engine coexist on different sockets. | Preflight (see below). |
| **Pod Security Standards on the `loom` namespace = `privileged`** | The worker DaemonSet, the `loom-llm-gateway-sandbox` singleton's worker shim, the egress proxy, and the preflight Job all need hostPath mounts and/or `hostNetwork: true`. PSS "Baseline" or "Restricted" block these. **Operator responsibility**: do NOT deploy non-loom workloads into the `loom` namespace — they'd inherit privileged access. Co-tenanting workloads belong in their own namespace. Documented in runbook. | Preflight `kubectl get ns loom -o jsonpath='{.metadata.labels.pod-security\.kubernetes\.io/enforce}'`; must be `privileged` or unset. |
| **`hostPort: 30443` reachable** from each node's Docker `loom-uplink` bridge | Singleton dials the gateway via this port. `hostPort` typically binds on all host interfaces (CNI portmap installs DNAT for "node-local" IPs), but Docker bridge IPs aren't always in that set; behavior depends on CNI + iptables chain order. | Preflight TCP-connect probe from a temporary container on `loom-uplink` to `<bridge-gw>:30443`. |
| **`hostPort: 30443` not occupied** by another DaemonSet | Conflict → pod stays Pending with `FailedScheduling`. | Preflight `ss -tlnp \| grep :30443` (via `hostNetwork: true`). |
| **`10.42.0.0/16` (or operator-supplied CIDR) free on every worker node** | Per-trial bridges are allocated from this range; collisions with VPN routes, existing Docker workloads, or k8s pod CIDRs break trial setup. | `loom cluster up --sandbox-cidr 10.X.0.0/16` flag for operators with conflicts; default `10.42.0.0/16`. Preflight `ip route` parse. |
| **`etcd` encryption at rest** (or explicit `--allow-plaintext-etcd` opt-out) | See [K8s manifest changes §](#k8s-manifest-changes-loom-cluster). | Attestation flag (see that §). |

**Hard incompatibility — managed-k8s offerings that lock down hostPath/hostNetwork.** EKS Fargate, GKE Autopilot, Cloud Run / Knative-only environments enforce PSS Restricted with no escape hatch — operators cannot relabel the namespace, cannot install Docker on nodes (no node access), cannot mount hostPaths. **These environments cannot use `loom cluster` at all.** They can use `loom service` (single-box) instead. This is a real and documented limitation, not a workaround opportunity; the Job-based preflight is not a way around it.

**Preflight mechanism — operator-selectable:**

```
loom cluster up --preflight-via=job|ssh   # default: job
```

- **`job` (default)**: `loom cluster up` applies a one-shot k8s Job in the `loom` namespace with `parallelism = N_workers`, `topologySpreadConstraints: {maxSkew: 1, topologyKey: kubernetes.io/hostname, whenUnsatisfiable: DoNotSchedule}` so one pod lands on each worker without requiring pre-existing role labels (those don't exist until apply). Image `ghcr.io/carinrc/loom-preflight:<version>` (~5 MB Alpine + Go). `hostNetwork: true` + hostPath mount of `/var/run` (the directory, NOT `/var/run/docker.sock` directly — the latter fails to mount when Docker isn't installed, leaving the pod stuck in `ContainerCreating`). Container then `test -S /host/var/run/docker.sock` for the actual check. `hostNetwork: true` gives the pod direct access to the host's port table for `ss -tlnp` and to the host's network namespace for the bridge connectivity probe. Reports JSON per-node to a ConfigMap; CLI reads via the Job's `status.conditions: Complete` signal (canonical "done" marker — ConfigMap is for details, not for the completion signal). Job pod selects on `kubernetes.io/hostname IN <hostfile-derived list>` so only declared workers run preflight.
- **`ssh`**: legacy / dev path — runs the same checks via SSH against each host in the hostfile. Requires operator SSH keys on every worker. Faster for small setups; doesn't require image pull.

Both modes batch failures: `loom cluster up` collects results for every worker before reporting, so the operator sees the full punch list ("workers 2 and 4 missing Docker; worker 3 has hostPort 30443 occupied by Prometheus; namespace `loom` not labelled PSS privileged") and can fix everything before retrying.

The Job-based path helps in clusters where SSH isn't available BUT PSS still allows hostPath/hostNetwork (e.g., self-managed kubeadm + bastion-only SSH access). It does NOT help in managed offerings that block PSS-privileged.

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
┌────────── Control node (ascent-0, tainted) ──────────┐    ┌── Worker node × N ──────────────────────┐
│  postgres   (StatefulSet, 1 replica, local PVC)       │    │  loom-worker (DaemonSet pod)            │
│  minio      (StatefulSet, 1 replica, local PVC)       │    │  loom-llm-gateway-sandbox               │
│  loom-service / control-plane / llm-gateway / web     │    │      (Docker singleton on two bridges:  │
│      (Deployments, ≥2 replicas, spread)               │ ←→ │       loom-uplink + per-trial. Managed  │
│  loom-egress-proxy (Deployment, ≥2)                   │200G│       by worker, NOT a k8s pod — see    │
│  loom-egress-xds   (Deployment, 1)                    │bps │       Sandbox→gateway §.)               │
│  loom-gateway-router (DaemonSet, hostPort:30443)      │    │  loom-gateway-router (DaemonSet pod;    │
│                                                       │    │      receives singleton CONNECT, fwds   │
│                                                       │    │      to in-cluster gateway)             │
│  loom-worker (DaemonSet pod)                          │    │  docker.sock (hostPath)                 │
│      [DISABLED by default; --co-locate-              │    │  bench-cache (hostPath, read-           │
│       workers-on-control to opt in]                   │    │      through cache of MinIO)            │
│  ingress controller (nginx)                           │    │  trajectory-cache (hostPath,            │
│                                                       │    │      write-through to MinIO)            │
└───────────────────────────────────────────────────────┘    └─────────────────────────────────────────┘

SPA (`loom-web`) is in the manifest set but the Deployment ships
with `replicas: 0` for the cluster rollout — SPA development is
paused per the project roadmap. Operators opt in via
`kubectl scale deploy/loom-web --replicas=2` once SPA work resumes.
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

**Read-through model for benchmarks**: worker checks `bench-cache/<slug>/<version>/.complete`; if missing, downloads from MinIO and atomically renames `.tmp` → final + writes the marker. Concurrent first-fetches: per-slug-version `flock(2)` prevents thundering herd (assumes local FS — current spec doesn't put bench-cache on NFS; if a future operator wants NFS-backed hostPath, they need a different lock or to disable shared caching). Eviction: LRU when `/var/lib/loom/benchmarks/` exceeds an operator-configurable quota (`LOOM_BENCH_CACHE_QUOTA_GB`, default 200).

**In-use registry + locking discipline (TOCTOU-safe).** `/var/lib/loom/benchmarks/.in-use/<slug>__<version>__<pid>` files; eviction loop groups by `<slug>__<version>` and skips any group with at least one live PID. The eviction loop and the trial-start path serialize through a shared `bench-cache/.lock/<slug>__<version>` flock:

- **Trial start**: acquire flock on `.lock/<slug>__<version>` (shared/`LOCK_SH`); write `.in-use/<slug>__<version>__<pid>`; mount the cache dir into the sandbox; release flock.
- **Eviction**: acquire flock (exclusive/`LOCK_EX`) on `.lock/<slug>__<version>`; check `.in-use/<slug>__<version>__*` for live PIDs (via `kill(pid, 0)`); if none, unlink the cache dir; release flock.

The flock pair forces trial-start and eviction to interleave correctly: a trial mid-mount holds the shared lock; eviction blocks until the trial releases (i.e., until the in-use marker is on disk and visible). Stale in-use entries from SIGKILL'd workers are reaped on worker startup by walking `.in-use/`, calling `kill(pid, 0)` on each, and unlinking entries whose PID is gone. flock itself is released automatically on process death (Linux ties flock to the holding fd; SIGKILL closes all fds), so no stale-flock recovery is needed.

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
    [--backup-target s3://...]                                         # required to enable backup-cronjob
    [--registry URL]                                                   # default: ghcr.io/carinrc
    [--ingress nginx|none] [--tls-cert PATH --tls-key PATH]
    [--co-locate-workers-on-control]                                   # opt-in; otherwise control node is tainted
    [--etcd-encryption-attested | --allow-plaintext-etcd]              # one is required; see runbook for verification
    [--worker-concurrency N]                                           # default: CPU/RAM-derived (see Worker concurrency §)
    [--sandbox-cidr 10.42.0.0/16]                                      # per-trial bridge subnet pool; override for CIDR conflicts
    [--gateway-host-port 30443]                                        # hostPort for loom-gateway-router; configurable for clusters with port-range policy
```

**Rename decision (revised):** keep `loom service` (single-box). Add `loom cluster` for k8s. No unified `loom deploy` verb family. Reasons: less churn on existing docs/scripts, asymmetric verbs match other tools (`docker run` vs `docker-compose up`), and `service` accurately describes the single-box mode (it IS a service, not a deployment). Reversed from rev 1.

### User-facing CLI (Phase 2 — before cluster work)

`loom auth`, `loom providers`, `loom eval` work against **any deployed Loom** — they don't require `loom cluster`. We ship them first so they're usable against an existing `loom service` install while the cluster work proceeds.

```
loom auth login --server URL --token {env:VAR | file:PATH | -}    # --token REQUIRED; no interactive paste fallback
loom auth status                                                   # for CI: passes if logged in, fails otherwise
loom auth logout

loom providers create --name N --type {openai-compatible,anthropic,google,custom} \
    --base-url URL --api-key {env:VAR | file:PATH | -}             # never literal
    [--allowed-models LIST]
    [--input-usd-per-1m FLOAT --output-usd-per-1m FLOAT]           # both-or-neither; argparse validator
loom providers list / show NAME / test NAME
loom providers models NAME [--refresh] [--hide MODEL] [--unhide MODEL]
loom providers update NAME [--base-url URL] [--api-key {env:VAR | file:PATH | -}]
    [--allowed-models LIST] [--input-usd-per-1m FLOAT --output-usd-per-1m FLOAT]
loom providers delete NAME

loom eval run --provider N --model M --agent A --benchmark B
    [--task ID | --task-filter JSON] [--backend B] [--name N]
loom eval batch create --provider N --model M --agent A --benchmark B
    [--combinations FILE | --task-filter JSON] [--concurrency N] [--name N]
loom eval batch {list,show,cancel} [--state S]
loom eval trial {list,show} | trajectory ID [--out PATH] | atif ID [--out PATH]
```

CLI-to-route mapping for `loom providers models`:

| CLI | Route |
|---|---|
| `models NAME` | `GET /provider-connections/{id}/models` (returns cache; 1 h read-triggered background refresh) |
| `models NAME --refresh` | `POST /provider-connections/{id}/models:refresh` (synchronous; returns refreshed cache) |
| `models NAME --hide MODEL` | `POST /provider-connections/{id}/models/{model_id}:hide` |
| `models NAME --unhide MODEL` | `POST /provider-connections/{id}/models/{model_id}:unhide` |

**Argv hygiene rule (uniform):** every secret-bearing flag (`--token`, `--api-key`) accepts ONLY `env:VAR`, `file:PATH`, or `-` (stdin). Literal values are rejected at argparse-time with a clear error pointing at the indirection forms. No interactive-paste fallback for `--token`: `loom auth login` requires the flag explicitly, so scripts can't accidentally hang waiting for stdin and humans always read a clear "missing required argument" error if they typo it. Reasons: shell history, `ps` listings, CI log capture all leak argv.

**Pricing argv validation:** `--input-usd-per-1m` and `--output-usd-per-1m` are interdependent — set both or neither. An argparse custom action enforces this with a clear error ("--input-usd-per-1m requires --output-usd-per-1m"); the route layer additionally validates that `pricing_source='operator-supplied'` rows have non-null, non-negative `pricing_data.{input_usd_per_1m, output_usd_per_1m}` before accepting the row.

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

Each entry overrides the corresponding `loom eval batch create` flag for that combination. `provider_connection_id` / `provider_model_id` per entry allow cross-provider sweeps within one batch. UUIDs come from `loom providers show NAME --format=id` or `loom providers list --format=json`.

**First-time human login (no interactive paste):** users export the token once and reference it by env var, e.g.,
```bash
export LOOM_TOKEN="loom_admin_..."        # paste once into your shell rc / 1password CLI
loom auth login --server https://loom.cluster.local --token env:LOOM_TOKEN
```
The literal-value rejection is deliberate (#21 review concern from rev 4): scripts can't typo their way into a hung-stdin; humans get a clear "missing required argument" error rather than a silent prompt; `ps`/history don't leak the token.

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
display_name                text -- UNIQUE per (team_id, display_name) WHERE deleted_at IS NULL
base_url                    text
upstream_host               text -- derived from base_url at create/PATCH (re-derived if base_url changes)
resolved_egress_ips         inet[] -- native Postgres array; populated by re-resolver background job
egress_ips_refreshed_at     timestamptz
egress_ips_min_ttl_seconds  int NOT NULL DEFAULT 300   -- operator FLOOR; actual TTL = max(DNS TTL, this)
encrypted_api_key_ref       text -- ref into `secrets` table for local-encrypted; "k8s://ns/name" for k8s-secret
allowed_models              text[] | NULL    -- NULL = "all that upstream returns"
status                      text -- 'pending' | 'valid' | 'invalid' | 'disabled'
last_validated_at           timestamptz | NULL
last_validation_error       text | NULL
pricing_source              text -- 'rate-card' | 'tokens-only' | 'operator-supplied'
pricing_data                jsonb | NULL    -- {input_usd_per_1m, output_usd_per_1m}; route validates shape on operator-supplied
created_by                  text -- "<token-type>:<token-id-suffix>"
deleted_at                  timestamptz | NULL  -- soft-delete; routes filter WHERE deleted_at IS NULL
created_at, updated_at      timestamptz
```

**Soft-delete semantics (replaces rev 3's ON DELETE SET NULL):** `DELETE /provider-connections/{id}` sets `deleted_at = now()`. The row stays — Trial / Batch / usage FKs remain valid for billing + audit. Active-listing routes (`GET /provider-connections`) filter `WHERE deleted_at IS NULL`. Re-creating a connection with the same `display_name` is allowed (the partial UNIQUE index excludes soft-deleted rows). In-flight trials continue with the cached decrypted key (Gateway LRU); new trial claims after delete fail-fast with `provider_connection_deleted`. Hard-delete is a separate ops step (`loom admin providers purge --id ID --older-than 90d`) that only runs on rows with no referring trial activity.

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
  provider_connection_id    UUID | None  -- null = platform default provider (env-keyed)
  provider_model_id         str | None

Batch._CreateBatch:
  provider_connection_id    UUID | None  -- one-of with per-Combination override
  provider_model_id         str | None

Trial DB row:
  provider_connection_id    UUID | None FK  -- no cascade; soft-delete on provider_connections keeps this valid
```

Delete semantics live with the `provider_connections` schema above (soft-delete pattern). The Trial FK has no cascade rule because the parent never hard-deletes.

## Secrets, SSRF, and the Gateway hot path

### `SecretStore` Protocol

`src/loom/security/secret_store.py`:

```python
class SecretStore(Protocol):
    async def put(self, *, namespace: str, key: str, value: str) -> str: ...        # → ref
    async def get(self, ref: str) -> str: ...
    async def delete(self, ref: str) -> None: ...
    async def list_refs(self, *, namespace: str | None = None) -> AsyncIterator[str]: ...
    async def rewrap(self, ref: str, *, new_master_key: bytes) -> str: ...           # returns new ref
```

All methods are `async` because callers are FastAPI / async (`loom_service`, `loom_llm_gateway`); sync calls would block the event loop. The two impls (`local-encrypted`, `k8s-secret`) wrap their backends accordingly (asyncpg for Postgres, `kubernetes_asyncio` for k8s).

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

**Cache invalidation on rewrap:** in the same transaction as the `secrets` table update, the rewrap walker bumps `provider_connections.updated_at` (and emits `NOTIFY provider_connections_changed, '<id>'`) for every connection whose `encrypted_api_key_ref` points to a rewrapped row. This is the load-bearing detail that makes rotation safe across multi-replica gateway caches: cache key includes `updated_at`, so after rewrap every replica misses on the next call and refetches with the new master. Without this, cached plaintext keeps serving but the ciphertext requires the new master — and once the `PREV_MASTER_KEY` cutover window ends, cache misses fail. Test plan in Phase 5 covers the cutover.

### Sandbox→gateway auth and routing

The security pivot. Trial sandbox containers are spawned by the worker via docker.sock onto a Docker bridge network — they are NOT k8s pods. Without explicit routing, a sandbox can dial the public internet directly via the Docker bridge → host → upstream, bypassing the gateway and the egress proxy.

Two design choices are needed: (a) network isolation and (b) authentication. Rev 4 of this spec described a CONNECT-proxy + JWT-injection mechanism that doesn't compose (CONNECT proxies can't inject headers into TLS-encrypted upstream traffic; the `--internal` bridge and `--network container:` namespace sharing are mutually exclusive). Rev 5 ships **Path B — "gateway as base URL"** — which works with stock SDKs and avoids both bugs.

#### (a) Network isolation: per-trial `--internal` bridge + persistent uplink bridge

The sandbox-endpoint is a **worker-spawned Docker container, not a k8s pod**. Rev 5 described it as a DaemonSet pod that "joins each trial bridge on demand," but `docker network connect` to a kubelet-managed pod doesn't work on containerd-based clusters. Rev 6 tried `--network host` on the singleton, but Docker rejects host-namespaced containers being attached to additional networks (`Container sharing network namespace with another container or host cannot be connected to any other network`). Rev 7 ships a two-bridge model that satisfies both constraints.

Rev 7 design — the singleton runs on bridge networks only (NOT host network), with two attachments:

| Attachment | Purpose | Created by |
|---|---|---|
| Per-trial `sandbox-<trial_id>` (`--internal`) | Where the sandbox dials the singleton. `--internal` denies egress to the host / internet from any container on it. | Worker, per trial. |
| Persistent `loom-uplink` (NOT internal) | Where the singleton dials the in-cluster gateway. The bridge's gateway IP is the host's IP; the host can route k8s Service IPs (kube-proxy) or NodePorts. | Worker, once at boot. |

Gateway reachability: the singleton dials `<host-bridge-gateway-ip>:30443` (or operator-configured port) on the `loom-uplink` bridge. **Implementation choice: `hostPort`, not NodePort.** A dedicated per-node `loom-gateway-router` DaemonSet pod has `hostPort: 30443` (forwards into the in-cluster `loom-llm-gateway` Service). `hostPort` is reachable from Docker bridges in most kubeadm/k3s setups, but the guarantee is conditional: CNI's portmap plugin installs iptables DNAT rules that match destination IPs the CNI considers "node-local," and Docker bridge IPs are typically — but not always — recognized as such. Behavior depends on CNI plugin choice (calico, cilium, flannel) and iptables chain ordering.

`loom cluster up` ships a **preflight connectivity probe**: a one-shot Docker container on `loom-uplink` that opens TCP to `<loom-uplink-gateway-ip>:30443` before the singleton starts. Fail-fast with a runbook link explaining CNI portmap configuration if the path doesn't work. Port configurable via `loom cluster up --gateway-host-port 30443`.

`loom-uplink` gateway IP is determined by Docker IPAM. Worker boot:
1. `docker network create --driver bridge loom-uplink || true` (idempotent — survives Docker daemon restart; the bridge persists across reboots once created).
2. Discover: `GW=$(docker network inspect loom-uplink --format '{{(index .IPAM.Config 0).Gateway}}')`.
3. Pass `LOOM_GATEWAY_URL=https://${GW}:30443` to the singleton at `docker run` time.

If the operator forces a `loom-uplink` re-creation (rare; only on explicit `docker network rm` + recreate), Docker may pick a different subnet → different gateway IP → singleton's env stale. Worker watches for this via a 60 s loop that runs `docker network inspect loom-uplink --format '{{(index .IPAM.Config 0).Gateway}}'`; on mismatch with the cached gateway IP, worker calls `docker stop loom-llm-gateway-sandbox && docker run -d ... -e LOOM_GATEWAY_URL=https://${NEW_GW}:30443 ...` to recreate the singleton with the fresh env. 60 s loop overhead is one daemon call per worker per minute; worker logs this at `debug` level only (info-level would flood logs ~5760 messages/node/day on a stable cluster).

Worker boot (once per node):
1. `docker network create --driver bridge loom-uplink` — persistent bridge; the host has an IP on it (Docker IPAM allocates), the `loom-gateway-router` pod's `hostPort: 30443` is reachable from this IP.
2. Discover the bridge gateway IP: `GW=$(docker network inspect loom-uplink --format '{{(index .IPAM.Config 0).Gateway}}')`. Pass to singleton as env.
3. Read public-key JWT material: worker bind-mounts the k8s-managed ConfigMap volume (mounted into the worker pod itself at `/etc/loom/jwt-public/`) to a host path `/var/lib/loom/jwt/` via an in-worker copy step on update notification; this becomes the bind-mount source for the singleton.
4. `docker run -d --name loom-llm-gateway-sandbox --network loom-uplink --restart unless-stopped -v /var/lib/loom/jwt:/var/lib/loom/jwt:ro -v /var/lib/loom/sandbox-tls:/etc/loom/tls:ro -e LOOM_GATEWAY_URL=https://${GW}:30443 <image>`. Singleton listens on `0.0.0.0:8443` for incoming sandbox traffic. Only the singleton is attached to `loom-uplink` (operator must not put other workloads on this bridge — documented in runbook).
5. Worker monitors the singleton via `docker events`; restarts on crash.

Worker at trial start:
1. **Discover in-use subnets** (worker-restart-safe): `docker network ls --filter name=sandbox- --format '{{.Name}}'` + `docker network inspect` on each → set of in-use `/24` slots in `10.42.0.0/16`. Allocate the lowest free `<trial_index>`.
2. `docker network create --driver bridge --internal --subnet 10.42.<trial_index>.0/24 --gateway 10.42.<trial_index>.1 sandbox-<trial_id>` — per-trial bridge. `--internal` blocks egress to host and internet from any container attached.
3. `docker network connect --ip 10.42.<trial_index>.2 sandbox-<trial_id> loom-llm-gateway-sandbox` — singleton joins the per-trial bridge at a pinned IP. The singleton's listener on `0.0.0.0:8443` is now reachable from both `loom-uplink` and `sandbox-<trial_id>`.
4. Run the sandbox container with `--network sandbox-<trial_id>` + `--add-host loom-sandbox-gateway.local:10.42.<trial_index>.2`. Sandbox can reach the singleton at the per-trial bridge IP and nothing else.
5. At trial end: `docker network disconnect sandbox-<trial_id> loom-llm-gateway-sandbox` + `docker network rm sandbox-<trial_id>`. Subnet slot freed for the next trial.

Container count: O(nodes) singletons + O(concurrent_trials) sandboxes. No per-trial sidecar; no `CAP_NET_ADMIN`; no host iptables; no host network mode (which Docker would refuse). The singleton's two-bridge attachment is supported and standard.

Why NodePort (not direct ClusterIP): the singleton is a Docker container on bridge networks; its routes know about Docker bridges, not k8s Service CIDRs. The host's NodePort rules expose every Service on every node's IP. The host-bridge-gateway IP (the host's IP on `loom-uplink`) is routable from the singleton, and dialing `<that-ip>:30443` hits the local kube-proxy's NodePort DNAT into the Service. One hop, no `--network host`.

**Subnet allocator restart safety**: on worker boot, the discovery step (1 above) rebuilds the in-use set from `docker network ls`. The allocator is stateless — no on-disk index that can desync with reality. Multiple workers don't collide because there is one worker DaemonSet pod per node and each node has its own `10.42.0.0/16` to allocate from.

#### (b) Authentication: gateway-as-base-URL with step-JWT as API key

Stock SDKs accept both a base-URL redirect and an API-key env var. The worker injects every relevant variant into the sandbox so SDK version drift doesn't break the redirect:

```
# Base URL — all variants the SDKs in scope accept, in case different versions
# read different env names. Phase 2 deliverable verifies each name against the
# SDK's released docs and trims this list to the canonical ones.
OPENAI_BASE_URL          = https://loom-sandbox-gateway.local:8443/openai/v1
ANTHROPIC_API_URL        = https://loom-sandbox-gateway.local:8443/anthropic
ANTHROPIC_BASE_URL       = https://loom-sandbox-gateway.local:8443/anthropic   # accepted by some forks
GOOGLE_GENAI_API_BASE    = https://loom-sandbox-gateway.local:8443/google
GOOGLE_GEMINI_BASE_URL   = https://loom-sandbox-gateway.local:8443/google      # accepted by older SDKs

# API key — SDK injects this into the appropriate header.
OPENAI_API_KEY     = <step-JWT>    # → Authorization: Bearer <step-JWT>
ANTHROPIC_API_KEY  = <step-JWT>    # → x-api-key: <step-JWT>
GOOGLE_API_KEY     = <step-JWT>    # → Authorization (varies by SDK version)

# CA trust — used by httpx/requests/urllib for TLS validation.
SSL_CERT_FILE         = /etc/ssl/loom-ca/loom-ca.crt
REQUESTS_CA_BUNDLE    = /etc/ssl/loom-ca/loom-ca.crt
NODE_EXTRA_CA_CERTS   = /etc/ssl/loom-ca/loom-ca.crt    # for any Node-based agents
```

The worker bind-mounts the loom-ca cert into every sandbox at `/etc/ssl/loom-ca/loom-ca.crt` (read-only) and `--add-host loom-sandbox-gateway.local:<per-trial-IP>` so the hostname resolves to the singleton's IP on the trial's bridge. The cert SAN has exactly one entry: `loom-sandbox-gateway.local`. This avoids the unbounded-SAN problem of cert-per-bridge-IP — one hostname, per-trial `/etc/hosts` injection, one cert.

Sandboxes built from arbitrary base images (SWE-Bench eval images, OSWorld VMs, user-supplied) get the same `SSL_CERT_FILE` redirect; Python's `httpx`/`requests`/`urllib`, Node's stock TLS, Go's `crypto/tls` all honor a CA bundle override. **Limitation:** SDKs that hardcode a CA bundle path (rare; some statically linked Go binaries) ignore the env var; those agents can't make LLM calls through the facade. Operators discover this at task-onboarding time.

**JWT in env is visible to the agent.** The `OPENAI_API_KEY=<step-JWT>` env var is readable by the agent process (`/proc/self/environ`). A malicious agent could log the JWT, embed it in trajectory events, or echo it via the model's response. Step-JWTs are short-lived (3600 s) and scoped to (trial_id, team_id, provider_connection_id, step_id) — exfiltration grants no cross-trial / cross-team access; the worst case is replaying the JWT into the same step's call window, which the gateway is already serving for the legitimate sandbox.

Mitigations shipped Phase 2:
- **Trajectory writer scrubs** `Authorization`, `x-api-key`, `Proxy-Authorization` headers AND env-var values where the key matches `*_API_KEY` / `*_TOKEN` / `*_SECRET`. Pattern is intentionally broad — it catches `LOOM_TOKEN` and other internal credentials too (correct: those should not be in trajectories either). The serializer applies these scrubs before any event reaches MinIO. Tested with a synthetic agent that includes its env in a tool_call output.
- **Agent self-logging** (LangChain/LlamaIndex's "explain my config" features) is the easy leak path; the agent-runtime base image disables verbose-config dumps in the SDK constructors it ships.
- **Known limitation**: pattern-based scrubbing catches structured headers + env values. A determined exfiltration via `tool_call({"args": "my JWT is <step-JWT>"})` embeds the credential in a regular string field of a JSON event — that bypasses pattern matching. Acceptable for trusted-users threat model; hostile-tenant adaptations would need bidirectional payload inspection (egress proxy DLP filter), out of scope here.

Threat model: trusted users, untrusted prompts — appropriate for Loom; not appropriate for hostile-tenant SaaS. Hostile-tenant adaptations (per-call short-lived JWTs minted per-request via a sandbox-side helper that signs requests with a per-trial key) are future work.

```
┌─ sandbox container (on per-trial --internal bridge) ──┐
│  OPENAI_BASE_URL=https://loom-sandbox-gateway.local:  │   stock SDK honors *_BASE_URL +
│      8443/openai/v1                                    │   uses its *_API_KEY in headers.
│  OPENAI_API_KEY=<step-JWT>                             │   No SDK changes, no HTTPS_PROXY.
│  SSL_CERT_FILE=/etc/ssl/loom-ca/loom-ca.crt            │
│  /etc/hosts: loom-sandbox-gateway.local → 10.42.N.2    │   worker --add-host injects
│  agent dials https://loom-sandbox-gateway.local:8443   │   per-trial bridge IP.
└──────────────────┬─────────────────────────────────────┘
                   │ HTTPS (TLS validates via loom-ca)
                   ▼
┌─ loom-llm-gateway-sandbox (worker-spawned Docker      ┐
│  singleton per node on TWO bridges: loom-uplink +     │   TLS-terminates, validates step-JWT,
│  per-trial sandbox-<trial>. NOT --network host        │   forwards body to gateway via
│  (Docker rejects host + bridges together).            │   loom-uplink → host-bridge-gw IP →
│  worker --restart unless-stopped on the worker pod.   │   NodePort 30443 → kube-proxy.
└──────────────────┬─────────────────────────────────────┘
                   │ HTTPS NodePort, host-bridge-gw:30443
                   ▼
┌─ loom-llm-gateway (k8s Service, ≥2 replicas) ─────────┐
│  resolves connection, decrypts API key, rate-limits,  │   trust boundary: knows team_id,
│  calls upstream via egress proxy                       │   applies per-team rate, records
└──────────────────┬─────────────────────────────────────┘   usage.
                   │ HTTPS CONNECT to (target_ip, 443)
                   │   X-Loom-Connection-Id: <uuid>
                   │   X-Loom-Team-Id:       <uuid>
                   ▼
┌─ loom-egress-proxy (Envoy, ≥2 replicas) ──────────────┐
│  validates target_ip ∈ resolved_egress_ips            │   second wall: even with a
│  [connection_id]; per-team local_ratelimit keyed on   │   compromised gateway, only
│  X-Loom-Team-Id                                        │   operator-resolved IPs are dialed.
└──────────────────┬─────────────────────────────────────┘
                   │ HTTPS to provider IP
                   ▼
            OpenAI / Anthropic / Google
```

Design properties:

- **No TLS interception.** The sandbox terminates TLS at the singleton (legitimately — the singleton IS the endpoint as far as the SDK is concerned). No per-trial CA bootstrap, no MITM.
- **No header injection during CONNECT.** The SDK's `Authorization` / `x-api-key` header carries the JWT in plain HTTP (decrypted at the TLS terminator on the singleton). Standard SDK paths.
- **SSE streaming.** OpenAI/Anthropic streaming responses (`text/event-stream`) pass through the gateway facade via FastAPI `StreamingResponse`; the singleton + gateway forward chunks unbuffered. No buffer-and-flush, so first-byte latency matches a direct provider call within ~5 ms.
- **JWT lifetime + refresh.**
  - JWT lifetime: 3600 s. Issued by control-plane at step start; verified by gateway; signing key in `k8s-secret`.
  - **Signing-key rotation: dual-key validation window.** Two keys (`current` + `previous`) are valid simultaneously; verifier accepts either. Rotation: write `current` → `previous`, mint new `current`, gateway picks both up via secret watcher. `previous` retired after `2 × JWT_LIFETIME` (= 2 h default) so all in-flight JWTs have expired. Without dual-key, rotation invalidates every in-flight step.
  - **Signing scheme: asymmetric (EdDSA / Ed25519).** Control-plane holds the private key in `k8s-secret`; the gateway and the per-node sandbox singletons hold only the public key. Control-plane's ServiceAccount needs `get`/`watch` on `Secret loom-step-jwt-keys-private` in the `loom` namespace (RBAC in `control-plane-rbac.yaml`). Without this, control-plane can't mint step JWTs at trial start.
  - **JWT validity envelope after rotation**: with `JWT_LIFETIME = 3600 s` (1 h) and `dual_window = 2 h`, an in-flight step holding a JWT minted right before rotation starts can still be served for up to `JWT_LIFETIME + dual_window − propagation_lag ≈ 2.5 h` total. Operators planning rotations during low-traffic windows should budget this overlap.
  - Public-key distribution to singletons (two-hop):
    1. k8s mounts the `loom-jwt-public-keys` ConfigMap (`current.pem` + `previous.pem`) into the worker pod at `/etc/loom/jwt-public/` (k8s auto-updates the volume contents on ConfigMap change, ~60 s lag).
    2. Worker fsnotify-watches `/etc/loom/jwt-public/`; on change, copies atomically to host path `/var/lib/loom/jwt/` (worker is k8s-mounted to host hostPath for this purpose). No k8s API watch from the worker — RBAC stays minimal.
    3. Singleton bind-mounts `/var/lib/loom/jwt/` read-only; Go binary fsnotify-watches the directory + reloads its in-memory key set. fsnotify listens for IN_CLOSE_WRITE, IN_MOVED_TO, AND IN_MODIFY events to cover both atomic-rename rotation (most common; worker pattern is write-tmp + rename) and direct overwrite.
    4. Total propagation: k8s update + ~60 s + worker copy + singleton reload ≈ 60–90 s. Acceptable for rotation (cutover window is 2 h).
  - **FIPS-compliant deployments**: Ed25519 is in FIPS 186-5 (2023) but adoption lags; some compliance baselines still mandate ECDSA P-256 (FIPS 186-4). One-line config change to swap the JWT signing algorithm; covered in the runbook.
  - **Singleton's TLS server cert (separate from JWT keys).** The singleton presents a TLS server cert for `loom-sandbox-gateway.local` signed by loom-ca. Cert + private key live in a k8s Secret `loom-sandbox-tls`; worker writes to host path `/var/lib/loom/sandbox-tls/` with mode `0440`, owner `root`, group `loom` (gid 64055, fixed by `loom cluster up`). Singleton runs as user `loom-sandbox` (uid 64055) which is in group `loom` — can read but not write. Worker bind-mounts read-only into the singleton container. Singleton is a non-root, dedicated-user process; no shell, no exec, no docker.sock access. **Rotation is a brief in-flight-call interruption**: `docker restart loom-llm-gateway-sandbox` kills active TLS sessions including any streaming SSE responses mid-flight. Schedule rotations during low-activity windows; the runbook documents a graceful pattern (drain new claims for 60 s, restart, re-enable claims). Aligns with loom-ca rotation lifecycle (typical 5-year CA, annual server cert).
  - **Refresh path for Loom-managed agent-runtimes**: worker creates a per-trial host directory `/var/lib/loom/sandboxes/<trial_id>/` and bind-mounts it into the sandbox at `/run/loom/` (`--mount type=bind,source=/var/lib/loom/sandboxes/<trial_id>,target=/run/loom`). At trial start, worker writes the initial JWT to `/var/lib/loom/sandboxes/<trial_id>/step-jwt` on the host; the sandbox sees it immediately via the bind mount. To rotate, worker writes the new JWT to `step-jwt.tmp` + `rename(2)` to `step-jwt` (atomic; no partial-read window). The agent-runtime base image ships a watcher (fsnotify on the bind-mount path) that rereads on change and updates the SDK's effective API key. Worker re-rotates 5 min before the old JWT expires. **Empirical note (verified by [spike 04](../cluster-deploy-spikes/04-jwt-fsnotify-rotation.sh))**: revs 7–11 of this spec said "worker writes via `docker cp` to a tmpfs mount"; that mechanism doesn't work — `docker cp` into a tmpfs mount of a running container exits 0 but the file inside the container is unchanged because tmpfs is a kernel-managed in-memory filesystem owned by the running process's mount namespace, outside `docker cp`'s reach. Bind-mount + host-side write is the working pattern.
  - **For sandboxes NOT built from the agent-runtime base** (e.g., raw SWE-Bench eval images, OSWorld VMs, user-supplied): refresh doesn't work because the watcher isn't there. Step duration is capped at `JWT_LIFETIME - 5 min = 55 min` by the control-plane. Long-running benchmark steps (SWE-Bench: 6+ h) MUST either use the agent-runtime base or split into sub-steps. Documented in the benchmark-adapter guide.
  - **JWT expiry mid-call.** If a JWT expires while a streaming response is in flight, the gateway lets the current stream complete (the connection it serves was authenticated when established) but rejects any new request with 401. The agent-runtime SDK wrapper catches 401 + retries with the refreshed JWT once; on second 401, fails the call.
- **Limits:** doesn't catch agents that bypass `*_BASE_URL` and hard-code provider URLs (e.g., test fixtures). Those calls dial `api.openai.com` directly and hit the `--internal` bridge's "no route to host" — they fail closed. Logging on the bridge surfaces this as `connect: network is unreachable` in the trial trajectory so the agent author can fix the SDK config.
- **SDK config files.** Some SDK distributions read `~/.config/<provider>/config.toml` (or similar) that overrides env vars. Agent-runtime base images guarantee env-var priority by not shipping such config files; sandbox authors who add their own config files with provider URLs bypass the redirect. Documented in agent-runtime guide.
- **Wire protocol singleton ↔ gateway: HTTP/1.1 over TLS.** FastAPI defaults to HTTP/1.1 (uvicorn); SSE streaming + bidirectional bodies work on HTTP/1.1 with no special configuration. Singleton's Go binary uses `net/http` against HTTPS at `LOOM_GATEWAY_URL`. HTTP/2 to gateway is possible (hypercorn) but not required and not shipped Phase 2.
- **Linux-only invariants.** Stock Linux Python/Node/Go SDKs honor `SSL_CERT_FILE`; Docker `--add-host` writes `/etc/hosts` in Linux containers. Windows containers (rare in this deployment surface but supported by k8s) use the system cert store via Schannel + a different hosts-file path; for those, the worker would need different injection mechanisms (`certutil` for trust, `hosts` file edit via PowerShell). Out of scope; documented as a Linux-containers-only invariant.

Four layers of defense (any single one closes the path if the others fail):

1. **`--internal` sandbox bridge** — no host route, so direct dial to upstream IPs fails.
2. **Per-cluster TLS cert** — only the loom sandbox-endpoint validates as the SDK's target; rogue endpoints fail validation.
3. **Step-JWT authentication at the gateway** — anonymous calls rejected; per-trial scope.
4. **Egress proxy IP allowlist** — even with a compromised gateway, only IPs in `resolved_egress_ips[connection_id]` are dialed.

The egress proxy's role is **destination IP enforcement, scoped by connection_id**, and per-team rate-limiting. The gateway passes `(target_ip, X-Loom-Connection-Id)`; the proxy verifies `target_ip ∈ resolved_egress_ips` for that connection. No DNS at the proxy.

#### (c) Compose mode (`loom service`)

The cluster design above requires the worker to manage Docker networks. In compose mode the worker and gateway are already Docker containers on a compose-managed network; no per-trial bridge / singleton complexity is needed.

Compose-mode sandbox routing:
- `loom service up` creates a Docker network `loom-sandbox-net` out-of-band, **with a deterministic subnet** (`docker network create --subnet $LOOM_COMPOSE_SANDBOX_CIDR loom-sandbox-net`, default `10.43.0.0/16` — distinct from cluster mode's `10.42.0.0/16` so operators running both on the same host don't collide). Overridable via `loom service up --sandbox-cidr 10.X.0.0/16`. Compose stack references the network as `external: true`. Worker spawns sandbox containers on this network at trial start; compose lifecycle does not own them.
- `loom service up` is **idempotent**: re-running checks `docker network inspect loom-sandbox-net` and only creates if absent. If the existing network's subnet differs from the operator-supplied flag, fail-fast with a clear error rather than silently using the old subnet.
- **`loom service down` order of operations** (each step explicit so operators can stop/resume mid-teardown if needed):
  1. Worker enters drain mode: rejects new trial claims.
  2. Worker waits up to `--drain-timeout` for in-flight trials to finish; on timeout, force-kills. **Defaults differ by mode:** 30 s for `loom service` (dev usage, mostly fast trials); 3600 s for `loom cluster` (production benchmarks like SWE-Bench routinely run for hours; default-killing them at 30 s would destroy expensive work). Operators override via `--drain-timeout`.
  3. Worker enumerates sandbox containers it spawned (label `loom.trial-id=*`) and `docker stop` + `docker rm` each. `compose down --remove-orphans` does NOT catch these.
  4. `docker compose down --remove-orphans` tears down compose-managed services.
  5. **Network cleanup with partial-failure recovery**: `docker network rm loom-sandbox-net` first. If it errors with "network has active endpoints" (some sandbox container in step 3 didn't fully detach, OR an operator-spawned debug container is on the network), enumerate via `docker network inspect loom-sandbox-net` and run `docker network disconnect --force loom-sandbox-net <endpoint>` for each remaining endpoint, then retry the `rm`. Operator intervention required only if the second rm also fails.
  6. With `-v`: also `docker volume rm` for compose volumes (existing behavior).
- Sandbox env: `OPENAI_BASE_URL=https://loom-llm-gateway:9100/openai/v1` (gateway's compose DNS). TLS terminates at the gateway directly; the same loom-ca cert pattern applies. The CA cert lives at `$XDG_CONFIG_HOME/loom/loom-ca.crt` (default `~/.config/loom/loom-ca.crt`); compose service definitions bind-mount it into both the gateway container (server cert backing) and the sandbox containers (`SSL_CERT_FILE`).
- No singleton, no per-trial bridge, no `--internal` flag. The sandbox can reach the gateway via compose DNS and nothing else relevant via Docker's default bridge isolation.
- `team.allow_private_endpoints` defaults to `true` on `loom service up` (single-host trust model), so a local vLLM at `http://localhost:8000` works without admin opt-in.

**Compose-mode threat model (honest version)**: "one operator, one host, one trust boundary." Compose mode does NOT isolate trials of different teams from each other on the wire — all sandboxes share `loom-sandbox-net`, so a malicious sandbox could attempt lateral connections to another team's in-flight sandbox at the IP layer. The dominant compose-mode use case is one researcher running one team's batches at a time on a dev machine; for multi-team workloads, use `loom cluster` (where per-trial `--internal` bridges enforce cross-trial isolation).

#### Considered alternatives

**Path A (CONNECT proxy + TLS interception).** Sandbox uses `HTTPS_PROXY` to a per-trial forwarder that MITMs HTTPS via a per-trial CA and re-emits to the gateway with the JWT injected as a header. Workable but costs ~500–800 LOC + per-trial CA bootstrap + cert-trust injection into the sandbox image at trial-start time. Not shipped; reconsidered if a future need wants TLS-transparent proxying (e.g., third-party SDKs that don't honor `*_BASE_URL` and can't be patched).

**Per-trial sandbox-endpoint container.** Rev 5's first option was one sandbox-endpoint container per trial (O(concurrent_trials) extra containers). Rejected in favor of the per-node singleton in (a); the singleton's join-on-bridge mechanism is cleaner and cheaper.

**iptables-on-host enforcement.** Worker installs per-container iptables rules to block sandbox egress. Requires `CAP_NET_ADMIN` on the worker DaemonSet — a non-trivial privilege escalation (compromised worker can rewrite any host iptables rule). Rejected; Docker's `--internal` flag covers the same isolation at lower trust cost.

**Compose-mode `enable_icc=false` on the shared bridge** for per-container isolation. Rejected: Docker's `--opt com.docker.network.bridge.enable_icc=false` sets the bridge's FORWARD chain to default-DROP, which blocks ALL container-to-container traffic including sandbox → gateway. The flag was briefly considered for partial cross-team hardening in compose mode but it breaks the primary path. Compose-mode cross-trial isolation requires per-trial bridges (the cluster path's mechanism); accepted as a compose-mode limitation in the threat model.

### `resolved_egress_ips` re-resolution

Provider IP behavior is bimodal:
- **Anycast-fronted providers** (most CloudFlare/Fastly-routed APIs): the same IP (or small set) serves traffic stably on the day-scale; the Anycast pool does rotate, but typically over weeks. The 24-hour union window covers normal churn comfortably.
- **ELB-fronted providers** (some Anthropic regions, vLLM behind ALB, OpenRouter mirrors): IPs rotate with the LB pool, often hourly. Allowlist must track the union over a rolling window.

The re-resolver handles both via "union with bounded last-seen window":

- **Singleton runner**: re-resolver runs as a Postgres-advisory-lock-protected task on whichever gateway replica acquires `pg_try_advisory_lock(hashtext('egress_resolver_v1'))`. Non-leader replicas skip the tick. Lock released on shutdown; another replica takes over within one tick. The resolver's pg connection sets `tcp_keepalives_idle = 30, tcp_keepalives_interval = 5, tcp_keepalives_count = 3` so Postgres detects a SIGKILL'd holder within ~45 s (rather than the Linux 2-hour TCP default). Without short keepalives a dead leader's lock could block all resolution for hours.
- **Tick budget + jitter**: each tick resolves up to 32 connections (configurable: `LOOM_EGRESS_RESOLVE_BUDGET_PER_TICK`). On cold start, hash(connection_id) % tick_count distributes the initial fan-out across `egress_ips_min_ttl_seconds` worth of ticks instead of slamming DNS in a single second.
- **TTL**: per-connection re-resolve interval = `max(observed_dns_ttl, egress_ips_min_ttl_seconds)`. The `min_ttl_seconds` column is a FLOOR — operator can require a slow refresh on shaky DNS but not faster than the upstream TTL allows.
- **Union semantics**: new union = (current ∪ newly resolved); each IP carries a `last_seen` in a sidecar `egress_ip_last_seen JSONB` column. **Bounded retention**: drop IPs older than `egress_ip_window_hours` (default 24, configurable). **Hard cap**: 256 IPs per connection; if the rolling union exceeds 256, evict by `last_seen` ascending. Without the cap, a provider rotating IPs minutely produces an unbounded array.
- **Update atomicity**: single UPDATE per connection per tick, in its own transaction; `NOTIFY provider_egress_changed, '<id>'` on commit.
- **`upstream_host` re-derivation on PATCH**: when `base_url` changes via PATCH, the route re-derives `upstream_host`, clears `resolved_egress_ips`, and triggers an immediate synchronous resolve before returning. Stale `upstream_host` against fresh IPs would 100%-fail SNI matching at the egress proxy.

This keeps valid providers reachable across IP churn and still defeats DNS rebinding (rebinding an attacker-controlled host produces an IP NOT in the union; rejected at the egress proxy).

### SSRF defense: the four layers, summarized

Same four layers as the Sandbox→gateway § lists; this section gives the validation-time mechanism (one of those four) more detail.

1. `--internal` sandbox bridge (per Sandbox→gateway §).
2. Step-JWT authentication at the gateway (per Sandbox→gateway §).
3. **`POST /provider-connections` validation** (this layer): resolve `base_url`, classify each resolved address. By default, RFC1918 + IPv6 ULA (`fc00::/7`) + IPv6 link-local (`fe80::/10`) + IPv4 link-local + loopback + `0.0.0.0`, `[::]`, `0.0.0.0/8` are rejected as obvious SSRF targets. **`team.allow_private_endpoints` opt-in** (admin-only flag, `loom admin teams set --team T --allow-private-endpoints true`) permits RFC1918, ULA, **AND loopback**. Why loopback too: `loom service` single-box mode runs the cluster on one host, and the dominant use case is a local vLLM at `http://localhost:8000`. `loom service` defaults the team's flag to `true` automatically (single-host trust model); `loom cluster` defaults it to `false`. Link-local and `0.0.0.0` stay rejected unconditionally (those are never legitimate provider hosts even on a single host).
4. **Egress proxy IP allowlist** (per Sandbox→gateway §): destination IP must match the connection's `resolved_egress_ips`, scoped by `connection_id` passed in the `X-Loom-Connection-Id` header. **403 → user-visible error mapping:** when Envoy returns `HTTP/1.1 403 Forbidden` on an allowlist miss, the gateway translates to `503 Service Unavailable` with body `{"error":"provider_egress_rejected","detail":"target IP not in allowlist; re-resolve in progress"}` and `Retry-After: 30` header. 503 + Retry-After is the standard semantic for "transient; retry after a delay" — SDK retry loops back off appropriately instead of hammering the proxy (which a 502 would trigger).

Layers (1) and (4) are the load-bearing ones. (3) is a UX layer at create time. (2) is a trust-boundary check (only authenticated trial calls reach the egress proxy).

### Cache + config update durability (multi-replica)

Three caches matter: gateway's decrypted-key LRU (≥2 gateway replicas), egress-proxy's per-connection IP allowlist (≥2 egress replicas), and gateway's provider-connection metadata cache (same replica set). All must agree quickly when a user PATCHes a connection, rotates credentials, or the re-resolver writes new IPs.

- **Cache key includes `provider_connections.updated_at`.** Every gateway call does a cheap indexed `SELECT id, updated_at FROM provider_connections WHERE id = $1` (single PK lookup, ~100 µs p99). If `updated_at` matches the cache entry, hit; otherwise miss → fetch + decrypt → repopulate. Stale cache is impossible regardless of pubsub reliability; the only cost is the indexed lookup per call.
- **NOTIFY is the optimization layer.** Gateway and egress-proxy subscribe to `provider_connections_changed` and `provider_egress_changed`. On NOTIFY each replica invalidates proactively, eliminating the one-call window where another replica wrote and this one still cached.
- **NOTIFY-loss recovery.** Each replica polls `SELECT max(updated_at) FROM provider_connections` every 30 s. If max advanced past the local NOTIFY cursor, force-invalidate the whole cache.
- **Egress proxy: pushed allowlist via xDS.** The `loom-egress-xds` deployment reads Postgres (NOTIFY + 30 s poll) and serves Envoy a CDS/EDS config that includes a per-connection cluster keyed by `connection_id`, with `resolved_egress_ips` as the EDS endpoint list. At call time the gateway passes `X-Loom-Connection-Id` + `X-Loom-Team-Id` in the CONNECT request (Envoy listener config: `connection_options.allow_post: false` + custom-header matchers on the CONNECT request). Envoy routes to the matching cluster and rejects `target_ip` if it's not in that cluster's endpoint list. Per-team `local_ratelimit` filter is keyed on `X-Loom-Team-Id`.
- **`local_ratelimit` is per-Envoy-instance, not aggregate.** With ≥2 Envoy replicas, configured rate `N/sec/team` becomes up to `2N/sec/team` actual (each instance enforces independently). This is fine for **defense-in-depth abuse prevention** (catching a runaway agent at ~2× the soft cap). For **hard per-team quotas** (billing-anchored), enforcement lives in the gateway's pre-call quota check against `TeamQuota` (existing); the egress proxy is the second layer, not the canonical enforcer. Documented in the runbook.
- **Envoy listener config (HTTP/2 with CONNECT enabled):** the downstream listener sets `http2_protocol_options.allow_connect: true` so Envoy correctly handles HTTP/2 `:method = CONNECT` framing (rather than rejecting CONNECT as it does under default HTTP/2).
- **Cluster count scaling.** xDS-pushed config has one Envoy cluster per `provider_connection`. Envoy comfortably handles ~10k clusters; beyond that, sharding the egress proxy by `hash(connection_id) % n_proxy_shards` keeps each Envoy instance under the limit. For typical Loom deployments (≤ 1k teams × ≤ 5 connections each = 5k clusters), one Envoy pool is sufficient.
- The "no DNS at proxy" and "proxy enforces allowlist" facts are complementary, not contradictory — the gateway does the DNS work (which IPs are valid for a connection); the proxy enforces "is the IP the gateway picked actually in that set?"

NOTIFY-driven cache flush p99 < 500 ms across replicas; polling-driven p99 < 30 s. Both documented in the runbook.

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

**`provider_type = 'openai-compatible'` defaults to `tokens-only`**, never to `rate-card` — the URL-based "is this the canonical OpenAI endpoint?" heuristic is brittle (proxies, VPC endpoints, mirrors all break it). Operator opts in to `operator-supplied` via `--input-usd-per-1m`/`--output-usd-per-1m` for endpoints with known pricing.

`provider_type = 'custom'` is `tokens-only` only (no rate-card lookup attempted).

## `provider_models_cache` lifecycle

| Event | Action |
|---|---|
| `POST /provider-connections` succeeds | Background refresh: enumerate models, populate cache, `last_seen_at = now()`, `upstream_present = true`. |
| `GET /provider-connections/{id}/models` | If `now() - max(last_seen_at) > 1 h`, background refresh. Always return current cache (no client-side wait for refresh). |
| `loom providers models NAME --refresh` | Synchronous refresh. |
| Background refresh detects model gone | `upstream_present = false`, `hidden_reason = "missing-upstream"`. NOT deleted (audit trail). Re-appearing model flips back. |
| `loom providers models NAME --hide MODEL` | `visible = false`, `hidden_reason = "operator-hidden"`. |
| Refresh fails (network error) | Cache rows untouched. `provider_connections.last_validation_error` set (NOT a column on `provider_models_cache`). Surfaced in `loom providers show`. |

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

- **`worker.yaml`**: Deployment → DaemonSet, one pod per node, `LOOM_WORKER_CONCURRENCY` from ConfigMap. Volume mounts: `docker.sock` (hostPath), `bench-cache` (hostPath, read-through cache only), `trajectory-cache` (hostPath, write-through to MinIO), `/var/lib/loom/jwt/` (hostPath RW), `/var/lib/loom/sandbox-tls/` (hostPath RW). No `--privileged` and no `CAP_NET_ADMIN`. Worker runs as `loom` user; **`securityContext.supplementalGroups: [<host-docker-gid>]`** in the manifest (templated by `loom cluster up` from preflight-discovered gid). **Drift detection**: worker runs a 5-min loop that `stat -c %g /var/run/docker.sock`; on mismatch with the cached gid (typically caused by an operator reinstalling/upgrading Docker), it emits an error log + sets a `ready: false` health status. The DaemonSet doesn't auto-fix (requires `kubectl rollout` to re-template the manifest), but the loud failure mode beats silent permission-denied errors. Documented in runbook. Honest framing: docker.sock access is functionally root-equivalent on the host; same security tradeoff as today's single-box mode. **OpenShift caveat**: clusters that assign UIDs from namespace-allocated ranges (OpenShift `restricted` SCC, similar policies) reject the worker's fixed `loom` UID; operators must apply a custom SCC binding for the `loom-worker` ServiceAccount. Documented in runbook.
- **Control node taint**: `loom cluster up` taints the control node with `loom.io/role=control:NoSchedule`. Worker DaemonSet pods don't tolerate this taint by default. `--co-locate-workers-on-control` flag adds the toleration (degrades on small clusters; documented tradeoff). Postgres+MinIO StatefulSets get the toleration + a `nodeSelector: loom.io/role=control` so they pin to the tainted node.
- **`postgres.yaml`, `minio.yaml`**: keep `replicas: 1`. Add nodeSelector + toleration. Ship `backup-cronjob.yaml` that `pg_dump`s + `mc mirror`s to `s3://<external-bucket>/backups/...`. **Backup target is required external** — `loom cluster up --backup-target s3://...` enables the CronJob; without it the CronJob is templated but disabled. Writing backups to the embedded MinIO is refused with a clear error (same-disk backup doesn't survive control node failure). MinIO bucket lifecycle: `AbortIncompleteMultipartUpload after 7 days` (covers SWE-Bench-class long trials; rev 4's 1-day window would have cancelled active uploads mid-run). Applies to non-trajectory prefixes; the `trajectories/` prefix has `AbortIncompleteMultipartUpload after 14 days` to give long-running agent loops headroom.
- **`bootstrap-job.yaml`**: one-shot Job. `python -m loom_service.bootstrap` runs `alembic upgrade head`, checks if admin token exists, mints if not. **Output mechanism**: tokens written to stdout (visible via `kubectl logs job/loom-bootstrap`); `loom cluster up` watches the Job, captures stdout, prints to operator, then `kubectl delete job/loom-bootstrap` (Job logs purged automatically). No k8s Secret involved — no etcd plaintext leak. Re-running bootstrap on an already-bootstrapped cluster is a no-op (token rotation is a separate verb, see below).
- **etcd encryption pre-flight (attestation, not a check)**: there's no portable kubectl-level introspection for EncryptionConfiguration on most distros (kubeadm hides it on the apiserver process; k3s doesn't expose it). `loom cluster up` requires either `--etcd-encryption-attested` (operator confirms they've followed the runbook's verification steps) or `--allow-plaintext-etcd` (explicit dev/lab opt-out). Without one of those two flags, `loom cluster up` refuses with a runbook link explaining how to verify (steps: dump a known secret via etcdctl + check the value starts with `k8s:enc:aescbc:`). The verification is the operator's responsibility; default-fail is the safer posture, but we don't claim a check we can't deliver portably.
- **`loom-service`, `control-plane`, `llm-gateway`, `web`**: keep ≥2 replicas, add `topologySpreadConstraints` so they spread across nodes.
- **`loom-gateway-router.yaml`**: new DaemonSet (one pod per worker node) with `hostPort: 30443` (operator-configurable via `--gateway-host-port`). Pod is a thin TCP proxy (envoy-tcp-proxy or a 50-LOC Go binary) that forwards `:30443` → in-cluster `loom-llm-gateway.loom.svc.cluster.local:9100`. Why `hostPort` and not NodePort: hostPort binds on all host interfaces unconditionally; NodePort can be restricted via kube-proxy's `--nodeport-addresses` and silently drop traffic from Docker bridges. The per-node DaemonSet places one hostPort pod on every node so the singleton's local-IP dial always lands somewhere.
- **`egress-proxy.yaml` + `egress-xds.yaml`**: two Deployments, NOT one pod-with-sidecar. `egress-proxy` is Envoy, ≥2 replicas. `egress-xds` is the control plane, 1 replica (its state is Postgres so HA isn't critical, and Envoy survives xDS disconnects). **xDS server impl: vendored from [`envoyproxy/python-control-plane`](https://github.com/envoyproxy/python-control-plane) (in-tree Python reference), NOT built from scratch.** xds reads `provider_connections` from Postgres (NOTIFY on `provider_connections_changed` + `provider_egress_changed`; 30 s poll fallback) and serves CDS/EDS to Envoy over gRPC. Effort budget: ~3 days (config only; no protocol work). Egress proxy resolves no DNS itself — gateway passes target IP + `X-Loom-Connection-Id`; Envoy looks up the per-connection cluster in its EDS-pushed state and accepts/rejects accordingly. The split-into-two-Deployments choice keeps NetworkPolicy tight: xds reaches Postgres + DNS only (narrow blast radius); egress proxy reaches `0.0.0.0/0:443` + xds:18000 (gRPC) only. Sidecar-in-same-pod would have inherited the proxy's `0.0.0.0/0:443` allow on the xds container — overbroad.
- **Egress proxy replica count differs by deployment mode**:
  - `loom cluster` (k8s): Deployment, ≥2 replicas, `topologySpreadConstraints` across worker nodes.
  - `loom service` (compose): single container alongside the gateway.
  - Both modes expose port 8443 (HTTPS CONNECT). Gateway dials it via service DNS in cluster mode, `loom-egress-proxy:8443` in compose.
- **Compose service definition** (deliverable in Phase 2): `docker/compose.yaml` gains a `loom-egress-proxy` service. `LOOM_EGRESS_PROXY_DISABLE=1` removes it from the active profile and falls back to the gateway making direct outbound calls (single-box dev convenience; documented as a security regression in the comment block above the env var).
- **Network policies** (k8s mode), all default-DENY + explicit allow:
  - `loom-worker` pod → `loom-llm-gateway:9100`, `loom-control-plane:8080`, kube-dns UDP/53.
  - `loom-gateway-router` (DaemonSet) → `loom-llm-gateway:9100`, kube-dns UDP/53. Receives hostPort traffic from outside the k8s network plane (the Docker-bridged singleton); NetworkPolicy ingress allow-from-everywhere on port 30443.
  - `loom-llm-gateway` → `loom-egress-proxy:8443`, `postgres:5432`, kube-dns UDP/53 (gateway does the upstream IP resolution before the egress proxy CONNECT).
  - `loom-egress-proxy` egress: `loom-egress-xds:18000` (xDS gRPC), `0.0.0.0/0:443` (TCP only). No DNS needed.
  - `loom-egress-proxy` ingress: from `loom-llm-gateway` pods only (explicit ingress rule; without it, anything in the cluster network plane can submit CONNECT requests).
  - `loom-egress-xds` → `postgres:5432`, kube-dns UDP/53. Cannot reach the internet.
  - All other pods → kube-dns UDP/53 + their explicit deps.
  - **`loom-llm-gateway-sandbox` is NOT a k8s pod** (it's a worker-spawned Docker singleton on the `loom-uplink` bridge + per-trial bridges; not on host network — Docker rejects host+bridge mixing). k8s NetworkPolicy does not apply. Its egress confinement comes from the Go binary itself: it only dials `LOOM_GATEWAY_URL` (the gateway NodePort on `<host-bridge-gw>:30443`), nothing else; the `loom-uplink` bridge has Docker's default outbound rules. Defense in depth: operators may add a host-level egress firewall rule on the worker node restricting the `loom-uplink` bridge's outbound to the cluster Service / NodePort CIDR.
  - **Sandbox container egress is NOT enforced by k8s NetworkPolicy** (sandboxes are docker.sock-spawned bridge containers, outside the k8s network plane). Their egress is enforced by the per-trial Docker `--internal` bridge: the only reachable host is `loom-llm-gateway-sandbox` joined to the bridge at a known IP. The `--internal` flag is the load-bearing piece — sandboxes have no default gateway, so even ICMP to `8.8.8.8` fails closed.

### Token rotation (separate from bootstrap)

```
loom admin token rotate --kind {admin,worker,team:<id>}     # mints new, marks old revoked, prints new
loom admin secrets rotate --new-master-key-file PATH        # rewraps all encrypted_api_key_refs
```

Rotation is its own verb. Bootstrap is fire-once-idempotent.

## Reject-batch-when-no-worker

Confirmed against `src/loom_service/routes/backends.py:44` — capabilities shape is `list[dict]` with optional `backend` key; existing `list_backends` does the same extraction. Add the same check to `POST /batches` (`src/loom_service/routes/batches.py:268+`): query active workers' capabilities, fail-fast 400 if `payload.backend` not in the union. Admin override flag for "I'm provisioning a worker right now": `--force-no-worker-check`.

Race directions:
- **False-negative** (SELECT says no worker, worker registers a millisecond later): batch rejected; user retries; succeeds. Acceptable — same UX as a transient network blip.
- **False-positive** (SELECT says worker exists, worker dies between SELECT and INSERT): batch accepted; trial claims fail; surfaced via the existing batch-state machine. No regression vs today.

Both directions are acceptable; the check eliminates the much more common "user submits to wrong backend by accident, batch sits forever" failure.

## Token race fix (`loom service up`)

Confirmed `_up` at `src/loom_cli/service_cmd.py:172` with `_write_env_tokens` at line 215. After `_write_env_tokens` returns, run:

```
docker compose ... up -d --force-recreate --no-deps worker
```

so worker reads the fresh `LOOM_WORKER_TOKEN`. `docker restart` reuses old env; only `up --force-recreate` re-reads `.env`. The recreate is always-on — rev 3 documented a `--no-recreate-worker` opt-out but there's no legitimate reason to keep the worker holding a stale token, so the flag is dropped.

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

- `.github/workflows/images.yml`: add `linux/arm64` to buildx matrix; push multi-arch manifest lists to GHCR for all **8 images**: worker, service, control-plane, llm-gateway, web, **llm-gateway-sandbox** (singleton TLS terminator), **gateway-router** (hostPort DaemonSet TCP proxy), **preflight** (Job image for `loom cluster up`). All three new images are small Go binaries on Alpine. CI delta dominated by GHCR manifest push (~30–60 s per image × 3 new = ~2–3 min added to the existing CI run).
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

Sandbox network isolation + gateway-as-base-URL (rev 8 design):
- `cmd/loom-llm-gateway-sandbox/main.go` (~300 LOC Go binary): TLS terminator (presents loom-ca-signed cert for `loom-sandbox-gateway.local`) on `:8443`. Validates inbound step-JWT (Ed25519 public-key verify); reads `provider_connection_id` from the JWT; forwards body to in-cluster `loom-llm-gateway` via HTTP/1.1 over TLS to `LOOM_GATEWAY_URL` (default `https://<loom-uplink-gateway-ip>:30443`, configurable; reaches the per-node `loom-gateway-router` DaemonSet via `hostPort: 30443`). Watches `/var/lib/loom/jwt/` via fsnotify (IN_CLOSE_WRITE + IN_MOVED_TO + IN_MODIFY) for public-key rotation. Multi-arch build via Phase 1 pipeline.
- `cmd/loom-gateway-router/main.go` (~50 LOC Go binary): simple TCP proxy. Listens on `:30443` (hostPort, all interfaces); forwards to `loom-llm-gateway.loom.svc.cluster.local:9100`. Multi-arch.
- Singleton lifecycle: worker boot creates the persistent `loom-uplink` Docker bridge, discovers its gateway IP via `docker network inspect`, then `docker run -d --name loom-llm-gateway-sandbox --network loom-uplink --restart unless-stopped -e LOOM_GATEWAY_URL=https://${GW}:30443 -v /var/lib/loom/jwt:/var/lib/loom/jwt:ro -v /var/lib/loom/sandbox-tls:/etc/loom/tls:ro` (NOT `--network host`). Worker monitors via `docker events`; restarts on crash.
- Singleton TLS cert + key: `loom-sandbox-tls` k8s Secret; worker bind-mounts to host path `/var/lib/loom/sandbox-tls/` (mode 0400); singleton reads at startup. Rotation = rewrite Secret + `docker restart loom-llm-gateway-sandbox` on every node.
- Gateway facade routes in `loom_llm_gateway/routes/facade.py`: `/openai/v1/chat/completions`, `/openai/v1/embeddings`, `/anthropic/v1/messages`, `/google/v1/models/<model>:generateContent`. Each route reads the bearer/x-api-key header as a step-JWT, validates, looks up the connection, decrypts the real key, forwards. SSE responses pass through via FastAPI `StreamingResponse`.
- `loom-llm-gateway` Service exposes a NodePort (`30443`) so the per-node singleton can reach it from a Docker bridge without host networking.
- Step JWT signing: Ed25519. Control-plane holds the private key in `k8s-secret`; gateway + singletons hold only the public key. Public-key distribution: `loom-jwt-public-keys` ConfigMap (current + previous), watched by worker DaemonSet, atomically rewritten to `/var/lib/loom/jwt/` on the host, bind-mounted into the singleton. Dual-key rotation window: 2 h. Key rotation verb in Phase 5.
- Sandbox env injection in `src/loom_worker/sandbox.py`: at trial start, allocate `trial_index` (discover-from-Docker), create `--internal --subnet 10.42.<idx>.0/24` bridge, `docker network connect --ip 10.42.<idx>.2` singleton, mount loom-ca to `/etc/ssl/loom-ca/` (read-only), **bind-mount per-trial host dir `/var/lib/loom/sandboxes/<trial_id>/` to `/run/loom/` (RW)** for JWT refresh (host-side write + atomic rename; tmpfs+docker-cp doesn't work, see [spike 04](../cluster-deploy-spikes/04-jwt-fsnotify-rotation.sh)), set every `*_BASE_URL` / `*_API_KEY` / `SSL_CERT_FILE` / `REQUESTS_CA_BUNDLE` / `NODE_EXTRA_CA_CERTS` env, `--add-host loom-sandbox-gateway.local:10.42.<idx>.2`. Tear down at trial end (rm bridge, rm host dir).
- Worker reads `docker.sock` gid at startup; setgids before opening the socket.
- Trajectory writer scrubs `Authorization` / `x-api-key` / `Proxy-Authorization` headers and `*_API_KEY` / `*_TOKEN` / `*_SECRET` env values before MinIO write.
- Tests:
  - Synthetic agent that hard-codes `https://api.openai.com/v1/chat/completions` (ignores `OPENAI_BASE_URL`) — must fail closed with "connect: network is unreachable" via the `--internal` bridge.
  - Synthetic agent with invalid JWT — gateway facade returns 401.
  - Stock OpenAI SDK (latest stable) against the facade — full round-trip, usage recorded, SSE streaming works end-to-end with first-byte latency within 5 ms of a direct call.
  - JWT signing-key rotation under load — in-flight JWTs continue to validate against `previous` key during the cutover window.
  - JWT expiry mid-stream — current stream completes, next request returns 401, agent-runtime retries once with refreshed JWT.
- HTTP/3 + QUIC: current design assumes HTTP/1.1+TLS over TCP (matches all stable SDKs as of 2026-06). If an SDK ships HTTP/3-only support, the `*_BASE_URL` redirect still works (SDK dials our endpoint, not the provider's), but the singleton's TLS terminator would need to add HTTP/3 listener support — future work.

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
- `deploy/k8s/gateway-router.yaml` (DaemonSet, hostPort 30443; for Singleton's reach to gateway).
- `deploy/k8s/jwt-public-keys-configmap.yaml` + worker DaemonSet's ConfigMap volume mount + hostPath mount to `/var/lib/loom/jwt/` (read-write) for the worker→singleton copy.
- `deploy/k8s/sandbox-tls-secret.yaml` (templated; cert minted by `loom cluster up`) + worker hostPath mount to `/var/lib/loom/sandbox-tls/` (read-write; mode `0440 root:loom` written by worker, mounted RO into singleton via `docker run -v ...:ro`).
- `deploy/k8s/worker-rbac.yaml` (ServiceAccount + Role + RoleBinding for the worker DaemonSet's Secret reads).
- **Important runtime note for worker pod's `docker run`**: paths in `-v <host>:<container>` are resolved by `dockerd` against the HOST filesystem, NOT the worker pod's filesystem. Worker DaemonSet uses hostPath mounts (`/var/lib/loom/*`) so the host paths it writes are the same paths `docker run -v` references. Documented in worker.yaml comment block.
- `deploy/k8s/backup-cronjob.yaml` (disabled by default).
- `deploy/k8s/networkpolicies.yaml`.
- `src/loom_cli/cluster_cmd.py`: `loom cluster {up,down,status}`.
  - **Pre-preflight bootstrap** (idempotent, skippable): `kubectl create namespace loom || true`; `kubectl label namespace loom pod-security.kubernetes.io/enforce=privileged --overwrite`. **`--skip-namespace-bootstrap`** flag for GitOps shops where namespace creation + labels are owned by Argo / Flux / similar — preflight then verifies and fails-fast if the namespace doesn't exist or PSS isn't `privileged`.
  - **Preflight checks** (`--preflight-via=job|ssh`, default `job`) before any apply: PSS label verification, per-worker `docker version` (via `test -S /host/var/run/docker.sock` from the Job pod after hostPath-mounting `/var/run`), `loom-uplink` → `:30443` TCP-connect probe, `ss -tlnp | grep :30443` (port not occupied), `--sandbox-cidr` vs `ip route` overlap, etcd encryption attestation flag, **docker.sock gid discovery** (used to template worker.yaml's `supplementalGroups`). Job pod tolerations: `Exists` for `node.kubernetes.io/not-ready` and `node.kubernetes.io/unreachable` with `tolerationSeconds: 30` so preflight doesn't hang indefinitely on a partially-unavailable node. Job `activeDeadlineSeconds: 300` as a final guardrail. Results batched per-worker; full punch list reported in one error. Job completion signaled via `status.conditions: Complete`; detailed per-node results in a ConfigMap named `loom-preflight-results-<RFC3339-timestamp>` (timestamped so historic runs are preserved for debugging, not overwritten on retry).
  - **RBAC**: applies `worker-rbac.yaml` (ServiceAccount `loom-worker` + Role `loom-worker-secret-reader` with `get`/`list`/`watch` on Secrets `loom-sandbox-tls`, `loom-step-jwt-keys` in the `loom` namespace). Without this RBAC, worker DaemonSet can't read the singleton TLS cert or JWT keys.
  - Reads hostfile, labels nodes (`loom.io/role=control|worker`), taints control node.
  - Optionally installs ingress-nginx via `kubectl apply -f` from a pinned URL.
  - Generates self-signed CA + cert if no `--tls-cert`.
  - Mints + uploads JWT keypair (Ed25519) to ConfigMap + Secret; mints sandbox TLS cert.
  - Applies manifests in order: namespace → secrets → PVCs → Postgres → MinIO → bootstrap Job → core deployments → gateway-router DaemonSet → worker DaemonSet → egress proxy + xds → ingress + cert.
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
| Sandbox container bypasses gateway and dials providers directly | HIGH | Four layers: Docker `--internal` sandbox bridge (no host route), per-cluster TLS cert (only loom-sandbox-endpoint validates), step-JWT auth at gateway, egress proxy per-connection IP allowlist. Phase 2 ships explicit bypass tests (hardcoded URL + invalid JWT). |
| Agent hardcodes provider URL and ignores `*_BASE_URL` | MEDIUM | Fails closed at the `--internal` bridge ("connect: network is unreachable"). Surfaced in trial trajectory as a connection error so the agent author can fix their SDK config. |
| Sandbox base image lacks `SSL_CERT_FILE` honoring | MEDIUM | Agent-runtime base shipped Phase 2 sets this env unconditionally; Python httpx/requests/urllib + Node tls + Go crypto/tls all honor it. Statically linked binaries with pinned CA bundles don't — those agents can't make LLM calls through the facade. Documented in agent-runtime guide. |
| Long step (> 55 min) on non-runtime sandbox can't refresh JWT | MEDIUM | Step duration capped at JWT_LIFETIME - 5 min for sandboxes without the agent-runtime helper. Long benchmarks (SWE-Bench: 6h) MUST use the agent-runtime base or split into sub-steps. |
| JWT signing key rotation invalidates in-flight steps | LOW | Dual-key validation window (current + previous, 2h cutover) keeps in-flight JWTs valid through the rotation. |
| Singleton's outbound is not constrained by k8s NetworkPolicy | MEDIUM | Singleton's Go binary only dials the in-cluster gateway Service IP (hardcoded). Defense in depth: operators can add host-firewall rule restricting the singleton's outbound to cluster Service CIDR. Documented in runbook. |
| etcd attestation flag misused (operator claims attested without actually verifying) | MEDIUM | Honest design: we cannot reliably check; we require the operator to take responsibility via an explicit flag. Runbook spells out the etcdctl verification. Compensating control: any operator capable of running `kubectl apply` against a cluster also has the etcd access to verify, so this is socially enforced. |
| `docker.sock` access on worker is root-equivalent on host | MEDIUM | Same tradeoff as today's single-box mode; documented honestly. AppArmor/SELinux profiles + future user-namespaced Docker mitigate (not shipped Phase 3). Risk is the worker container, not the sandbox — the sandbox does NOT mount docker.sock. |
| etcd plaintext leaks master key + bootstrap creds | HIGH | `loom cluster up` refuses without `EncryptionConfiguration` unless `--allow-plaintext-etcd` set. Default-secure; opt-out for dev/lab. |
| Bench-cache backup writes go to embedded MinIO and don't survive control-node failure | MEDIUM | Backup CronJob refuses to target the embedded MinIO; `--backup-target s3://external` required to enable. Runbook documents the offsite-only invariant. |
| MinIO multipart uploads leak on trial crash | LOW | Bucket `LifecycleConfiguration: AbortIncompleteMultipartUpload after 1 day` ships in `minio.yaml`. |
| `resolved_egress_ips` goes stale, breaks legitimate provider traffic | HIGH | Background re-resolver (TTL = min(DNS TTL, 300 s)), union with last-24h IPs, NOTIFY + poll fallback to keep proxy live. |
| Cache invalidation across gateway/egress replicas drops a NOTIFY | HIGH | `updated_at` is the authoritative version stamp on every call; NOTIFY is an optimization, polling is the safety net. Stale-cache cannot happen except inside one indexed lookup. |
| Worker nodes lack Docker engine (containerd-only k8s) | HIGH | `loom cluster up` preflight runs `docker version` on every worker via SSH and fails-fast with a runbook link to the install steps. Documented as a hard prerequisite. |
| `hostPort: 30443` not reachable from Docker `loom-uplink` bridge (CNI portmap doesn't cover bridge IPs) | HIGH | `loom cluster up` preflight ships a TCP-connect probe from a container on `loom-uplink` to `<bridge-gw>:30443` and fail-fasts on miss. Runbook links to CNI portmap configuration. |
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

- **2026-06-15 rev 11**: Addresses 15 PR #50 review-of-rev-10 concerns. All material/smaller (no architectural blockers — design has been stable since rev 6):
  1. **docker.sock gid drift detection**: worker runs a 5-min stat loop; on mismatch (operator reinstalled Docker), emits error + sets `ready: false`. Loud failure mode vs silent permission-denied.
  2. **PSS=privileged namespace exclusivity warning** in Prerequisites table — operators must not deploy non-loom workloads into the `loom` namespace.
  3. **Network rm partial-failure recovery**: `loom service down` step 5 enumerates remaining endpoints via `docker network inspect` and `docker network disconnect --force` each before retrying `rm`.
  4. **`--drain-timeout` defaults differ by mode**: 30 s for `loom service` (dev), 3600 s for `loom cluster` (production benchmarks routinely run hours).
  5. **`--skip-namespace-bootstrap` flag** for GitOps shops that own namespace creation + labels via Argo/Flux. Preflight then verifies and fails-fast if PSS isn't `privileged`.
  6. **Preflight Job tolerations** for `not-ready` / `unreachable` taints (30 s) + `activeDeadlineSeconds: 300` so Jobs don't hang.
  7. **Phase 1 CI delta estimate corrected** to 2–3 min (was understated at 2 min; GHCR multi-arch manifest push dominates, not the Go compile).
  8. **ConfigMap naming**: `loom-preflight-results-<RFC3339-timestamp>` so retries don't overwrite history.
  9. **Control-plane RBAC** for `loom-step-jwt-keys-private` Secret added to deliverables (without this, control-plane can't mint step JWTs).
  10. **`loom-egress-proxy` NetworkPolicy ingress**: explicit allow from `loom-llm-gateway` pods only (was implicit before).
  11. (Covered in #3: same partial-failure recovery fixes both compose-managed and operator-spawned debug containers.)
  12. **`docker network inspect` 60 s loop logs at debug level** (info would flood ~5760 messages/node/day).
  13. **flock SIGKILL note**: Linux auto-releases flock on process death; no stale-lock recovery needed.
  14. **OpenShift UID caveat** documented: clusters with namespace-allocated UID ranges (OpenShift `restricted` SCC) reject the fixed `loom` UID; operators apply custom SCC binding.
  15. **JWT validity envelope after rotation**: `JWT_LIFETIME + dual_window − propagation_lag ≈ 2.5 h` worst case; operators plan rotations accordingly.
- **2026-06-15 rev 10**: Addresses 16 PR #50 review-of-rev-9 concerns. Blockers (1–4), material (5–10), smaller (11–16):
  1. **Preflight hostPath fix.** Rev 9's `hostPath: /var/run/docker.sock` would have left the pod stuck in `ContainerCreating` when Docker isn't installed (kubelet rejects missing socket mount). Rev 10 mounts `/var/run` (the directory; always exists) and the container `test -S /host/var/run/docker.sock` for the actual check.
  2. **Preflight node-targeting via `topologySpreadConstraints`** (maxSkew 1, hostname topology, `DoNotSchedule`) — one pod lands on each worker without requiring pre-existing role labels (those don't exist until apply). Plus hostfile-derived `kubernetes.io/hostname` selector restricts to declared workers only.
  3. **PSS honesty.** Rev 9 implied managed-k8s works via Job preflight; that's wrong. Rev 10's Prerequisites table makes PSS=`privileged` on the `loom` namespace a hard prerequisite + ships `loom cluster up` step that labels the namespace before any other apply. Managed-k8s with locked-down PSS (EKS Fargate, GKE Autopilot, Cloud Run) is explicitly called out as "cannot use `loom cluster`" — they use `loom service` instead.
  4. **Phase 1 image list expanded to 8** (added `loom-preflight`). Rev 9 had 7; the preflight image wasn't in the build list.
  5. **TLS cert rotation = brief in-flight interruption** honestly documented. Runbook ships a graceful pattern (drain 60 s, restart, re-enable).
  6. **Compose-mode `loom-sandbox-net` subnet** is now deterministic (`10.43.0.0/16` default, distinct from cluster mode's `10.42.0.0/16`); operator override via `loom service up --sandbox-cidr`.
  7. **`loom-uplink` IPAM check mechanism** specified: 60 s loop running `docker network inspect`; on gateway IP mismatch, recreate singleton with fresh env.
  8. **Worker pod uses `supplementalGroups`**, not runtime `setgid`. PSS Restricted blocks `setgid`-in-process; `loom cluster up` templates the docker.sock gid (discovered by preflight) into worker.yaml's manifest at apply time.
  9. **`compose down --remove-orphans` doesn't catch sandbox containers** (they're not in the compose project) — `loom service down` enumerates them via `loom.trial-id=*` label and `docker stop`/`docker rm` BEFORE compose down.
  10. **`loom service down` step-by-step order** spelled out (drain → kill in-flight → tear sandbox containers → compose down → network rm → -v volume rm). Six explicit steps; operators can stop / resume mid-teardown.
  11. **Job-based preflight uses `hostNetwork: true`** (not hostPath mount of `/proc/net/*`). Cleaner; gives direct access to the host's port table and network namespace.
  12. **Sandbox-CIDR collision avoided** by distinct defaults (cluster `10.42.0.0/16`, compose `10.43.0.0/16`).
  13. **Preflight completion signal**: Job `status.conditions: Complete`, NOT ConfigMap. ConfigMap is for detailed per-node results that the CLI reads only after the canonical Job-complete signal fires.
  14. **`supplementalGroups` solves the PSS-vs-setgid conflict** (covered in #8).
  15. **Linux-only container invariants** (SSL_CERT_FILE + `--add-host`) consolidated into one note; Windows out of scope.
  16. **`loom service up` idempotent**: re-run checks `docker network inspect loom-sandbox-net` and only creates if absent; fail-fast if the existing subnet differs from operator-supplied flag.
- **2026-06-15 rev 9**: Addresses 17 PR #50 review-of-rev-8 concerns. Blockers (1–4), material (5–10), smaller (11–17):
  1. **`enable_icc=false` dropped** from compose-mode `loom-sandbox-net` creation. The flag sets the bridge's FORWARD chain to default-DROP and blocks ALL container-to-container traffic, including sandbox → gateway — rev 8 inadvertently broke compose mode. Moved to "Considered alternatives" with the explicit "rejected: breaks the primary path" reason. Compose-mode cross-trial isolation stays as a documented limitation (per-trial bridges = cluster path's mechanism only).
  2. **`hostPort` claim weakened**. Rev 8 said "binds on all host interfaces unconditionally"; that's overconfident. Reachability from Docker bridges depends on CNI portmap behavior + iptables chain order. Spec now ships a preflight TCP-connect probe from `loom-uplink` to verify the path, with a runbook link on miss.
  3. **Singleton TLS cert mode fixed.** Was 0400 root-only (incompatible with non-root singleton). Now `mode 0440 root:loom` (gid 64055); singleton runs as user `loom-sandbox` (uid 64055) in group `loom`, can read but not write. Singleton is non-root, no shell, no docker.sock.
  4. **Phase 1 image list expanded to 7**: added `llm-gateway-sandbox` (singleton) and `gateway-router` (hostPort DaemonSet TCP proxy). Were missing from rev 7/8 task list; Phase 2 would have shipped with no published images for these binaries.
  5. **Job-based preflight as default.** Rev 8's SSH-only preflight broke for managed-k8s (EKS Fargate, GKE Autopilot, restricted environments). Rev 9 ships `--preflight-via=job|ssh`, default `job`: one-shot k8s Job hostPath-mounts `/var/run/docker.sock` (stat) + `/proc/net/tcp` + `/proc/net/route`, reports results via ConfigMap. SSH path kept for small / dev setups.
  6. **Worker RBAC explicit**: ServiceAccount `loom-worker` + Role `loom-worker-secret-reader` (`get`/`list`/`watch` on `loom-sandbox-tls` + `loom-step-jwt-keys` Secrets). Without this, worker can't read the singleton TLS cert or JWT keys.
  7. **`hostPort: 30443` collision check** added to preflight: per-worker `ss -tlnp | grep :30443` reports pending DaemonSet conflicts.
  8. **Worker hostPath mounts spelled out.** `worker.yaml` includes `hostPath` volumes for `/var/lib/loom/jwt/` (RW) + `/var/lib/loom/sandbox-tls/` (RW). Important comment block explains that `docker run -v` paths inside the worker pod resolve against the HOST filesystem (a common foot-gun for new operators).
  9. **`docker compose down --remove-orphans`** in `loom service down` ensures sandbox containers don't block network removal.
  10. **`loom service down` network cleanup is unconditional**; volume cleanup is `-v` (existing). Both modes documented to avoid operator confusion.
  11. **`loom-uplink` IPAM gateway IP change handling**: worker periodically `docker network inspect` + restart singleton on IP change.
  12. (Covered in #4: multi-arch `gateway-router` image is part of Phase 1 expansion.)
  13. **`enable_icc=false` moved to Considered alternatives** with the explicit "rejected: breaks sandbox → gateway path" reason (also covered in #1).
  14. **Singleton runs as non-root** (`loom-sandbox` user, uid 64055). Cert mode aligned. Best-practice non-root execution preserved.
  15. **Preflight batches failures**: full punch list across all workers reported in one error, not fail-on-first.
  16. **`docker network ls` discovery** for subnet allocation (rev 7's restart-safety mechanism) confirmed to apply post-rev-8 changes; cross-referenced in Prerequisites.
  17. **`loom-uplink` bridge idempotent creation** (`docker network create ... || true`) so worker boot works on first install AND subsequent restarts.
- **2026-06-15 rev 8**: Addresses 18 PR #50 review-of-rev-7 concerns. Blockers (1–2), material (3–10), smaller (11–18):
  1. **Docker prerequisite made explicit.** New "Prerequisites" section: `loom cluster` requires Docker engine installed on every worker node alongside k8s's CRI (containerd). On modern kubeadm 1.24+ / k3s / EKS / GKE Autopilot, Docker is NOT installed by default; operators must add it. Preflight checks (`docker version` on every host) fail-fast with a clear error.
  2. **NodePort → hostPort.** Rev 7 used NodePort 30443; would have silently broken on clusters with kube-proxy `--nodeport-addresses` restrictions (common in security-baselined setups). Rev 8 adds a `loom-gateway-router` DaemonSet with `hostPort: 30443` that binds on all interfaces unconditionally, including Docker bridges. Configurable via `--gateway-host-port`.
  3. **Singleton TLS cert + private key distribution.** `loom-sandbox-tls` k8s Secret; worker bind-mounts to host path `/var/lib/loom/sandbox-tls/` (mode 0400); singleton reads at startup. Rotation = rewrite + `docker restart`.
  4. **JWT public-key ConfigMap watch flow** detailed: k8s mounts ConfigMap into worker pod; worker fsnotify-watches + copies to host path; singleton bind-mounts + fsnotify-watches the host path. No k8s API watch from the worker — RBAC stays minimal. Two-hop propagation ~60–90 s.
  5. **`loom-uplink` gateway IP discovery** via `docker network inspect` at worker boot; passed to singleton as `LOOM_GATEWAY_URL` env.
  6. **`--sandbox-cidr` flag** for CIDR override on operators with subnet conflicts.
  7. **`loom service down` removes `loom-sandbox-net`** to prevent stale-network accumulation.
  8. **Singleton ↔ gateway protocol: HTTP/1.1 over TLS** (matches FastAPI/uvicorn defaults).
  9. **Trajectory scrub `LOOM_TOKEN`** intentional + documented (broad pattern correctly catches internal tokens too).
  10. **`--gateway-host-port` configurable** for clusters with policy restrictions on the default port range.
  11. **fsnotify event triplet** (IN_CLOSE_WRITE + IN_MOVED_TO + IN_MODIFY) documented for cross-Docker-version `docker cp` portability.
  12. **FIPS deployments note**: Ed25519 default; ECDSA P-256 one-line swap for FIPS 186-4 environments.
  13. **Compose-mode `enable_icc=false`** on `loom-sandbox-net` for partial cross-team hardening (containers can reach gateway but not each other).
  14. **`loom-uplink` workload exclusivity** documented in runbook (operator must not put other workloads on the bridge).
  15. **Trajectory scrub limitation** honestly documented: agent embedding the JWT in a tool_call body string bypasses pattern matching; acceptable for trusted-users threat model.
  16. **`SSL_CERT_FILE` Linux-only invariant** documented; Windows containers (rare in scope) need separate cert installation.
  17. **`docker.sock` gid resolution at startup** (rev 4 behavior) confirmed in Phase 2 deliverables.
  18. **NodePort range concern resolved** by switching to hostPort (no longer subject to kube-proxy port-range policy).
- **2026-06-15 rev 7**: Addresses 15 PR #50 review-of-rev-6 concerns. Blocker (1), material (2–9), smaller (10–15):
  1. **Singleton networking redesigned: two bridges, NOT `--network host`.** Rev 6 required the singleton on `--network host` AND attached to per-trial bridges; Docker rejects this combination (host-namespaced containers cannot be attached to additional networks). Rev 7 ships the singleton on two bridges: persistent `loom-uplink` (reaches the cluster gateway via NodePort on the host's bridge-gateway IP) and per-trial `--internal sandbox-<trial>` (serves sandboxes). No host network mode. Gateway is exposed as `type: NodePort` (30443).
  2. **JWT verification key distribution to singletons.** Ed25519 asymmetric signing. Private key in `k8s-secret` (control-plane only); public keys distributed via `loom-jwt-public-keys` ConfigMap watched by the worker, bind-mounted into the singleton, watched by the singleton's Go binary via fsnotify. Compromised singleton cannot mint JWTs (verify only).
  3. **Subnet allocator restart-safe.** Worker discovers in-use subnets on boot via `docker network ls --filter name=sandbox-` instead of an on-disk index that can desync.
  4. **JWT refresh via `docker cp` to tmpfs**, not `docker exec sh -c`. Works with distroless / FROM-scratch sandbox images. Worker mounts `tmpfs` at `/run/loom/` at sandbox create.
  5. **Compose mode bridge as `external: true`.** `loom service up` creates `loom-sandbox-net` out-of-band; compose stack references it as external so ad-hoc worker-spawned sandboxes can join without compose lifecycle interference.
  6. **Egress 403 → gateway 503 + `Retry-After: 30`** (not 502). 503 is the standard semantic for "transient; retry after delay"; SDK retry loops back off appropriately instead of hammering.
  7. **`local_ratelimit` is per-Envoy honestly documented.** Soft cap for defense-in-depth; hard per-team quotas live in the gateway's pre-call `TeamQuota` check.
  8. **Compose-mode multi-team honesty.** All sandboxes share `loom-sandbox-net`; no per-trial isolation. Documented as a constraint; multi-team workloads use `loom cluster`.
  9. **Singleton `LOOM_GATEWAY_URL` env var** (default the NodePort), not hardcoded — supports dev / multi-cluster setups without image rebuilds.
  10. **Host port 8843 dropped.** Singleton listens only on its bridge interfaces; no spurious host-port exposure.
  11. **Trajectory writer scrubs** `Authorization` / `x-api-key` / `Proxy-Authorization` headers AND env values matching `*_API_KEY` / `*_TOKEN` / `*_SECRET` before MinIO write.
  12. **Compose-mode `loom-ca`** lives at `$XDG_CONFIG_HOME/loom/loom-ca.crt`, bind-mounted by compose into gateway + sandbox containers.
  13. **JWT key rotation orchestration to singletons** (covered in #2): ConfigMap watch + worker DaemonSet rewrite + singleton fsnotify reload. No singleton restart needed for rotation.
  14. **Envoy CONNECT custom headers** noted: `connection_options.allow_post: false` + custom-header matchers for `X-Loom-Connection-Id` / `X-Loom-Team-Id`.
  15. **Subnet allocator recovery** explicit via `docker network ls` discovery (covered in #3).
- **2026-06-15 rev 6**: Addresses 22 PR #50 review-of-rev-5 concerns. Blockers (1–5), material (6–13), smaller (14–22):
  1. **Sandbox-endpoint is now a worker-spawned Docker singleton, NOT a k8s pod.** Rev 5 called it a DaemonSet pod that "joins each trial bridge on demand," but `docker network connect` to a k8s pod doesn't work on containerd-based clusters (the kubelet owns the pod's net namespace; there's no `dockerd` managing it). Rev 6 ships a Docker container per node, lifecycle-managed by the worker. `--network host` so kube-proxy routes the singleton's outbound to the in-cluster gateway Service IP.
  2. **Deterministic per-trial subnets (`10.42.<idx>.0/24`)** via `docker network create --subnet`. Singleton joins at a pinned IP via `docker network connect --ip 10.42.<idx>.2`. No more "fixed 172.30.0.2" claim that rev 5 made (Docker IPAM defaults don't assign that).
  3. **Hostname-based URL + per-trial `--add-host`** instead of IP-literal `*_BASE_URL`. Cert SAN has one entry (`loom-sandbox-gateway.local`); `/etc/hosts` injection resolves it per-trial. Solves the unbounded-SAN problem from rev 5.
  4. **SDK env var names corrected** based on per-SDK docs. Spec now injects every plausible variant (OPENAI_BASE_URL, ANTHROPIC_API_URL + ANTHROPIC_BASE_URL, GOOGLE_GENAI_API_BASE + GOOGLE_GEMINI_BASE_URL) so SDK version drift doesn't break the redirect; Phase 2 delivers an audit and trims to canonical names.
  5. **`team.allow_private_endpoints` now relaxes loopback too.** Rev 5 kept loopback always-rejected, which broke the `loom service` single-box mode's primary use case (local vLLM at `http://localhost:8000`). Single-box auto-defaults the flag on; cluster defaults off.
  6. **JWT refresh limitations honestly documented.** Refresh requires the agent-runtime helper in the sandbox. Non-runtime sandboxes (SWE-Bench eval images, OSWorld VMs, user-supplied) get a 55 min step ceiling. Long benchmarks MUST use the agent-runtime base or split into sub-steps.
  7. **`SSL_CERT_FILE` + `REQUESTS_CA_BUNDLE` + `NODE_EXTRA_CA_CERTS`** env vars + worker bind-mount loom-ca into every sandbox at `/etc/ssl/loom-ca/`. Lets arbitrary base images validate loom-ca-signed certs.
  8. **Envoy cluster count scaling note** added (≤ 10k clusters per pool; shard beyond).
  9. **`X-Loom-Team-Id` header added** to the gateway→proxy CONNECT for per-team Envoy local_ratelimit.
  10. **Compose-mode sandbox routing spec'd** (subsection (c)): one custom bridge connects sandbox to the gateway compose service; no singleton complexity for single-box.
  11. **Gateway facade SSE streaming** documented: FastAPI `StreamingResponse`, first-byte latency within 5 ms of direct.
  12. **JWT signing-key rotation dual-window**: `current` + `previous` keys both valid for 2h cutover; rotation no longer invalidates in-flight steps.
  13. **JWT expiry mid-call policy**: current stream completes, next request returns 401, agent-runtime retries once with refreshed JWT.
  14. Path A moved to **"Considered alternatives"** subsection alongside per-trial-container + iptables variants. Stops cluttering the primary flow.
  15. (Covered in #2) deterministic subnet assignment by worker avoids Docker default-cascade collisions on hosts running other Docker workloads.
  16. **SDK config-file precedence** documented: agent-runtime base ships no config file; env wins; sandbox authors who add their own config bypass redirect.
  17. **Envoy listener: `http2_protocol_options.allow_connect: true`** documented so CONNECT works under HTTP/2 framing.
  18. **`loom service` default for `allow_private_endpoints`**: on. Covered in #5.
  19. **Singleton on `--network host`** so it can reach in-cluster Service IPs via kube-proxy IPVS. Covered in #1.
  20. (Path A placement covered in #14.)
  21. **JWT visibility to agent** acknowledged honestly: appropriate for trusted-users threat model; hostile-tenant adaptation (per-call short-lived JWT minted via sandbox-side helper) listed as future work.
  22. **Egress 403 → user-visible error**: gateway translates `HTTP/1.1 403 Forbidden` from Envoy into a 502 response body `{"error":"provider_egress_rejected", ...}` so agents see meaningful diagnostics.
- **2026-06-15 rev 5**: Addresses 22 PR #50 review-of-rev-4 concerns. Blockers (1–4), material (5–12), smaller (13–22):
  1. **Sandbox→gateway mechanism redesigned (Path B).** Rev 4's CONNECT-proxy + `Authorization: Bearer` header injection was mechanically impossible (CONNECT can't inject headers into TLS-encrypted upstream traffic) AND the `--internal` bridge + `--network container:` namespace mix didn't compose (a container has one net namespace). Replaced with **gateway-as-base-URL**: `OPENAI_BASE_URL=https://172.30.0.2:8443/openai/v1` + step-JWT as `OPENAI_API_KEY`. Stock SDKs work unmodified; new `loom-llm-gateway-sandbox` DaemonSet pod terminates TLS on per-trial Docker bridges with the cluster's loom-ca cert.
  2. Per-trial `--internal` bridge with `loom-llm-gateway-sandbox` joined on demand; no `--network container:` namespace mix; no localhost forwarder; no per-trial sidecar (container count stays O(nodes)).
  3. **RFC1918 endpoints opt-in.** `team.allow_private_endpoints` admin flag lets operators register vLLM/Llama-host providers on internal subnets. Without it, RFC1918 / ULA are rejected at create (matches rev 4); loopback / link-local stay rejected unconditionally.
  4. **Cache vs proxy reconciled.** Egress proxy still enforces per-connection IP allowlist (the second wall vs a compromised gateway). Gateway passes `X-Loom-Connection-Id` + target IP in the CONNECT; Envoy looks up the connection's EDS-pushed allowlist and rejects mismatches. "No DNS at proxy" is about DNS resolution work; the allowlist still matters.
  5. **Step JWT lifetime + refresh** spelled out (3600 s, re-mint via control-plane endpoint, tmpfs-injected refresh).
  6. **MinIO `AbortIncompleteMultipartUpload`** moved to 7 days (non-trajectory prefixes) / 14 days (`trajectories/`). Rev 4's 1-day window would have killed long agent loops.
  7. **etcd encryption is attestation, not check.** `--etcd-encryption-attested` OR `--allow-plaintext-etcd` required; no false claim of a programmatic check (which isn't portable across distros).
  8. **Advisory lock keepalives.** Resolver pg connection sets `tcp_keepalives_idle=30` so a SIGKILL'd leader's lock releases in ~45 s, not the Linux 2-hour TCP default.
  9. **Bench-cache eviction TOCTOU** fixed via shared/exclusive `flock(2)` discipline on `.lock/<slug>__<version>`.
  10. Four-layer SSRF count unified across the Sandbox→gateway § and the SSRF defense §.
  11. **`loom-egress-xds` is its own Deployment**, not a sidecar in the egress-proxy pod. NetworkPolicy stays tight (xds reaches only Postgres + DNS).
  12. `last_validation_error` scope clarified as `provider_connections.last_validation_error`, NOT a cache-row column.
  13. `HTTPS_PROXY=http://` confusion moot — Path B removes the HTTPS_PROXY mechanism entirely.
  14. Rev 2/3 commentary trimmed from spec body (kept in Changelog).
  15. Forwarder-per-trial container doubling no longer applies (forwarder removed); sandbox-endpoint is a per-node DaemonSet that joins per-trial bridges, O(nodes) not O(trials).
  16. `--no-recreate-worker` flag dropped (no legitimate use case for keeping a stale worker token).
  17. Anycast wording corrected ("stable on the day-scale" not "forever").
  18. Topology diagram adds egress-proxy / egress-xds / sandbox-endpoint; opt-in worker-on-control annotation kept; SPA `loom-web` documented as `replicas: 0` for the cluster rollout.
  19. SPA story: web Deployment ships with `replicas: 0`; operator scales up when SPA work resumes.
  20. `--combinations` UUID source noted (`loom providers show NAME --format=id`).
  21. First-time human login pattern shown (`export LOOM_TOKEN; --token env:LOOM_TOKEN`).
  22. HTTP/3 / QUIC acknowledged: current design assumes HTTP/1.1+TLS over TCP; `*_BASE_URL` redirect survives HTTP/3 transport changes because the SDK dials our endpoint, not the provider's.
- **2026-06-15 rev 4**: Addresses 27 PR #50 review-of-rev-3 concerns. Blockers (1–2), severe (3–7), material (8–16), smaller (17–27):
  1. **Sandbox→gateway auth redesigned.** The `HTTPS_PROXY=http://step-jwt@gateway` mechanism in rev 3 doesn't work with stock SDKs (httpx parses userinfo as basic-auth; gateway isn't a forward proxy). Replaced with per-trial localhost forwarder: a ~150 LOC Go binary (`loom-trial-fwd`) in the agent-runtime image, listens on `127.0.0.1:8443`, injects step-JWT in `Authorization: Bearer` header. Stock SDKs see a vanilla `HTTPS_PROXY`. New section + diagram.
  2. **Docker `--internal` bridge** instead of host iptables manipulation. Removes the `CAP_NET_ADMIN` privilege escalation on the worker DaemonSet. Per-trial bridges teardown at trial end. iptables-based design retained as a documented alternative with the privilege tradeoff spelled out.
  3. **Rewrap cache invalidation** wired through: the rewrap walker bumps `provider_connections.updated_at` (and NOTIFYs) for every connection whose secret was rewrapped. Without this, post-rotation cache stayed stale and would fail mid-cutover-window.
  4. **Soft-delete `provider_connections`.** `deleted_at` column replaces `ON DELETE SET NULL` on Trial FK. Trials retain their connection_id for audit/billing; active-listing routes filter `WHERE deleted_at IS NULL`. Hard-delete is a separate admin verb.
  5. **Re-resolver singleton via `pg_try_advisory_lock`.** Eliminates the multi-replica write race rev 3 introduced. Per-tick budget + jittered cold start avoid DNS rate-limit hits.
  6. **Egress proxy resolves no DNS.** Gateway resolves at call time and passes the IP in the CONNECT target. Removes the broken `0.0.0.0/0:443 only` NetworkPolicy (which would have killed all calls — no DNS allow rule). New NetworkPolicy matrix.
  7. **MinIO bucket `LifecycleConfiguration: AbortIncompleteMultipartUpload after 1 day`** in `minio.yaml`. Crashed trajectory uploads no longer accumulate.
  8. Anycast vs ELB framing explicit in re-resolver section.
  9. **Cap of 256 entries per `resolved_egress_ips`**, FIFO eviction by `last_seen`. Bounded growth.
  10. **`egress_ips_ttl_seconds` → `egress_ips_min_ttl_seconds`**. Operator FLOOR, not the TTL itself.
  11. **`SecretStore` Protocol → async.** Avoids blocking the FastAPI event loop.
  12. **`upstream_host` re-derived on PATCH `base_url`**, plus immediate sync re-resolve of `resolved_egress_ips`.
  13. **Cold-start DNS pressure**: tick budget + hash-based stagger across `egress_ips_min_ttl_seconds`.
  14. **Backup target must be external** (`--backup-target s3://...`). CronJob refuses to write to embedded MinIO. Backup-as-DR is now real.
  15. **etcd encryption pre-flight**: `loom cluster up` refuses without `EncryptionConfiguration`; `--allow-plaintext-etcd` opt-out for dev/lab.
  16. **xDS server vendored** from `envoyproxy/python-control-plane` (config-only, ~3 days), not built from scratch.
  17. **`docker.sock` gid** read at worker startup; no manifest changes for hosts with non-default gid.
  18. NOTIFY channel matrix documented: `provider_connections_changed` for PATCH/soft-delete; `provider_egress_changed` for re-resolver writes. Both subscribed by gateway + egress-xds sidecar.
  19. `flock(2)` documented as local-FS-only.
  20. **In-use registry uses PID files** (`<slug>__<version>__<pid>`), reaped via `kill(pid, 0)`. Covers all worker shapes regardless of CWD.
  21. **`loom auth login --token` required**; no interactive-paste fallback (CI safety + clear error UX).
  22. **`loom providers update --api-key`** flag added (rotation flow).
  23. **`pricing_data` route-level validation** for `operator-supplied` (non-null + numeric).
  24. **`--input-usd-per-1m` / `--output-usd-per-1m` interdependence** enforced by argparse custom action.
  25. **Reject-batch race directions** spelled out: false-negative (retry succeeds), false-positive (existing behavior, surfaced via batch state).
  26. **`loom providers models --hide MODEL`** maps to `POST /provider-connections/{id}/models/{model_id}:hide`. CLI-to-route mapping table added.
  27. **Topology diagram** annotates `loom-worker on control` as DISABLED-by-default + flag name.
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
- [cluster-deploy-spikes/](cluster-deploy-spikes/README.md) — **executable proofs** that this spec's load-bearing mechanisms (Docker bridges, hostPath/hostNetwork, hostPort routing) actually compose with the underlying primitives. CI gate; spec changes that propose new mechanisms must add a spike or accept review-only verification.
- [service-mode.md](service-mode.md) — current single-host architecture
- [drf-scheduling.md](drf-scheduling.md) — how the claim path matches workers to trials
- [overview.md](overview.md)
