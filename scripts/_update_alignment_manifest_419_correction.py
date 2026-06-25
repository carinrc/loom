"""One-shot manifest editor for #419 Harbor reference correction.

The original Layer 2 batches (PRs #499, #513, #514, #517) wrote the
manifest against `coder-harbor-cloud` (a Huawei agent platform mistakenly
identified as "Harbor"). The real Harbor is `harbor-framework/harbor`,
pinned in `harbor_reference` at commit `2ead3f1f` (Task A2).

This script rewrites `harbor_support` for:
- The 5 v1.0 benchmarks where the real Harbor ships an adapter (Task A3,
  `SUPPORTED` dict).
- The 7 v1.0 benchmarks where the real Harbor does NOT ship an adapter
  (Task A4, `UNSUPPORTED` dict). For these, `harbor_support.status`
  stays `not_supported` but `parity_target` + `decision` are rewritten
  against `harbor-framework/harbor@2ead3f1f` (with the
  `docs/research/harbor-adapter-snapshot-2026-06-25.md` snapshot cited)
  and `layer2_evidence.parity_kind` is set to
  `upstream_canonical_by_construction`. `layer2_evidence.status` stays
  `replay_validated` because the upstream-canonical-by-construction
  equivalence still holds.

The script is one-shot and will be DELETED in Task A8.

Mutation summary per entry (SUPPORTED batch):
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


_SNAPSHOT_DOC = "docs/research/harbor-adapter-snapshot-2026-06-25.md"


UNSUPPORTED: dict[str, dict[str, str]] = {
    "humaneval": {
        "status": "not_supported",
        "parity_target": (
            "The canonical upstream OpenAI HumanEval scorer is the parity "
            "target: each task's `check(candidate)` function executed "
            "against the model's completion. Real Harbor "
            "(harbor-framework/harbor at 2ead3f1f, see "
            f"{_SNAPSHOT_DOC}) ships `adapters/humanevalfix` (the "
            "HumanEval+ bugfix variant) but no plain HumanEval adapter; "
            "humanevalfix scores bug-fix correctness on a different task "
            "set with different prompts and is NOT a valid parity target "
            "for plain HumanEval."
        ),
        "decision": (
            "Real Harbor (harbor-framework/harbor at 2ead3f1f) ships no "
            f"plain HumanEval adapter (see {_SNAPSHOT_DOC}); the "
            "humanevalfix adapter targets a different task set. Loom's "
            "`HumanEvalAdapter` emits the upstream OpenAI `check(candidate)` "
            "function verbatim into the pytest harness, so Loom's verifier "
            "IS the canonical OpenAI HumanEval scorer by construction. "
            "Equivalence is proven by replay, not paired-runtime comparison."
        ),
    },
    "mbpp": {
        "status": "not_supported",
        "parity_target": (
            "The canonical upstream sanitized-MBPP test-string scorer is "
            "the parity target: each task's bundled assertion statements "
            "executed against the model's solution under pytest. Real "
            "Harbor (harbor-framework/harbor at 2ead3f1f, see "
            f"{_SNAPSHOT_DOC}) ships no MBPP adapter."
        ),
        "decision": (
            "Real Harbor (harbor-framework/harbor at 2ead3f1f) ships no "
            f"MBPP adapter (see {_SNAPSHOT_DOC}). Loom's `MBPPAdapter` "
            "writes the upstream sanitized-MBPP test strings verbatim "
            "into the bundled pytest files, so Loom's verifier IS the "
            "canonical MBPP scorer by construction. Equivalence is "
            "proven by replay, not paired-runtime comparison."
        ),
    },
    "math-500": {
        "status": "not_supported",
        "parity_target": (
            "The canonical upstream MATH-500 scorer (HuggingFaceH4/MATH-500 "
            "boxed-answer equivalence, inherited from the original "
            "Hendrycks MATH evaluator) is the parity target. Real Harbor "
            "(harbor-framework/harbor at 2ead3f1f, see "
            f"{_SNAPSHOT_DOC}) ships no MATH-500 adapter; the closest "
            "math adapters in Harbor (`aime`, `ineqmath`, `omnimath`) "
            "cover different task sets and are NOT parity targets for "
            "MATH-500."
        ),
        "decision": (
            "Real Harbor (harbor-framework/harbor at 2ead3f1f) ships no "
            f"MATH-500 adapter (see {_SNAPSHOT_DOC}). Loom's "
            "`MATH500Adapter` inherits from `HendrycksMATHAdapter` and "
            "uses the boxed-answer equivalence routine the MATH paper and "
            "HuggingFaceH4/MATH-500 use, so Loom's verifier IS the "
            "canonical MATH-500 scorer by construction. Equivalence is "
            "proven by replay, not paired-runtime comparison."
        ),
    },
    "mmlu-pro": {
        "status": "not_supported",
        "parity_target": (
            "The canonical upstream MMLU-Pro scorer (exact-letter match "
            "against the dataset row's canonical answer) is the parity "
            "target. Real Harbor (harbor-framework/harbor at 2ead3f1f, "
            f"see {_SNAPSHOT_DOC}) ships `adapters/mmmlu` (the M-MMLU "
            "multilingual variant) but no MMLU-Pro adapter; mmmlu covers "
            "a different question pool with multilingual prompts and is "
            "NOT a valid parity target for MMLU-Pro."
        ),
        "decision": (
            "Real Harbor (harbor-framework/harbor at 2ead3f1f) ships no "
            f"MMLU-Pro adapter (see {_SNAPSHOT_DOC}); the mmmlu adapter "
            "targets a different (multilingual) question pool. Loom's "
            "`MMLUProAdapter` implements exact-letter match against the "
            "MMLU-Pro dataset row's canonical answer, so Loom's verifier "
            "IS the canonical MMLU-Pro scorer by construction. "
            "Equivalence is proven by replay, not paired-runtime "
            "comparison."
        ),
    },
    "terminal-bench-2": {
        "status": "not_supported",
        "parity_target": (
            "The canonical upstream Terminal-Bench 2 (laude-institute) "
            "test runner is the parity target. Real Harbor "
            "(harbor-framework/harbor) IS descended from terminal-bench, "
            "but at commit 2ead3f1f the `adapters/` directory ships no "
            f"terminal-bench-2 adapter (see {_SNAPSHOT_DOC}) — TB-2 is "
            "Harbor's host benchmark framework itself, not an adapted "
            "external benchmark."
        ),
        "decision": (
            "Real Harbor (harbor-framework/harbor at 2ead3f1f) ships no "
            f"terminal-bench-2 adapter (see {_SNAPSHOT_DOC}) because TB-2 "
            "is the host framework Harbor is built on, not an external "
            "benchmark Harbor adapts. Loom's TB-2 adapter wraps the "
            "upstream TB-2 test runner verbatim, so Loom's verifier IS "
            "the canonical TB-2 scorer by construction. Equivalence is "
            "proven by replay, not paired-runtime comparison."
        ),
    },
    "skillflow": {
        "status": "not_supported",
        "parity_target": (
            "The canonical upstream SkillFlow task-bundle scorer "
            "(pre-baked solution + pytest tests bundled with each task) "
            "is the parity target. Real Harbor (harbor-framework/harbor "
            f"at 2ead3f1f, see {_SNAPSHOT_DOC}) ships no SkillFlow "
            "adapter — SkillFlow is a Loom-supported external benchmark "
            "not in Harbor's catalog."
        ),
        "decision": (
            "Real Harbor (harbor-framework/harbor at 2ead3f1f) ships no "
            f"SkillFlow adapter (see {_SNAPSHOT_DOC}); SkillFlow is not "
            "in Harbor's catalog. Loom's SkillFlow adapter passes "
            "through the upstream task bundle's pre-baked solution + "
            "tests, so pytest IS the canonical SkillFlow scorer by "
            "construction. Equivalence is proven by replay, not "
            "paired-runtime comparison."
        ),
    },
    "skilllearnbench": {
        "status": "not_supported",
        "parity_target": (
            "The canonical upstream SkillLearnBench task-bundle scorer is "
            "the parity target. Real Harbor (harbor-framework/harbor at "
            f"2ead3f1f, see {_SNAPSHOT_DOC}) ships no SkillLearnBench "
            "adapter — SkillLearnBench is a Loom-supported external "
            "benchmark not in Harbor's catalog."
        ),
        "decision": (
            "Real Harbor (harbor-framework/harbor at 2ead3f1f) ships no "
            f"SkillLearnBench adapter (see {_SNAPSHOT_DOC}); "
            "SkillLearnBench is not in Harbor's catalog. Loom's "
            "SkillLearnBench adapter passes through the upstream task "
            "bundle's structure unchanged, so pytest IS the canonical "
            "SkillLearnBench evaluator by construction. Equivalence is "
            "proven by replay, not paired-runtime comparison."
        ),
    },
}


def _apply_supported(entry: dict, harbor_support: dict[str, object]) -> None:
    entry["harbor_support"] = harbor_support
    layer2 = entry.setdefault("layer2_evidence", {})
    layer2["status"] = "pending_paired_run"
    layer2["parity_kind"] = "matched_config_paired_run_pending"
    layer2["pending_reason"] = _PENDING_REASON


def _apply_unsupported(entry: dict, harbor_support: dict[str, str]) -> None:
    entry["harbor_support"] = harbor_support
    layer2 = entry.setdefault("layer2_evidence", {})
    # status stays "replay_validated" — upstream-canonical-by-construction
    # equivalence still holds. parity_kind is made explicit.
    layer2["parity_kind"] = "upstream_canonical_by_construction"


def main() -> int:
    manifest = json.loads(MANIFEST.read_text())
    by_id = {b["benchmark_id"]: b for b in manifest["benchmarks"]}

    updated: list[str] = []
    for bid, harbor_support in SUPPORTED.items():
        if bid not in by_id:
            raise SystemExit(f"benchmark {bid!r} missing from manifest")
        _apply_supported(by_id[bid], harbor_support)
        updated.append(bid)

    for bid, harbor_support in UNSUPPORTED.items():
        if bid not in by_id:
            raise SystemExit(f"benchmark {bid!r} missing from manifest")
        _apply_unsupported(by_id[bid], harbor_support)
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
