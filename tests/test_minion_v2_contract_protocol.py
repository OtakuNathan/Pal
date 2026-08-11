from __future__ import annotations

from pal.shared.tool_protocol import ToolCallIR, new_tool_call

import copy
import shutil
import tempfile
import unittest
from pathlib import Path

import yaml

from pal.minion.turns import sanitize_runner_session_pack
from pal.minion.v2.architecture_templates import ArchitectureTemplateCompiler
from pal.minion.v2.contract_protocol import (
    compile_contract_markdown,
    load_architect_yaml,
    read_architect_yaml,
    software_contract_projection,
    validate_contract_payload,
)
from pal.minion.v2.contract_submission import (
    architect_path,
    bind_architect_file,
    contract_submit_tool_result,
)
from pal.minion.v2.repository import MinionV2Repository
from pal.minion.v2.review_submission import review_submit_tool_result
from pal.minion.v2.submission_drafts import AUTHORING_CONTRACT_VERSION
from pal.minion.v2.skeleton import compile_skeleton_markdown
from pal.minion.v2.work_items import (
    ADD_FINDING_CAPABILITY,
    UPDATE_CHECKLIST_CAPABILITY,
    add_finding_tool_result,
    read_work_items,
    submission_work_items,
    update_checklist_tool_result,
)
from pal.shared import MinionInvocationPack


class ContractProtocolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="pal-contract-protocol-"))

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def test_submission_work_items_exposes_semantics_without_manager_identity(self) -> None:
        self.assertEqual(
            submission_work_items(
                [
                    {
                        "item_id": "work_internal",
                        "kind": "phase",
                        "status": "completed",
                        "summary": "settle the contract",
                        "ordinal": 0,
                        "origin": "role_playbook",
                        "required": True,
                        "semantic_hash": "manager-only",
                    }
                ]
            ),
            [
                {
                    "kind": "phase",
                    "status": "completed",
                    "summary": "settle the contract",
                }
            ],
        )

    def test_every_family_specialization_compiles_a_valid_example_and_template(
        self,
    ) -> None:
        compiler = ArchitectureTemplateCompiler()
        definitions = [
            compiler.compile(item.specialization_id)
            for item in compiler.list_specializations()
        ]
        self.assertEqual(
            [item.specialization_id for item in definitions],
            [
                "general.v1",
                "lifestyle.nutrition_checkin.v1",
                "software_engineering.v1",
            ],
        )
        for definition in definitions:
            with self.subTest(schema=definition.specialization_id):
                document = validate_contract_payload(
                    copy.deepcopy(definition.example),
                    definition=definition,
                )
                self.assertEqual(document.schema_version, "2")
                template = yaml.safe_load(definition.template)
                self.assertEqual(
                    set(template),
                    {
                        "schema_version",
                        "graph",
                        "context",
                        "requirements",
                        "modules",
                        "scenarios",
                    },
                )
                self.assertNotIn("contract_schema", template)
                self.assertIn(
                    "requirements: {}  # type: map[snake_case, Requirement], min 1",
                    definition.template,
                )
                self.assertIn(
                    'contract_flow: ["input -> provider output", "provider output -> consumer observation"]',
                    definition.template,
                )
                self.assertIn(
                    "type: ordered unique list[non-empty string], min 1",
                    definition.template,
                )
                if definition.specialization_id == "software_engineering.v1":
                    self.assertIn(
                        "keys exactly match provides",
                        definition.template,
                    )
                    self.assertIn(
                        "Stateful minimum valid shape",
                        definition.template,
                    )
                    self.assertIn(
                        "transitions:  # type: map[snake_case_event, Transition]",
                        definition.template,
                    )
                    self.assertIn(
                        "build_system:\n    system: replace_with_selected_or_inherited_build_system",
                        definition.template,
                    )
                    self.assertEqual(
                        definition.workspace_authority_rules[0]["id"],
                        "build_system",
                    )

    def test_software_build_property_is_required_and_compiled_from_property_data(self) -> None:
        definition = ArchitectureTemplateCompiler().compile(
            "software_engineering.v1"
        )
        payload = copy.deepcopy(definition.example)
        payload["context"].pop("build_system")
        with self.assertRaisesRegex(ValueError, "build_system.*required"):
            validate_contract_payload(payload, definition=definition)

        property_data = definition.property_data[0]
        self.assertEqual(property_data["target_pointer"], "/context/build_system")
        self.assertIn("project-owned manifest", property_data["guidance"])
        self.assertEqual(
            property_data["authoring_shape"]["write_scopes"][0]["kind"],
            "file",
        )

    def test_contract_graph_rejects_cycles_and_undeclared_outputs(self) -> None:
        definition = ArchitectureTemplateCompiler().compile("general.v1")
        payload = copy.deepcopy(definition.example)
        module_name = next(iter(payload["modules"]))
        payload["modules"][module_name]["dependencies"][module_name] = {
            "consumes": ["daily_plan"],
            "purpose": "cycle",
            "handoff": "invalid",
        }
        with self.assertRaisesRegex(ValueError, "cannot depend on itself"):
            validate_contract_payload(payload, definition=definition)

    def test_yaml_loader_rejects_aliases_and_duplicate_keys(self) -> None:
        definition = ArchitectureTemplateCompiler().compile("general.v1")
        aliased = self.root / "aliased.yaml"
        aliased.write_text(
            "schema_version: &version '1'\n"
            "context: {}\nrequirements: *version\nmodules: {}\nscenarios: {}\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, "aliases are not allowed"):
            load_architect_yaml(aliased, definition=definition)
        duplicate = self.root / "duplicate.yaml"
        duplicate.write_text(
            "schema_version: '1'\nschema_version: '1'\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, "duplicate key"):
            load_architect_yaml(duplicate, definition=definition)

    def test_yaml_loader_rejects_keyword_keys_before_role_gateway(self) -> None:
        path = self.root / "keyword-key.yaml"
        path.write_text(
            "schema_version: '1'\n"
            "context: {}\n"
            "requirements: {}\n"
            "modules:\n"
            "  decoder:\n"
            "    definition:\n"
            "      on: ready\n"
            "scenarios: {}\n",
            encoding="utf-8",
        )

        with self.assertRaisesRegex(
            ValueError,
            'mapping keys must be strings.*Quote YAML keyword-like keys such as "on"',
        ):
            read_architect_yaml(path)

    def test_software_projection_is_derived_and_preserves_semantics(self) -> None:
        definition = ArchitectureTemplateCompiler().compile(
            "software_engineering.v1"
        )
        payload = copy.deepcopy(definition.example)
        projected = software_contract_projection(payload)
        module_name = next(iter(payload["modules"]))
        self.assertEqual(projected["schema_version"], 5)
        self.assertEqual(
            projected["modules"][module_name]["responsibility"],
            payload["modules"][module_name]["responsibility"],
        )
        self.assertEqual(
            projected["context"]["build_system"]["owner"],
            "delivery",
        )
        self.assertNotIn("contract_schema", projected)

    def test_human_review_renders_only_the_completed_family_specialization(
        self,
    ) -> None:
        definition = ArchitectureTemplateCompiler().compile(
            "lifestyle.nutrition_checkin.v1"
        )
        markdown = compile_contract_markdown(
            copy.deepcopy(definition.example),
            requirements_payload={
                "schema_version": "1",
                "title": "Weekly nutrition plan",
                "original": {"objective": "Provide a feasible weekly plan."},
                "revisions": [],
            },
        )

        self.assertIn("## Family context", markdown)
        self.assertIn("safety_boundary:", markdown)
        self.assertIn("#### Family definition", markdown)
        self.assertIn("meals:", markdown)
        self.assertIn("- oats", markdown)
        self.assertIn("## Scenarios", markdown)
        self.assertNotIn("$schema", markdown)
        self.assertNotIn("$defs", markdown)
        self.assertNotIn("x-pal-specialization", markdown)
        self.assertNotIn("moduleDefinition", markdown)
        self.assertNotIn("specialization_id", markdown)

    def test_software_human_review_renders_build_authority_context(self) -> None:
        definition = ArchitectureTemplateCompiler().compile(
            "software_engineering.v1"
        )
        markdown = compile_skeleton_markdown(
            {
                "submission": software_contract_projection(
                    copy.deepcopy(definition.example)
                )
            },
            requirements_payload={
                "schema_version": "1",
                "title": "Frame decoder",
                "original": {"objective": "Decode frames."},
                "revisions": [],
            },
        )

        self.assertIn("## Family Context", markdown)
        self.assertIn('"build_system"', markdown)
        self.assertIn('"owner": "delivery"', markdown)
        self.assertIn('"path": "CMakeLists.txt"', markdown)

    def test_git_authoring_contract_uses_manager_path_and_survives_rebinding(self) -> None:
        definition = ArchitectureTemplateCompiler().compile(
            "software_engineering.v1"
        )
        repo = self.root / "repo"
        repo.mkdir()
        workspace = bind_architect_file(
            {
                "repo_path": str(repo),
                "contract_authoring_mode": True,
            },
            template=definition.template,
        )
        path = architect_path(workspace)
        self.assertEqual(path.parent.name, ".pal-minion-architect")
        self.assertTrue(path.is_file())
        authored = "schema_version: '1'\ntitle: authored contract\n"
        path.write_text(authored, encoding="utf-8")

        rebound = bind_architect_file(
            workspace,
            template="schema_version: overwritten-template\n",
            base_contract={"schema_version": "overwritten-base"},
        )

        self.assertEqual(architect_path(rebound), path)
        self.assertEqual(path.read_text(encoding="utf-8"), authored)

    def test_artifact_role_workspace_uses_one_visible_architect_path(self) -> None:
        definition = ArchitectureTemplateCompiler().compile(
            "lifestyle.nutrition_checkin.v1"
        )
        role_workspace = self.root / "role-workspace"
        artifact_stage = self.root / "artifact-stage"
        workspace = bind_architect_file(
            {
                "repo_path": str(role_workspace),
                "artifact_stage_dir": str(artifact_stage),
                "v2_role_workspace": True,
            },
            template=definition.template,
        )

        path = architect_path(workspace)
        self.assertEqual(path, role_workspace / "architect.yaml")
        self.assertTrue(path.is_file())
        self.assertFalse((artifact_stage / "architect.yaml").exists())

        authored = "schema_version: '2'\ngraph:\n  sink: week_plan\n"
        path.write_text(authored, encoding="utf-8")
        self.assertEqual(architect_path(workspace).read_text(encoding="utf-8"), authored)

    def test_runner_pack_never_receives_manager_architecture_compiler_state(
        self,
    ) -> None:
        sentinel = "RAW-SCHEMA-MUST-STAY-IN-MANAGER"
        pack = sanitize_runner_session_pack(
            MinionInvocationPack(
                invocation_id="inv-architect",
                goal="design",
                metadata={
                    "minion_v2": {
                        "workflow_id": "wf",
                        "contract_schema": {
                            "schema": {"description": sentinel},
                        },
                        "architecture_definition": {
                            "schema": {"description": sentinel},
                        },
                        "architecture_schema": {"description": sentinel},
                        "architect_template": sentinel,
                    }
                },
            )
        )
        serialized = str(pack.to_dict())
        self.assertNotIn(sentinel, serialized)
        self.assertEqual(
            dict(pack.metadata["minion_v2"]),
            {"workflow_id": "wf"},
        )


class WorkItemProtocolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="pal-work-items-"))
        self.repository = MinionV2Repository(self.root)
        self.repository.ensure_schema()
        self.invocation_id = "inv_contract_author"
        self.resource = "architecture:revision:author"
        lease = self.repository.claim_lease(
            self.resource,
            self.invocation_id,
            ttl_seconds=120,
        )
        self.workspace = {
            "runtime_root": str(self.root),
            "artifact_stage_dir": str(self.root / "stage"),
            "minion_v2": {
                "workflow_id": "wf_contract",
                "invocation_id": self.invocation_id,
                "lease_resource_key": self.resource,
                "fencing_token": lease.fencing_token,
                "role": "architect",
                "mode": "author",
                "authoring_input_fingerprint": "input-v1",
                "authoring_contract_version": AUTHORING_CONTRACT_VERSION,
                "role_protocol": {
                    "playbook": {
                        "steps": [
                            {
                                "key": "requirements_design",
                                "instruction": "Design first.",
                                "done_when": "The graph is settled.",
                            },
                            {
                                "key": "contract_projection",
                                "instruction": "Write the contract.",
                                "done_when": "The contract validates.",
                            },
                        ]
                    }
                },
                "work_item_seed": [
                    {
                        "kind": "phase",
                        "summary": "requirements design",
                        "status": "pending",
                        "required": True,
                    },
                    {
                        "kind": "phase",
                        "summary": "contract projection",
                        "status": "pending",
                        "required": True,
                    },
                ],
            },
        }
        definition = ArchitectureTemplateCompiler().compile("general.v1")
        self.workspace = bind_architect_file(
            self.workspace,
            template=definition.template,
            base_contract=definition.example,
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def test_contract_submit_requires_complete_manager_seed(self) -> None:
        premature = contract_submit_tool_result(
            new_tool_call(
                name="op_minion_contract_submit",
                args={},
                call_id="submit-premature",
            ),
            self.workspace,
        )
        self.assertFalse(premature.ok)
        self.assertIn("complete every checklist item", premature.llm_text)
        self.assertEqual(premature.invocation_result.kind, "rejected")
        self.assertEqual(premature.invocation_result.effect.value, "not_started")

        updated = update_checklist_tool_result(
            new_tool_call(
                name=UPDATE_CHECKLIST_CAPABILITY,
                args={
                    "plan": [
                        {
                            "step": "requirements design",
                            "status": "completed",
                        },
                        {
                            "step": "contract projection",
                            "status": "completed",
                        },
                    ]
                },
                call_id="checklist-complete",
            ),
            self.workspace,
        )
        self.assertTrue(updated.ok)
        submitted = contract_submit_tool_result(
            new_tool_call(
                name="op_minion_contract_submit",
                args={},
                call_id="submit-complete",
            ),
            self.workspace,
        )
        self.assertFalse(submitted.ok)
        self.assertIn("Manager gateway", submitted.llm_text)
        self.assertEqual(submitted.invocation_result.kind, "rejected")

    def test_invalid_checklist_is_a_pre_effect_rejection(self) -> None:
        invalid = update_checklist_tool_result(
            new_tool_call(
                name=UPDATE_CHECKLIST_CAPABILITY,
                args={
                    "plan": [
                        {
                            "step": "invented replacement phase",
                            "status": "in_progress",
                        }
                    ]
                },
                call_id="checklist-invalid",
            ),
            self.workspace,
        )

        self.assertFalse(invalid.ok)
        self.assertEqual(invalid.invocation_result.kind, "rejected")
        self.assertEqual(invalid.invocation_result.effect.value, "not_started")
        self.assertEqual(
            invalid.invocation_result.error_code,
            "invalid_work_item",
        )

    def test_reviewer_finding_identity_is_manager_generated_and_deduplicated(self) -> None:
        reviewer = self._reviewer_workspace()
        checklist = update_checklist_tool_result(
            new_tool_call(
                name=UPDATE_CHECKLIST_CAPABILITY,
                args={
                    "plan": [
                        {"step": "breadth audit", "status": "completed"},
                        {"step": "verdict", "status": "completed"},
                    ]
                },
                call_id="review-checklist",
            ),
            reviewer,
        )
        self.assertTrue(checklist.ok)
        args = {
            "finding_kind": "contract_defect",
            "priority": "p2",
            "disposition": "blocking",
            "summary": "The declared failure path has no legal terminal state.",
            "locations": [
                {
                    "scope": "workspace",
                    "file": "include/protocol.hpp",
                    "line": 12,
                }
            ],
        }
        first = add_finding_tool_result(
            new_tool_call(
                name=ADD_FINDING_CAPABILITY,
                args=args,
                call_id="finding-1",
            ),
            reviewer,
        )
        second = add_finding_tool_result(
            new_tool_call(
                name=ADD_FINDING_CAPABILITY,
                args=args,
                call_id="finding-2",
            ),
            reviewer,
        )
        self.assertTrue(first.ok)
        self.assertTrue(second.ok)
        self.assertEqual(
            first.structured["finding_id"],
            second.structured["finding_id"],
        )
        self.assertEqual(
            len(
                [
                    item
                    for item in read_work_items(reviewer)["items"]
                    if item["kind"] == "finding"
                ]
            ),
            1,
        )
        result = review_submit_tool_result(
            new_tool_call(
                name="op_minion_review_submit",
                args={},
                call_id="review-submit",
            ),
            reviewer,
        )
        self.assertTrue(result.ok)

    def _reviewer_workspace(self) -> dict[str, object]:
        invocation = "inv_contract_reviewer"
        resource = "architecture:revision:review"
        lease = self.repository.claim_lease(
            resource,
            invocation,
            ttl_seconds=120,
        )
        return {
            "runtime_root": str(self.root),
            "minion_v2": {
                "workflow_id": "wf_contract",
                "invocation_id": invocation,
                "lease_resource_key": resource,
                "fencing_token": lease.fencing_token,
                "role": "reviewer",
                "mode": "architecture",
                "authoring_input_fingerprint": "review-input-v1",
                "authoring_contract_version": AUTHORING_CONTRACT_VERSION,
                "work_item_seed": [
                    {
                        "kind": "phase",
                        "summary": "breadth audit",
                        "status": "pending",
                        "required": True,
                    },
                    {
                        "kind": "phase",
                        "summary": "verdict",
                        "status": "pending",
                        "required": True,
                    },
                ],
            },
        }
