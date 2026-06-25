"""One-shot manifest editor for #419 Harbor reference correction.

The original Layer 2 batches (PRs #499, #513, #514, #517) wrote the
manifest against `coder-harbor-cloud` (a Huawei agent platform mistakenly
identified as "Harbor"). The real Harbor is `harbor-framework/harbor`,
pinned in `harbor_reference` at commit `2ead3f1f` (Task A2).

This script rewrites `harbor_support` for the 5 v1.0 benchmarks where
the real Harbor ships an adapter (Task A3). A follow-up batch (Task A4)
will extend this script with an UNSUPPORTED dict for the remaining 7
benchmarks. The script is one-shot and will be DELETED in Task A8.

Mutation summary per entry:
- `harbor_support` is fully replaced with a `supported`-status block
  pointing at the real Harbor adapter directory at the pinned commit
  plus a summary of the published `parity_experiment.json` baseline
  (or a "no baseline published" note for AIME).
- `layer2_evidence.status` is demoted from `replay_validated` to
  `pending_paired_run` because parity claims against the wrong Harbor
  do not validate score parity against the real Harbor.
- `layer2_evidence.parity_kind` is set to
  `matched_config_paired_run_pending` and a `pending_reason` is added.
- All other fields on the entry (`canonical_reference`,
  `score_semantics`, `layer1_evidence`, `layer2_evidence.replay_tests`,
  etc.) are PRESERVED.
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "docs" / "benchmark-score-alignment.json"

HARBOR_COMMIT = "2ead3f1f2462f6f7260aca5ef2377cd7e309ff06"
HARBOR_TREE = f"https://github.com/harbor-framework/harbor/tree/{HARBOR_COMMIT}"


def _adapter_url(adapter_dir: str) -> str:
    return f"{HARBOR_TREE}/adapters/{adapter_dir}"


SUPPORTED: dict[str, dict[str, object]] = {
    "aime-24": {
        "status": "supported",
        "harbor_adapter_dir": _adapter_url("aime"),
        "parity_target": (
            "harbor-framework/harbor's `adapters/aime` adapter at pinned "
            "commit 2ead3f1f covers AIME 2024 + 2025. No published "
            "parity_experiment.json baseline exists at 2ead3f1f (the file "
            "returns 404), so Stage B must establish the Harbor baseline "
            "itself rather than compare against a published number."
        ),
        "decision": (
            "Real Harbor (harbor-framework/harbor) supports AIME 2024 via "
            "`adapters/aime`. Loom's `AIME24Adapter` and Harbor's adapter "
            "implement the same exact-integer-match parity target but "
            "via independent code paths, so end-to-end matched-config "
            "paired runs are required to validate score parity. Stage B "
            "paired-run task tracked in a child issue (link added in Task A7)."
        ),
    },
    "aime-25": {
        "status": "supported",
        "harbor_adapter_dir": _adapter_url("aime"),
        "parity_target": (
            "harbor-framework/harbor's `adapters/aime` adapter at pinned "
            "commit 2ead3f1f covers AIME 2024 + 2025 with a shared "
            "exact-integer-match verifier. No published "
            "parity_experiment.json baseline exists at 2ead3f1f (the file "
            "returns 404), so Stage B must establish the Harbor baseline "
            "itself rather than compare against a published number."
        ),
        "decision": (
            "Real Harbor (harbor-framework/harbor) supports AIME 2025 via "
            "the same `adapters/aime` adapter used for AIME 2024. Loom's "
            "`AIME25Adapter` shares its script-verifier infrastructure "
            "with Loom's AIME 2024 adapter, mirroring Harbor's "
            "single-adapter approach, but the implementations are still "
            "independent so end-to-end matched-config paired runs are "
            "required to validate score parity. Stage B paired-run task "
            "tracked in a child issue (link added in Task A7)."
        ),
    },
    "gpqa": {
        "status": "supported",
        "harbor_adapter_dir": _adapter_url("gpqa-diamond"),
        "parity_target": (
            "harbor-framework/harbor's `adapters/gpqa-diamond` adapter at "
            "pinned commit 2ead3f1f covers GPQA Diamond (198 tasks). "
            "Published parity_experiment.json baseline: codex + gpt-5.2, "
            "3 trials, 198 Diamond tasks — Harbor 87.21% +/- 0.34 vs "
            "XuandongZhao/gpqa-harbor-adapter original 87.88% +/- 0.58. "
            "STAGE B COMPLICATION: Harbor's adapter covers Diamond only, "
            "but Loom's current `gpqa` adapter targets the Extended subset; "
            "Stage B must either point at Diamond or document the subset "
            "mismatch as a structural reason parity cannot be claimed."
        ),
        "decision": (
            "Real Harbor (harbor-framework/harbor) supports GPQA via "
            "`adapters/gpqa-diamond`, which targets the Diamond subset "
            "(198 tasks). Loom's `GPQAAdapter` currently emits the "
            "Extended subset, so matched-config paired runs require "
            "either reconfiguring Loom to Diamond or treating the subset "
            "mismatch as a structural blocker for parity. Stage B "
            "paired-run task tracked in a child issue (link added in "
            "Task A7) and must record the subset choice."
        ),
    },
    "livecodebench": {
        "status": "supported",
        "harbor_adapter_dir": _adapter_url("livecodebench"),
        "parity_target": (
            "harbor-framework/harbor's `adapters/livecodebench` adapter at "
            "pinned commit 2ead3f1f. Published parity_experiment.json "
            "baselines: (1) terminus-2 + gpt-5-mini, 4 trials, 100 "
            "release_v6 tasks — Harbor n/a (not run), TB adapter 76.50% "
            "+/- 0.50 vs audreycs/terminal-bench original 77.25% +/- 0.48; "
            "(2) claude-code@2.0.32 + claude-haiku-4-5, 4 trials, 100 "
            "release_v6 tasks — Harbor 53.25% +/- 0.95 vs TB adapter "
            "54.50% +/- 1.50."
        ),
        "decision": (
            "Real Harbor (harbor-framework/harbor) supports LiveCodeBench "
            "via `adapters/livecodebench`. Loom's `LiveCodeBenchAdapter` "
            "decodes the upstream IO/functional test cases and runs them "
            "under pytest (by-construction parity with the upstream "
            "evaluator); Harbor's adapter does the same against the same "
            "upstream dataset but via an independent code path. "
            "Matched-config paired runs against Harbor's published "
            "claude-haiku-4-5 baseline are required to validate score "
            "parity. Stage B paired-run task tracked in a child issue "
            "(link added in Task A7)."
        ),
    },
    "swe-bench-verified": {
        "status": "supported",
        "harbor_adapter_dir": _adapter_url("swebench"),
        "parity_target": (
            "harbor-framework/harbor's `adapters/swebench` adapter at "
            "pinned commit 2ead3f1f covers SWE-Bench Verified. Published "
            "parity_experiment.json baselines: (1) terminus-2 + "
            "Claude-Sonnet-4-5, 1 run, 500 tasks — Harbor 68.6% vs TB "
            "adapter 70.0%; (2) mini-swe-agent@2.1.0 + gpt-5-mini, 3 "
            "runs, 499 comparable tasks — Harbor (daytona) 54.5% +/- 0.7 "
            "vs swebench leaderboard 56.3%."
        ),
        "decision": (
            "Real Harbor (harbor-framework/harbor) supports SWE-Bench "
            "Verified via `adapters/swebench`. Loom's "
            "`SWEBenchVerifiedAdapter` emits the official SWE-Bench "
            "evaluation harness verbatim inside the per-instance "
            "`swebench/sweb.eval.x86_64.<slug>` Docker image; Harbor's "
            "adapter targets the same harness via an independent code "
            "path. Matched-config paired runs against Harbor's published "
            "Claude-Sonnet-4-5 or gpt-5-mini baselines are required to "
            "validate score parity. Stage B paired-run task tracked in "
            "a child issue (link added in Task A7)."
        ),
    },
}


_PENDING_REASON = (
    "Original Layer 2 evidence (PRs #499 #513 #514 #517) claimed "
    "parity-by-construction against coder-harbor-cloud (Huawei platform, "
    "wrong Harbor). Real Harbor (harbor-framework/harbor at 2ead3f1f) "
    "ships an adapter for this benchmark; matched-config paired runs "
    "required to validate score parity. Stage B child issue link added "
    "in Task A7."
)


def _apply_supported(entry: dict, harbor_support: dict[str, object]) -> None:
    entry["harbor_support"] = harbor_support
    layer2 = entry.setdefault("layer2_evidence", {})
    layer2["status"] = "pending_paired_run"
    layer2["parity_kind"] = "matched_config_paired_run_pending"
    layer2["pending_reason"] = _PENDING_REASON


def main() -> int:
    manifest = json.loads(MANIFEST.read_text())
    by_id = {b["benchmark_id"]: b for b in manifest["benchmarks"]}

    updated: list[str] = []
    for bid, harbor_support in SUPPORTED.items():
        if bid not in by_id:
            raise SystemExit(f"benchmark {bid!r} missing from manifest")
        _apply_supported(by_id[bid], harbor_support)
        updated.append(bid)

    MANIFEST.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
    )
    print(f"updated {len(updated)} entries in {MANIFEST}:")
    for bid in updated:
        print(f"  - {bid}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
