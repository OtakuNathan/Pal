"""Verifier corpus for bound-input advertisement in the role prompt.

Adversarial cases derived from the prompt_adapter contract and the
input_binding edge that produces the entries: a bound entry is advertised
only at the exact in-workspace location the binding stage recorded
(``inputs/<name>/<repo_path>`` or the repository-relative path), every other
bound shape stays hidden with no host-path leak and no cross-entry path
inheritance, and rendering stays total.
"""

from __future__ import annotations

import unittest

from pal.bunshin.prompt_adapter import render_bunshin_task_prompt
from pal.shared import BunshinInvocationPack


def _pack_with_references(references: list[dict]) -> BunshinInvocationPack:
    return BunshinInvocationPack(
        invocation_id="inv-prompt-adapter-verifier",
        goal="inspect",
        workspace={"reference_paths": references},
    )


def _reference_line(prompt: str, name: str) -> str:
    head, _, tail = prompt.partition(f"reference:{name}:")
    assert tail, f"reference:{name} missing from prompt"
    return tail.splitlines()[0]


class BoundEntryIsolationTests(unittest.TestCase):
    def test_hidden_bound_entry_never_inherits_previous_entry_path(self) -> None:
        """A hidden bound entry must not reuse the prior entry's location.

        The renderer resolves each entry's visible path independently; a
        hidden (legacy) bound entry following an advertised one must render
        with no path and no read_file_args of its own or of any sibling.
        """
        pack = _pack_with_references(
            [
                {
                    "name": "spec_doc",
                    "path": "/runtime/workspaces/ws-1/inputs/spec_doc/docs/spec.md",
                    "include": ["docs/spec.md"],
                    "truth_source": True,
                    "bound_input": True,
                },
                {
                    "name": "legacy",
                    "path": "/host-only/artifacts/legacy.json",
                    "bound_input": True,
                    "truth_source": True,
                },
            ]
        )

        prompt = render_bunshin_task_prompt(pack)

        legacy_line = _reference_line(prompt, "legacy")
        self.assertNotIn("path=", legacy_line)
        self.assertNotIn("read_file_args=", legacy_line)
        self.assertNotIn("/host-only/artifacts/legacy.json", prompt)
        self.assertIn(
            "path=/runtime/workspaces/ws-1/inputs/spec_doc/docs/spec.md", prompt
        )

    def test_hidden_bound_entry_after_plain_entry_leaks_no_path(self) -> None:
        """A bound entry whose path is not its recorded location stays hidden
        even when an earlier non-bound entry was advertised."""
        pack = _pack_with_references(
            [
                {
                    "name": "plain",
                    "path": "/pal/references/plain",
                    "include": ["task.yaml"],
                },
                {
                    "name": "mismatched",
                    "path": "/runtime/other/root.json",
                    "include": ["docs/spec.md"],
                    "bound_input": True,
                },
            ]
        )

        prompt = render_bunshin_task_prompt(pack)

        mismatched_line = _reference_line(prompt, "mismatched")
        self.assertNotIn("path=", mismatched_line)
        self.assertNotIn("read_file_args=", mismatched_line)
        self.assertNotIn("/runtime/other/root.json", prompt)
        self.assertNotIn("/pal/references/mismatched", prompt)
        self.assertIn("path=/pal/references/plain", prompt)


class BoundEntryVisibilityBoundaryTests(unittest.TestCase):
    def test_multi_include_bound_entry_stays_hidden(self) -> None:
        """A bound input is one deterministic file; several includes cannot
        name one location, so nothing is advertised."""
        pack = _pack_with_references(
            [
                {
                    "name": "multi",
                    "path": "/runtime/workspaces/ws-1/inputs/multi/docs",
                    "include": ["docs/a.md", "docs/b.md"],
                    "bound_input": True,
                }
            ]
        )

        prompt = render_bunshin_task_prompt(pack)

        line = _reference_line(prompt, "multi")
        self.assertNotIn("path=", line)
        self.assertNotIn("read_file_args=", line)
        self.assertNotIn("/runtime/workspaces/ws-1/inputs/multi/docs", prompt)
        self.assertIn('projected_paths=["docs/a.md","docs/b.md"]', prompt)

    def test_bound_entry_without_include_stays_hidden(self) -> None:
        """Without a recorded repo path the location cannot be verified, so
        the legacy gateway shape renders with no path at all."""
        pack = _pack_with_references(
            [
                {
                    "name": "unlocated",
                    "path": "/host-only/artifacts/unlocated.json",
                    "bound_input": True,
                    "truth_source": True,
                }
            ]
        )

        prompt = render_bunshin_task_prompt(pack)

        line = _reference_line(prompt, "unlocated")
        self.assertNotIn("path=", line)
        self.assertNotIn("/host-only/artifacts/unlocated.json", prompt)
        self.assertNotIn("/pal/references/unlocated", prompt)

    def test_near_miss_suffix_is_hidden(self) -> None:
        """A path ending with a sibling of the repo path (spec.md.bak, or a
        longer directory overlap) is not the deterministic location."""
        pack = _pack_with_references(
            [
                {
                    "name": "near_miss",
                    "path": "/runtime/workspaces/ws-1/inputs/near_miss/docs/spec.md.bak",
                    "include": ["docs/spec.md"],
                    "bound_input": True,
                },
                {
                    "name": "short_tail",
                    "path": "/runtime/workspaces/ws-1/inputs/short_miss/spec.md",
                    "include": ["docs/spec.md"],
                    "bound_input": True,
                },
            ]
        )

        prompt = render_bunshin_task_prompt(pack)

        for name in ("near_miss", "short_tail"):
            line = _reference_line(prompt, name)
            self.assertNotIn("path=", line, name)
        self.assertNotIn("spec.md.bak", prompt)
        self.assertNotIn("inputs/short_miss/spec.md", prompt)

    def test_exact_repo_relative_path_is_advertised_verbatim(self) -> None:
        """A repository-including workspace records the repo-relative path
        itself; it is advertised exactly, with read_file_args."""
        pack = _pack_with_references(
            [
                {
                    "name": "contract",
                    "path": "docs/contract.md",
                    "include": ["docs/contract.md"],
                    "truth_source": True,
                    "bound_input": True,
                }
            ]
        )

        prompt = render_bunshin_task_prompt(pack)

        self.assertIn(
            "- reference:contract: read-only semantic input; "
            "access=ordinary file/search tools; truth_source=True; "
            "path=docs/contract.md; "
            'read_file_args={"file_path":"docs/contract.md"}; '
            'projected_paths=["docs/contract.md"]',
            prompt,
        )


if __name__ == "__main__":
    unittest.main()
