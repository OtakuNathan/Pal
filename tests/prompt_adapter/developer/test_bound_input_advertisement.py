"""Developer tests for bound-input advertisement in the role prompt.

The prompt adapter's contract: every ``bound_input`` reference entry that
carries its deterministic in-workspace location is advertised in the
Immutable Inputs section with that exact path and
``projected_paths=[repo_path]``, while rendering stays total, adds no host
paths, and keeps today's rendering for entries without ``bound_input`` and
for legacy bound entries whose path is a hidden gateway storage path.
"""

from __future__ import annotations

import unittest

from pal.bunshin.prompt_adapter import render_bunshin_task_prompt
from pal.shared import BunshinInvocationPack


def _pack_with_references(references: list[dict]) -> BunshinInvocationPack:
    return BunshinInvocationPack(
        invocation_id="inv-prompt-adapter-dev",
        goal="inspect",
        workspace={"reference_paths": references},
    )


class BoundInputAdvertisementTests(unittest.TestCase):
    def test_materialized_bound_input_advertises_exact_workspace_path(self) -> None:
        pack = _pack_with_references(
            [
                {
                    "name": "spec_doc",
                    "path": "/runtime/workspaces/ws-1/inputs/spec_doc/docs/spec.md",
                    "include": ["docs/spec.md"],
                    "mode": "read_only",
                    "truth_source": True,
                    "required": True,
                    "bound_input": True,
                }
            ]
        )

        prompt = render_bunshin_task_prompt(pack)

        self.assertIn(
            "- reference:spec_doc: read-only semantic input; "
            "access=ordinary file/search tools; truth_source=True; "
            "path=/runtime/workspaces/ws-1/inputs/spec_doc/docs/spec.md; "
            'read_file_args={"file_path":"/runtime/workspaces/ws-1/inputs/spec_doc/docs/spec.md"}; '
            'projected_paths=["docs/spec.md"]',
            prompt,
        )

    def test_relative_deterministic_location_is_advertised_verbatim(self) -> None:
        pack = _pack_with_references(
            [
                {
                    "name": "notes",
                    "path": "inputs/notes/docs/notes.md",
                    "include": ["docs/notes.md"],
                    "truth_source": True,
                    "bound_input": True,
                }
            ]
        )

        prompt = render_bunshin_task_prompt(pack)

        self.assertIn("path=inputs/notes/docs/notes.md", prompt)
        self.assertIn('projected_paths=["docs/notes.md"]', prompt)
        self.assertNotIn("/pal/references/notes", prompt)

    def test_in_repo_bound_input_of_repository_workspace_is_advertised(self) -> None:
        pack = _pack_with_references(
            [
                {
                    "name": "contract",
                    "path": "/worktrees/task-1/modules/docs/contract.md",
                    "include": ["docs/contract.md"],
                    "truth_source": True,
                    "bound_input": True,
                }
            ]
        )

        prompt = render_bunshin_task_prompt(pack)

        self.assertIn("path=/worktrees/task-1/modules/docs/contract.md", prompt)
        self.assertIn('projected_paths=["docs/contract.md"]', prompt)

    def test_legacy_bound_storage_path_stays_hidden(self) -> None:
        pack = _pack_with_references(
            [
                {
                    "name": "revision_finding",
                    "path": "/host-only/artifacts/secret.json",
                    "bound_input": True,
                    "required": True,
                    "truth_source": True,
                }
            ]
        )

        prompt = render_bunshin_task_prompt(pack)

        self.assertIn("reference:revision_finding", prompt)
        self.assertIn(
            "- reference:revision_finding: read-only semantic input; "
            "access=ordinary file/search tools; truth_source=True\n",
            prompt,
        )
        self.assertNotIn("/host-only/artifacts/secret.json", prompt)
        self.assertNotIn("/pal/references/revision_finding", prompt)

    def test_malformed_bound_entries_fall_back_without_raising(self) -> None:
        pack = _pack_with_references(
            [
                {
                    # Path does not name the included repo path.
                    "name": "mismatched",
                    "path": "/runtime/other/root.json",
                    "include": ["docs/spec.md"],
                    "bound_input": True,
                },
                {
                    # Glob include cannot identify one deterministic file.
                    "name": "globbed",
                    "path": "/runtime/workspaces/ws-1/inputs/globbed/docs",
                    "include": ["docs/*.md"],
                    "bound_input": True,
                },
                {
                    # No path at all.
                    "name": "pathless",
                    "include": ["docs/spec.md"],
                    "bound_input": True,
                },
            ]
        )

        prompt = render_bunshin_task_prompt(pack)

        self.assertIn("reference:mismatched", prompt)
        self.assertIn("reference:globbed", prompt)
        self.assertIn("reference:pathless", prompt)
        self.assertNotIn("path=/runtime/other/root.json", prompt)
        self.assertNotIn("/pal/references/mismatched", prompt)
        self.assertNotIn("/pal/references/globbed", prompt)
        self.assertNotIn("/pal/references/pathless", prompt)

    def test_entries_without_bound_input_keep_today_rendering(self) -> None:
        pack = _pack_with_references(
            [
                {
                    "name": "task",
                    "path": "/pal/references/task",
                    "include": ["task.yaml"],
                    "truth_source": True,
                },
                {
                    "name": "anonymous",
                    "include": [],
                },
            ]
        )

        prompt = render_bunshin_task_prompt(pack)

        self.assertIn("path=/pal/references/task", prompt)
        self.assertIn(
            'read_file_args={"file_path":"/pal/references/task"}',
            prompt,
        )
        self.assertIn("path=/pal/references/anonymous", prompt)

    def test_mixed_reference_kinds_render_totally(self) -> None:
        pack = _pack_with_references(
            [
                {
                    "name": "a_doc",
                    "path": "/runtime/workspaces/ws-2/inputs/a_doc/docs/a.md",
                    "include": ["docs/a.md"],
                    "truth_source": True,
                    "bound_input": True,
                },
                {
                    "name": "legacy",
                    "path": "/host-only/artifacts/legacy.json",
                    "bound_input": True,
                },
                {
                    "name": "plain",
                    "path": "/pal/references/plain",
                },
            ]
        )

        prompt = render_bunshin_task_prompt(pack)

        self.assertIn("path=/runtime/workspaces/ws-2/inputs/a_doc/docs/a.md", prompt)
        self.assertIn('projected_paths=["docs/a.md"]', prompt)
        self.assertNotIn("/host-only/artifacts/legacy.json", prompt)
        self.assertIn("path=/pal/references/plain", prompt)
        self.assertIn("reference:legacy", prompt)
        self.assertIn("reference:plain", prompt)


if __name__ == "__main__":
    unittest.main()
