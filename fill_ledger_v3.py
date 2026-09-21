#!/usr/bin/env python3
"""Fill acceptance_status.json with REAL evidence only (v3 delivery).

Maps only cases whose test nodes actually exist and were executed green at
the final HEAD; everything else stays NOT_RUN.  No fill-by-name magic.
"""
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PY = sys.executable
ENV = {"PYTHONPATH": str(ROOT / "src"), "PATH": "/usr/bin:/bin"}

# id -> (test node, note)
MAP = {
    "A02": ("tests/test_builder_mode_contract.py::BuilderModeContractTests::test_mode_is_public_and_validated",
            "mode is a public validated constructor param; builder forwards (post-build private writes removed in 939db45)"),
    "A03": ("tests/test_history_root_authority.py::PartitionTests::test_left_plus_right_partitions_store_exactly",
            "left+right partition the single store exactly, no mirror history"),
    "L01": ("tests/test_history_root_authority.py::LPromoteTests::test_L01_open_group_stays_whole_in_right", ""),
    "L02": ("tests/test_history_root_authority.py::LPromoteTests::test_L02_settle_does_not_auto_promote", ""),
    "L03": ("tests/test_history_root_authority.py::LPromoteTests::test_L03_compact_original_L_latest_R_not_moved", ""),
    "L04": ("tests/test_history_root_authority.py::LPromoteTests::test_L04_promote_preserves_content_and_order", ""),
    "L05": ("tests/test_history_root_authority.py::LPromoteTests::test_L05_result_arriving_during_compact_lands_in_R_once", ""),
    "L06": ("tests/test_history_root_authority.py::LPromoteTests::test_L06_result_and_commit_commute", ""),
    "L07": ("tests/test_history_root_authority.py::LPromoteTests::test_L07_cut_frozen_while_compact_live", ""),
    "L08": ("tests/test_history_root_authority.py::LPromoteTests::test_L08_minimal_left_refuses_recompaction", ""),
    "C01": ("tests/test_history_root_authority.py::CCompactTests::test_C01_snapshot_reads_left_only", ""),
    "C02": ("tests/test_history_root_authority.py::CCompactTests::test_C02_install_replaces_only_left", ""),
    "C03": ("tests/test_history_root_authority.py::CCompactTests::test_C03_failure_keeps_current_R_additions", ""),
    "C04": ("tests/test_history_root_authority.py::CCompactTests::test_C04_validator_rejection_never_installs",
            "owner-level terminal side; engine validator rejection pinned by inherited v2 schema tests"),
    "C05": ("tests/test_history_root_authority.py::CCompactTests::test_C05_construction_failure_does_not_half_publish", ""),
    "C06": ("tests/test_history_root_authority.py::CCompactTests::test_C06_duplicate_completion_installs_once", ""),
    "C07": ("tests/test_history_root_authority.py::CCompactTests::test_C07_conflicting_candidate_rejected", ""),
    "C08": ("tests/test_history_root_authority.py::CCompactTests::test_C08_full_left_source_is_not_truncated", ""),
    "X01": ("tests/test_history_root_authority.py::XArbitrationTests::test_X01_cancel_wins_then_late_summary_rejected", ""),
    "X02": ("tests/test_history_root_authority.py::XArbitrationTests::test_X02_commit_wins_interrupt_keeps_new_left", ""),
    "X03": ("tests/test_history_root_authority.py::XArbitrationTests::test_X03_idle_manual_is_not_a_turn", ""),
    "X04": ("tests/test_history_root_authority.py::XArbitrationTests::test_X04_old_run_events_cannot_touch_new_run", ""),
    "X05": ("tests/test_history_root_authority.py::XArbitrationTests::test_X05_reset_fences_old_incarnation_producers", ""),
    "X06": ("tests/test_history_root_authority.py::XArbitrationTests::test_X06_left_rebase_does_not_fence_producers", ""),
    "X07": ("tests/test_history_root_authority.py::XArbitrationTests::test_X07_interrupt_never_touches_left", ""),
    "X08": ("tests/test_history_root_authority.py::XArbitrationTests::test_X08_deadline_failure_beats_late_network_success", ""),
    "P01": ("tests/test_projection_two_segment.py::TwoSegmentOracleTests::test_P01_incremental_equals_full_across_shapes_and_rebase", ""),
    "P02": ("tests/test_projection_two_segment.py::TwoSegmentOracleTests::test_P02_preamble_once_and_never_frozen", ""),
    "P03": ("tests/test_projection_two_segment.py::TwoSegmentOracleTests::test_P03_anthropic_user_seam_survives_rebase", ""),
    "P05": ("tests/test_projection_two_segment.py::TwoSegmentOracleTests::test_P05_rebase_keeps_right_wire_and_native", ""),
    "P06": ("tests/test_projection_two_segment.py::TwoSegmentOracleTests::test_P06_endpoint_switch_differs_from_rebase", ""),
    "P07": ("tests/test_projection_two_segment.py::TwoSegmentOracleTests::test_P07_empty_tail_and_seed_only_view", ""),
    "P08": ("tests/test_projection_two_segment.py::TwoSegmentOracleTests::test_P08_handoff_leaves_normal_lineage_untouched", ""),
    "W01": ("tests/test_two_segment_executor_flow.py::TwoSegmentExecutorFlowTests::test_capture_left_and_engine_install_left_only",
            "single generation, no confirmation wait, no prewarm (structural)"),
    "W04": ("tests/test_projection_two_segment.py::HandoffRequestTests::test_W04_shell_policy_carried_verbatim", ""),
    "W05": ("tests/test_projection_two_segment.py::HandoffRequestTests::test_W05_tools_schema_stays_in_envelope", ""),
    "W06": ("tests/test_projection_two_segment.py::HandoffRequestTests::test_W_left_only_with_instruction",
            "cold-left full encode: no prewarm, no cached-prefix assumptions"),
    "H01": ("tests/test_h01_combination_matrix.py::H01CombinationMatrixTests::test_H01_matrix",
            "12 combinations (2 hosts x 3 shapes x 2 modes); warm column = honest cold fallback"),
    "B01": ("tests/test_two_segment_budget_facts.py::OwnerBudgetFactsTests::test_B01_local_history_may_exceed_any_window", ""),
    "B03": ("tests/test_two_segment_budget_facts.py::OwnerBudgetFactsTests::test_B03_handoff_source_must_fit_the_visible_budget", ""),
    "Q03": ("tests/test_two_segment_budget_facts.py::OwnerBudgetFactsTests::test_Q03_queued_input_never_enters_the_left_source", ""),
}

NOT_RUN_NOTES = {
    "P04": "explicit node not authored; native one-representation pinned by inherited projection suites, two-segment rebase covered by P05",
    "W02": "marker attribution is engine/cache-layer; session test asserts content-only left; NOT honestly claimable",
    "H02": "root snapshot persistence not implemented (see DELIVERY §4)",
    "H04": "old-schema explicit migration not implemented",
    "H06": "staging gate UX: staging auto-wire off in two_segment, no files can exist",
    "Q04": "two_segment BUSY routing at runtime level not wired",
}


def main() -> int:
    package_ledger = Path.home() / "Documents/coding/pal_two_segment_v3_20260920/acceptance_status.json"
    ledgers = [ROOT / "acceptance_status.json", package_ledger]
    results = {}
    for node in sorted({node for node, _ in MAP.values()}):
        cmd = [PY, "-m", "pytest", node, "-q", "-p", "no:cacheprovider"]
        proc = subprocess.run(cmd, cwd=ROOT, env=ENV, capture_output=True, text=True)
        results[node] = proc.returncode
        print(node, "->", proc.returncode)
    failures = {n: c for n, c in results.items() if c != 0}
    if failures:
        print("REFUSING TO FILL: failing nodes", failures)
        return 1
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT,
                         capture_output=True, text=True).stdout.strip()
    for path in ledgers:
        data = json.loads(path.read_text())
        for case in data["cases"]:
            case_id = case["id"]
            if case_id in MAP:
                node, note = MAP[case_id]
                case["status"] = "PASS"
                case["evidence"] = [{
                    "node": node,
                    "command": f"PYTHONPATH=src python3 -m pytest {node} -q",
                    "exit": 0,
                    "product_sha": sha,
                    "note": note,
                }]
            elif case_id in NOT_RUN_NOTES:
                case["status"] = "NOT_RUN"
                case["evidence"] = [{"note": NOT_RUN_NOTES[case_id]}]
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    passed = sum(1 for c in json.loads(ledgers[0].read_text())["cases"] if c["status"] == "PASS")
    print(f"LEDGER WRITTEN: {passed}/72 PASS, product_sha={sha}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
