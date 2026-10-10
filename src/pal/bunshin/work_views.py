from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Mapping
from pal.bunshin.contract_runtime import ContractArtifactAccess
from pal.bunshin.adapters import ARTIFACT_BUNDLE_ADAPTER, SOFTWARE_GIT_ADAPTER
from pal.bunshin.artifacts import ArtifactRef
from pal.bunshin.contracts import AggregateSnapshot
from pal.bunshin.contract_protocol import CONTRACT_ARTIFACT, software_contract_projection
from pal.bunshin.direct_contract import DIRECT_EXECUTION_ARTIFACT, report_only_candidate
from pal.bunshin.verification import repair_bill_semantic_view


@dataclass
class UnitWorkViewBuilder:
    contracts: ContractArtifactAccess

    @staticmethod
    def _entrypoint_view(value: Any) -> dict[str, Any]:
        """Normalize both contract entrypoint shapes at the work-view boundary.

        The family contract schema uses a structured ``{module, surface}``
        entrypoint, while the SWE skeleton projection deliberately preserves
        the authored scenario entrypoint as a compact string.  A work view is
        the common input to implementation and verification, so it must accept
        both representations instead of attempting ``dict(string)``.
        """

        if isinstance(value, Mapping):
            return dict(value)
        text = str(value or "").strip()
        return {"target": text} if text else {}

    def build(self, node: AggregateSnapshot) -> ArtifactRef:
        manifest_ref = dict(node.payload.get("architecture_manifest_ref") or {})
        record = self.contracts.repository.artifacts.read_artifact_record(str(manifest_ref.get("sha256") or ""))
        if record and record.get("artifact_type") == DIRECT_EXECUTION_ARTIFACT:
            artifact = dict(self.contracts.artifacts.read_json(manifest_ref))
            return self._build_skeleton_view(node, artifact_override=artifact)
        if record and str(record.get("artifact_type") or "") == CONTRACT_ARTIFACT:
            adapter = str(node.payload.get("execution_adapter") or "")
            if adapter == SOFTWARE_GIT_ADAPTER:
                artifact = dict(
                    self.contracts.artifacts.read_json(manifest_ref)
                )
                contract = dict(artifact.get("contract") or {})
                return self._build_skeleton_view(
                    node,
                    artifact_override={
                        **artifact,
                        "submission": software_contract_projection(contract),
                    },
                )
            if adapter == ARTIFACT_BUNDLE_ADAPTER:
                return self._build_data_contract_view(node)
            raise ValueError(
                "unit work view has no supported bound execution adapter"
            )
        raise ValueError(
            "unit work view requires a ContractArtifact"
        )

    def system_delivery_view(self, node: AggregateSnapshot) -> ArtifactRef:
        """Project whole-graph delivery semantics for the sink verifier only."""

        if not bool(node.payload.get("graph_sink")):
            raise ValueError("system delivery view is available only for the graph sink")
        manifest_ref = dict(node.payload.get("architecture_manifest_ref") or {})
        artifact = dict(self.contracts.artifacts.read_json(manifest_ref))
        adapter = str(node.payload.get("execution_adapter") or "")
        if artifact.get("execution_mode") == "direct":
            from pathlib import Path
            outputs = list(artifact.get("deliverable_paths") or [])
            target = str(node.payload.get("candidate_digest") or "")
            report_only = report_only_candidate(Path(str(node.payload["workspace_path"])),
                                                str(artifact["base_commit_sha"]), target, outputs)
            return self.contracts.artifacts.put_json(
                {"execution_mode": "direct", "requirements_ref": artifact["requirements_ref"],
                 "deliverable_paths": outputs, "report_only": report_only, "scenarios": {}, "entrypoints": []},
                artifact_type="SystemDeliveryViewArtifact",
                child_refs=((manifest_ref["sha256"], "task_binding"),
                            (node.payload["candidate_ref"]["sha256"], "candidate")),
            )
        if adapter == SOFTWARE_GIT_ADAPTER:
            full_contract = software_contract_projection(
                dict(artifact.get("contract") or {})
            )
        elif adapter == ARTIFACT_BUNDLE_ADAPTER:
            full_contract = dict(artifact.get("contract") or {})
        else:
            raise ValueError("system delivery view has no supported execution adapter")
        scenarios = dict(full_contract.get("scenarios") or {})
        payload = {
            "schema_version": "1",
            "graph_sink": True,
            "sink_module": str(
                node.payload.get("module_name")
                or node.payload.get("unit_id")
                or ""
            ),
            "requirements": dict(full_contract.get("requirements") or {}),
            "scenarios": scenarios,
            "entrypoints": [
                self._entrypoint_view(scenario.get("entrypoint"))
                for scenario in scenarios.values()
                if isinstance(scenario, Mapping)
                and self._entrypoint_view(scenario.get("entrypoint"))
            ],
            "family_context": dict(full_contract.get("context") or {}),
            "workspace_authorities": [
                dict(item)
                for item in list(
                    dict(node.payload.get("path_policy") or {}).get(
                        "workspace_authorities"
                    )
                    or []
                )
            ],
        }
        return self.contracts.artifacts.put_json(
            payload,
            artifact_type="SystemDeliveryViewArtifact",
            provenance={"owner": "manager", "audience": "sink_verifier"},
            child_refs=((str(manifest_ref["sha256"]), "contract"),),
        )

    def _build_skeleton_view(
        self,
        node: AggregateSnapshot,
        *,
        artifact_override: Mapping[str, Any] | None = None,
    ) -> ArtifactRef:
        manifest_ref = dict(node.payload.get("architecture_manifest_ref") or {})
        artifact = dict(
            artifact_override
            or self.contracts.artifacts.read_json(manifest_ref)
        )
        contract_ref = dict(node.payload.get("unit_contract_ref") or {})
        contract = dict(self.contracts.artifacts.read_json(contract_ref))
        submission = dict(artifact.get("submission") or {})
        all_modules = {
            str(name): dict(value or {})
            for name, value in dict(submission.get("modules") or {}).items()
        }
        path_policy = dict(node.payload.get("path_policy") or contract.get("paths") or {})
        module_name = str(contract.get("module_name") or node.payload.get("module_name") or "")
        semantic_module = dict(contract.get("module") or {})
        dependency_edges = {
            str(name): dict(value or {})
            for name, value in dict(semantic_module.get("dependencies") or {}).items()
        }
        dependency_names = set(dependency_edges)
        dependency_contract_slices: dict[str, Any] = {}
        for dependency_name in sorted(dependency_names):
            provider_module = dict(all_modules.get(dependency_name) or {})
            if not provider_module:
                raise ValueError(
                    f"module {module_name} has no accepted contract for "
                    f"dependency {dependency_name}"
                )
            provider_contract = dict(provider_module.get("contract") or {})
            provider_paths = dict(provider_module.get("paths") or {})
            edge = dependency_edges[dependency_name]
            consumed = [
                str(item) for item in list(edge.get("consumes") or [])
            ]
            dependency_contract_slices[dependency_name] = {
                "edge": edge,
                "contract_paths": list(
                    provider_paths.get("contract_paths") or []
                ),
                "consumed_outputs": {
                    name: dict(
                        dict(provider_contract.get("outputs") or {}).get(name) or {}
                    )
                    for name in consumed
                },
                "errors": list(provider_contract.get("errors") or []),
                "invariants": list(provider_contract.get("invariants") or []),
                "ownership": list(provider_module.get("ownership") or []),
                "lifecycle": dict(provider_module.get("lifecycle") or {}),
                "state_machine": provider_module.get("state_machine"),
            }
        historical_refs = [
            dict(item)
            for item in list(node.payload.get("historical_repair_bill_refs") or [])
            if isinstance(item, Mapping) and item.get("sha256")
        ]
        bound_requirements = dict(contract.get("requirements") or {})
        bound_scenarios = dict(contract.get("scenarios") or {})
        payload = {
            "schema_version": "3",
            "execution_mode": str(artifact.get("execution_mode") or "planned"),
            "requirements_ref": dict(artifact.get("requirements_ref") or {}),
            "deliverable_paths": list(artifact.get("deliverable_paths") or []),
            "direct_reference_refs": dict(artifact.get("direct_reference_refs") or {}),
            "module_name": module_name,
            "graph_sink": bool(node.payload.get("graph_sink")),
            "context": dict(submission.get("context") or {}),
            "module": semantic_module,
            "contract_mode": str(path_policy.get("contract_mode") or "review_guarded"),
            "contract_paths": list(path_policy.get("contract_paths") or []),
            "implementation_scopes": list(path_policy.get("implementation_scopes") or []),
            "developer_tests": dict(path_policy.get("developer_tests") or {}),
            "verification_corpus": dict(path_policy.get("verification_corpus") or {}),
            "reference_only": list(path_policy.get("reference_only") or []),
            "workspace_authorities": [
                dict(item)
                for item in list(path_policy.get("workspace_authorities") or [])
            ],
            "requirements": bound_requirements,
            "scenarios": bound_scenarios,
            "entrypoints": [
                self._entrypoint_view(scenario.get("entrypoint"))
                for scenario in bound_scenarios.values()
                if isinstance(scenario, Mapping)
                and self._entrypoint_view(scenario.get("entrypoint"))
            ],
            "dependency_contracts": dependency_contract_slices,
            "consumer_obligations": {
                name: dict(dict(value.get("dependencies") or {}).get(module_name) or {})
                for name, value in all_modules.items()
                if module_name in dict(value.get("dependencies") or {})
            },
            "historical_repair_bills": [
                repair_bill_semantic_view(self.contracts.artifacts, item) for item in historical_refs
            ],
        }
        return self.contracts.artifacts.put_json(
            payload,
            artifact_type="ModuleWorkViewArtifact",
            child_refs=(
                (str(manifest_ref["sha256"]), "architecture_skeleton"),
                (str(contract_ref["sha256"]), "module_contract"),
                *((str(item["sha256"]), "historical_repair_bill") for item in historical_refs),
            ),
        )

    def _build_data_contract_view(
        self,
        node: AggregateSnapshot,
    ) -> ArtifactRef:
        manifest_ref = dict(node.payload.get("architecture_manifest_ref") or {})
        contract_ref = dict(node.payload.get("unit_contract_ref") or {})
        module_contract = dict(
            self.contracts.artifacts.read_json(contract_ref)
        )
        manifest = dict(
            self.contracts.artifacts.read_json(manifest_ref)
        )
        full_contract = dict(manifest.get("contract") or {})
        all_modules = {
            str(name): dict(value or {})
            for name, value in dict(full_contract.get("modules") or {}).items()
        }
        owned_module = dict(module_contract.get("module") or {})
        dependency_contracts = {
            provider: {
                "module": all_modules[provider],
                "handoff": dict(dependency or {}),
            }
            for provider, dependency in dict(
                owned_module.get("dependencies") or {}
            ).items()
            if provider in all_modules
        }
        bound_requirements = dict(
            module_contract.get("requirements") or {}
        )
        bound_scenarios = dict(module_contract.get("scenarios") or {})
        payload = {
            "schema_version": "3",
            "execution_adapter": ARTIFACT_BUNDLE_ADAPTER,
            "module_name": str(
                module_contract.get("module_name")
                or node.payload.get("module_name")
                or ""
            ),
            "graph_sink": bool(node.payload.get("graph_sink")),
            "module": owned_module,
            "context": dict(module_contract.get("context") or {}),
            "requirements": bound_requirements,
            "scenarios": bound_scenarios,
            "entrypoints": [
                self._entrypoint_view(scenario.get("entrypoint"))
                for scenario in bound_scenarios.values()
                if isinstance(scenario, Mapping)
                and self._entrypoint_view(scenario.get("entrypoint"))
            ],
            "dependency_contracts": dependency_contracts,
            "historical_repair_bills": [
                repair_bill_semantic_view(self.contracts.artifacts, item)
                for item in list(
                    node.payload.get("historical_repair_bill_refs") or []
                )
                if isinstance(item, Mapping) and item.get("sha256")
            ],
        }
        return self.contracts.artifacts.put_json(
            payload,
            artifact_type="ModuleWorkViewArtifact",
            child_refs=(
                (str(manifest_ref["sha256"]), "contract"),
                (str(contract_ref["sha256"]), "module_contract"),
            ),
        )
