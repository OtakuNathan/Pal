"""Source inventory must cover expanded role tools and separate model surfaces."""
from functools import lru_cache

from pal.bunshin.scoped_execution import _WORKSPACE_TOOL_SPECS
from scripts.audit_tool_discovery import declared_tools
from scripts.audit_tool_surface import build_inventory
from scripts.benchmark_full_discovery_flow import build_flow_catalog


@lru_cache(maxsize=1)
def _inventory():
    return build_inventory()


def test_inventory_covers_assembled_roles_including_generated_aliases():
    rows = declared_tools()
    roles = {row.alias for row in rows if row.role_scoped}
    assert roles == {spec["alias"] for spec in _WORKSPACE_TOOL_SPECS.values()}
    generated = next(row for row in rows if row.alias == "run_verification_compile_check")
    assert generated.role_scoped
    assert generated.generated_spec_key == "op_bunshin_verification_run_compile_check"
    records, cases, generations = build_flow_catalog()
    for canonical, spec in _WORKSPACE_TOOL_SPECS.items():
        alias = spec["alias"]
        assert records[alias]["invocation_mode"] == "direct"
        assert records[alias]["input_contract"]["properties"].keys() == spec["InputModel"].model_json_schema().get("properties", {}).keys()
        assert alias in generations["role_union"].search_records
        assert alias not in generations["resident_union"].search_records
        assert any(case["alias"] == alias and case["group"] == "role_union" for case in cases)


def test_counts_deduplicate_providers_without_collapsing_visibility():
    report = _inventory()
    rows = {row["alias"]: row for row in report["tools"]}
    counts = report["counts"]
    assert counts["declarations"] - counts["distinct_aliases"] == sum(len(sources) - 1 for sources in report["duplicate_declarations"].values())
    assert counts["distinct_aliases"] == counts["resident_union"] + counts["role_only_union"]
    assert counts["resident_union"] == counts["resident_native"] + counts["resident_search_only"]
    assert counts["resident_native"] == counts["resident_direct"] + counts["resident_native_opt_ins"]
    assert len(rows["inspect_memory_provider_inventory"]["sources"]) == 2
    assert rows["inspect_memory_provider_inventory"]["module"] == "memory"
    assert rows["remember_memory"]["invocation_mode"] == "indirect"
    assert rows["remember_memory"]["exposure"] == "native"
    assert rows["read_file"]["exposure"] == "native"
    assert "inspect_file_cache" not in rows
    assert "write_provider_memory" not in rows
    assert sum(module["aliases"] for module in report["modules"].values()) == len(rows)
