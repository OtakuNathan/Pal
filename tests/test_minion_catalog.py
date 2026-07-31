from __future__ import annotations

import asyncio
import shutil
import tempfile
import unittest
from pathlib import Path

from pal.execution.contracts import CapabilityCall
from pal.execution.generated_tool_models import (
    MinionV2CapabilitiesMinionV2PublicProviderSetFamilyOverrideInput,
)
from pal.minion.capabilities import MinionManagerProvider
from pal.minion.catalog import MinionCatalogService
from pal.minion.catalog_store import family_override_path, profile_override_path
from pal.minion.families import MinionFamilyRegistry
from pal.minion.manager import MinionManager
from pal.minion.profiles import MinionProfileRegistry
from pal.minion.v2.capabilities import MinionV2PublicProvider
from pal.shared import IntrospectionCall, RuntimeStatus


class MinionCatalogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="pal_minion_catalog_"))

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def test_builtin_catalog_loads_from_package_without_runtime_seeds(self) -> None:
        snapshot = MinionCatalogService(self.root).bootstrap()

        self.assertFalse((self.root / "plugins" / "minion" / "profiles").exists())
        self.assertFalse((self.root / "plugins" / "minion" / "families").exists())
        generic = next(item for item in snapshot["profiles"] if item["name"] == "generic")
        software = next(item for item in snapshot["families"] if item["name"] == "software_engineering")
        self.assertEqual(generic["source"], "builtin")
        self.assertEqual(software["source"], "builtin")
        self.assertIn("v2_candidate_builder", generic["capability_groups"])

    def test_sidecar_bootstrap_ignores_legacy_plugin_seed_catalog(self) -> None:
        legacy_root = self.root / "plugins" / "minion" / "profiles"
        builtin = legacy_root / "generic.toml"
        builtin.parent.mkdir(parents=True)
        builtin.write_text(
            'profile_id = "generic"\nprofile_group = "general"\ndisplay_name = "Old Seed"\n[metadata]\nbuiltin = true\n',
            encoding="utf-8",
        )
        custom = legacy_root / "general" / "custom_writer.toml"
        custom.parent.mkdir(parents=True)
        custom.write_text(
            'profile_id = "custom_writer"\ndisplay_name = "Custom Writer"\ncapability_groups = ["core_minion_read"]\n',
            encoding="utf-8",
        )

        snapshot = MinionCatalogService(self.root).bootstrap()

        self.assertTrue(builtin.exists())
        self.assertTrue(custom.exists())
        self.assertNotIn("migration", snapshot)
        self.assertNotEqual(MinionProfileRegistry(runtime_root=self.root).get("generic").display_name, "Old Seed")
        self.assertIsNone(MinionProfileRegistry(runtime_root=self.root).get("custom_writer"))
        self.assertFalse(profile_override_path(self.root, "general", "custom_writer").is_file())

    def test_profile_override_is_atomic_versioned_and_resettable(self) -> None:
        service = MinionCatalogService(self.root)
        initial = service.bootstrap()

        updated = service.set_profile_override(
            profile="software_engineering.v2_coder",
            changes={"metadata": {"max_output_tokens": 65536}, "display_name": "Strict Coder"},
            if_generation=initial["generation"],
            actor="nathan",
        )

        self.assertNotEqual(updated["generation"], initial["generation"])
        effective = MinionProfileRegistry(runtime_root=self.root).get("software_engineering.v2_coder")
        self.assertEqual(effective.display_name, "Strict Coder")
        self.assertEqual(effective.metadata["max_output_tokens"], 65536)
        self.assertFalse(effective.metadata["builtin"])
        self.assertEqual(effective.metadata["override_of"], "software_engineering.v2_coder")
        with self.assertRaisesRegex(ValueError, "catalog changed"):
            service.set_profile_override(
                profile="software_engineering.v2_coder",
                changes={"display_name": "Stale Update"},
                if_generation=initial["generation"],
            )

        reset = service.reset_profile_override(profile="software_engineering.v2_coder", actor="nathan")

        self.assertEqual(reset["status"], "reset")
        self.assertEqual(
            MinionProfileRegistry(runtime_root=self.root).get("software_engineering.v2_coder").display_name,
            "V2 Contract Coder",
        )
        self.assertFalse(profile_override_path(self.root, "software_engineering", "v2_coder").exists())

    def test_profile_override_rejects_unknown_capability_group(self) -> None:
        service = MinionCatalogService(self.root)
        service.bootstrap()

        with self.assertRaisesRegex(ValueError, "unknown capability groups"):
            service.set_profile_override(
                profile="generic",
                changes={"capability_groups": ["made_up_privilege"]},
            )
        with self.assertRaisesRegex(ValueError, "unknown profile override fields"):
            service.set_profile_override(
                profile="generic",
                changes={"max_output_token_typo": 99999},
            )

    def test_family_override_validates_roles_and_restores_builtin(self) -> None:
        service = MinionCatalogService(self.root)
        service.bootstrap()

        updated = service.set_family_override(
            family="software_engineering",
            changes={"display_name": "Strict Software Engineering"},
            actor="nathan",
        )

        self.assertEqual(updated["definition"]["display_name"], "Strict Software Engineering")
        self.assertTrue(family_override_path(self.root, "software_engineering").is_file())
        self.assertEqual(
            MinionFamilyRegistry(runtime_root=self.root).get("software_engineering").display_name,
            "Strict Software Engineering",
        )
        with self.assertRaisesRegex(ValueError, "unknown profiles"):
            service.set_family_override(
                family="software_engineering",
                changes={
                    "role_bindings": {
                        "implementation": {
                            "executor": "profile",
                            "profile": "software_engineering.missing_coder",
                        }
                    }
                },
            )
        with self.assertRaisesRegex(ValueError, "missing architect"):
            service.set_family_override(
                family="software_engineering",
                changes={"role_bindings": {"architect": None}},
            )
        service.reset_family_override(family="software_engineering")
        self.assertFalse(family_override_path(self.root, "software_engineering").exists())

    def test_family_override_tool_uses_only_the_executor_binding_contract(self) -> None:
        parsed = (
            MinionV2CapabilitiesMinionV2PublicProviderSetFamilyOverrideInput.model_validate(
                {
                    "family": "lifestyle",
                    "changes": {
                        "role_bindings": {
                            "implementation": {
                                "executor": "null",
                                "profile": None,
                                "reason": "external_human_execution",
                            }
                        }
                    },
                }
            )
        )
        self.assertEqual(
            parsed.changes.role_bindings["implementation"].executor,
            "null",
        )
        strategy = (
            MinionV2CapabilitiesMinionV2PublicProviderSetFamilyOverrideInput.model_validate(
                {
                    "family": "lifestyle",
                    "changes": {
                        "execution_adapter": "artifact_bundle.v2",
                    },
                }
            )
        )
        self.assertEqual(
            strategy.changes.execution_adapter,
            "artifact_bundle.v2",
        )
        with self.assertRaisesRegex(ValueError, "extra"):
            MinionV2CapabilitiesMinionV2PublicProviderSetFamilyOverrideInput.model_validate(
                {
                    "family": "lifestyle",
                    "changes": {
                        "builders": {"contract": "contract_sketch.v2"}
                    },
                }
            )

    def test_family_override_rejects_unexecutable_role_combinations(self) -> None:
        service = MinionCatalogService(self.root)
        service.bootstrap()
        with self.assertRaisesRegex(
            ValueError,
            "reviewer requires a profile executor",
        ):
            service.set_family_override(
                family="lifestyle",
                changes={
                    "role_bindings": {
                        "reviewer": {
                            "executor": "null",
                            "profile": None,
                            "reason": "missing_reviewer",
                        }
                    }
                },
            )
        with self.assertRaisesRegex(
            ValueError,
            "implementation and verifier executors must both",
        ):
            service.set_family_override(
                family="software_engineering",
                changes={
                    "role_bindings": {
                        "verifier": {
                            "executor": "null",
                            "profile": None,
                            "reason": "missing_verifier",
                        }
                    }
                },
            )
        updated = service.set_family_override(
            family="software_engineering",
            changes={
                "role_bindings": {
                    role: {
                        "executor": "null",
                        "profile": None,
                        "reason": "external_human_execution",
                    }
                    for role in ("implementation", "verifier")
                }
            },
        )
        self.assertEqual(
            updated["definition"]["role_bindings"]["implementation"],
            {
                "executor": "null",
                "reason": "external_human_execution",
            },
        )

    def test_manager_owns_catalog_rpc(self) -> None:
        manager = MinionManager(self.root)

        before = asyncio.run(manager._call_method("catalog_snapshot", {"kind": "profiles"}))
        updated = asyncio.run(
            manager._call_method(
                "catalog_set_profile_override",
                {
                    "profile": "generic",
                    "changes": {"display_name": "Manager Owned"},
                    "actor": "nathan",
                },
            )
        )
        after = asyncio.run(manager._call_method("catalog_snapshot", {"kind": "profiles"}))

        self.assertNotEqual(before["generation"], after["generation"])
        self.assertEqual(updated["definition"]["display_name"], "Manager Owned")
        self.assertEqual(manager.health()["catalog_generation"], after["generation"])

    def test_plugin_attach_starts_sidecar_without_importing_legacy_catalog(self) -> None:
        legacy = self.root / "plugins" / "minion" / "profiles" / "generic.toml"
        legacy.parent.mkdir(parents=True)
        legacy.write_text(
            'profile_id = "generic"\nprofile_group = "general"\ndisplay_name = "Stale Generic"\n[metadata]\nbuiltin = true\n',
            encoding="utf-8",
        )
        provider = MinionManagerProvider(self.root)
        try:
            health = provider.attach_manager()
            snapshot = provider.client.catalog_snapshot_sync(kind="profiles")
        finally:
            provider.detach_manager()

        self.assertTrue(health["ok"])
        self.assertTrue(legacy.exists())
        generic = next(item for item in snapshot["profiles"] if item["name"] == "generic")
        self.assertEqual(generic["source"], "builtin")
        self.assertEqual(generic["display_name"], "Generic Minion")
        self.assertIn("v2_candidate_builder", generic["capability_groups"])

    def test_public_catalog_capabilities_are_only_sidecar_proxies(self) -> None:
        requests: list[tuple[str, dict[str, object]]] = []

        def request(method: str, params: dict[str, object] | None = None) -> dict[str, object]:
            requests.append((method, dict(params or {})))
            return {"status": "ok", "generation": "catalog-v2", "profiles": []}

        provider = MinionV2PublicProvider(runtime_root=self.root, manager_request=request)
        meta = {"actor_id": "nathan", "channel_id": "socket:test"}

        read = provider.read_catalog(
            IntrospectionCall(name="intro_minion_catalog_read", meta=meta, args={"kind": "profiles"})
        )
        changed = provider.set_profile_override(
            CapabilityCall(
                name="op_minion_catalog_set_profile_override",
                meta=meta,
                args={"profile": "generic", "changes": {"display_name": "Focused"}},
            )
        )
        refreshed = provider.refresh_catalog(
            CapabilityCall(name="op_minion_catalog_refresh", meta=meta, args={})
        )

        self.assertEqual(read.status, RuntimeStatus.OK)
        self.assertEqual(changed.status, RuntimeStatus.OK)
        self.assertEqual(refreshed.status, RuntimeStatus.OK)
        self.assertEqual(
            [item[0] for item in requests],
            ["catalog_snapshot", "catalog_set_profile_override", "catalog_refresh"],
        )
        self.assertEqual(requests[1][1]["actor"], "nathan")


if __name__ == "__main__":
    unittest.main()
