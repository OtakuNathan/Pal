from __future__ import annotations

from pal.shared.tool_protocol import ToolCallIR, new_tool_call

import asyncio
import importlib.util
import shutil
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pal.artifact import (
    ArtifactHotState,
    ArtifactHotStateModel,
    ArtifactManager,
    ArtifactRecordModel,
    ArtifactRepository,
    ArtifactRepresentationModel,
    register_with_core as register_artifact_with_core,
)
from pal.artifact.tools import ArtifactContentSearchTool, ArtifactReadTool, ArtifactTranscribeTool
from pal.artifact.prompt import ArtifactPromptFragmentProvider
from pal.core import PalCore, register_with_core as register_core_with_core
from pal.core.prompt_compiler import PromptCompiler
from pal.execution import register_with_core as register_execution_with_core
from pal.foundation import ArtifactIngestor, EventEnvelope, PalV2Database
from pal.llm import EndpointResolver, LLMRuntime
from pal.llm.conversions import request_ir_from_prompt
from pal.llm.ir import WireShape
from pal.llm.shapes import codec_for_shape
from pal.llm.shapes.base import ShapeContext
from pal.mcp.plugin import McpManagerPluginProvider
from pal.shared import EventKind, PromptAssemblyContext, RuntimeStatus, SourceKind


class _TurnIO:
    def __init__(self, scope_key: str) -> None:
        self.scope_key = scope_key

    def artifact_scope_for_turn(self, turn_id: str | None) -> str | None:
        _ = turn_id
        return self.scope_key


class _ToolRuntime:
    def __init__(self, scope_key: str) -> None:
        self.provider_registry = {"core:turn_io": _TurnIO(scope_key)}


class _FakeTranscriber:
    def transcribe(self, path: Path, *, mime_type: str = "") -> str | None:
        _ = (path, mime_type)
        return "transcribed hello"


class ArtifactManagerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="pal_artifact_test_"))
        self.database = PalV2Database(self.root / "pal_artifact.sqlite3")
        self.database.initialize([ArtifactRecordModel, ArtifactRepresentationModel, ArtifactHotStateModel])
        self.repository = ArtifactRepository()
        self.manager = ArtifactManager(runtime_root=self.root, repository=self.repository)
        self.scope_key = "telegram:chat:42"
        self.turn_id = "turn-artifact-1"

    def tearDown(self) -> None:
        self.database.close()
        shutil.rmtree(self.root, ignore_errors=True)

    def _write_source(self, name: str, text: str = "refund terms are on page one") -> Path:
        path = self.root / "incoming" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def _register_text(self, name: str = "refund.txt", text: str = "refund terms are on page one"):
        return self.manager.register_ingested(
            self._write_source(name, text),
            scope_key=self.scope_key,
            turn_id=self.turn_id,
            source_channel="socket",
            metadata={"source_text": "please inspect the attached refund file"},
        )

    def _register_image(
        self,
        *,
        name: str = "photo.jpg",
        turn_id: str | None = None,
        source_metadata: dict[str, str] | None = None,
    ):
        path = self.root / "incoming" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"not really an image")
        return self.manager.register_ingested(
            {
                "local_cached_path": str(path),
                "file_name": name,
                "mime_type": "image/jpeg",
                "source_channel": "telegram",
                "source_metadata": dict(source_metadata or {}),
            },
            scope_key=self.scope_key,
            turn_id=turn_id or self.turn_id,
            source_channel="telegram",
        )

    def test_text_artifact_read_search_and_ttl_rules(self) -> None:
        ref = self._register_text()

        hot_before_search = self.repository.list_hot_states(scope_key=self.scope_key)[0]
        hits = self.manager.artifact_search(self.scope_key, query="refund", limit=5)
        hot_after_search = self.repository.get_hot_state(hot_before_search.hot_id)
        self.assertEqual([hit.artifact_id for hit in hits], [ref.artifact_id])
        self.assertEqual(hot_after_search.access_count, hot_before_search.access_count)

        self.manager.select(ref.artifact_id, self.scope_key)
        hot_after_select = self.repository.get_hot_state(hot_before_search.hot_id)
        self.assertGreater(hot_after_select.access_count, hot_after_search.access_count)

        read = self.manager.read(ref.artifact_id, self.scope_key)
        self.assertTrue(read.ok)
        self.assertIn("refund terms", read.text)

        content_hits = self.manager.content_search(ref.artifact_id, self.scope_key, query="terms")
        self.assertGreaterEqual(len(content_hits), 1)
        self.assertTrue(any("refund terms" in hit.text for hit in content_hits))

    def test_prompt_exposure_is_user_context_and_hides_local_paths(self) -> None:
        ref = self._register_text(name="invoice.txt", text="invoice total is 42")
        provider = ArtifactPromptFragmentProvider(service=self.manager)
        event = EventEnvelope(
            event_kind=EventKind.USER_MESSAGE,
            source_kind=SourceKind.CHANNEL,
            payload={"text": "看看这个附件"},
        )

        fragments = provider.build_prompt_fragments(
            PromptAssemblyContext(
                event=event,
                metadata={
                    "artifact_scope_key": self.scope_key,
                    "artifact_turn_id": self.turn_id,
                    "llm_capabilities": {"supports_vision": False},
                },
            )
        )

        self.assertEqual(len(fragments), 1)
        self.assertEqual(fragments[0].section, "artifact")
        self.assertIn(ref.artifact_id, fragments[0].content)
        self.assertIn("invoice.txt", fragments[0].content)
        self.assertNotIn(str(self.root), fragments[0].content)

        compiler = PromptCompiler(_PromptContext(_Registry(provider), self.manager))
        request = compiler.build_canonical_prompt(
            PromptAssemblyContext(
                event=event,
                metadata={
                    "artifact_scope_key": self.scope_key,
                    "artifact_turn_id": self.turn_id,
                    "llm_capabilities": {"supports_vision": False},
                },
            )
        )
        self.assertEqual(request.messages[-1].role.value, "user")
        text = request.messages[-1].text
        self.assertIn('<runtime_context_update kind="artifact">', text)
        self.assertIn("Available Artifacts", text)
        self.assertIn("看看这个附件", text)
        self.assertNotIn("<runtime_reminder", text)
        self.assertEqual(request.metadata["runtime_reminder_text"], "")

    def test_legacy_artifact_fragment_is_suppressed_when_active_l1_owns_input(self) -> None:
        self._register_text(name="active.txt", text="only once")
        provider = ArtifactPromptFragmentProvider(service=self.manager)
        fragments = provider.build_prompt_fragments(
            PromptAssemblyContext(
                event=EventEnvelope(
                    event_kind=EventKind.USER_MESSAGE,
                    source_kind=SourceKind.CHANNEL,
                    payload={"text": "inspect"},
                ),
                metadata={
                    "artifact_scope_key": self.scope_key,
                    "artifact_turn_id": self.turn_id,
                    "active_l1_owns_primary_input": True,
                },
            )
        )
        self.assertEqual(fragments, [])

    def test_artifact_info_exposes_local_file_metadata_for_tool_use(self) -> None:
        ref = self._register_text(name="invoice.txt", text="invoice total is 42")

        info = self.manager.info(ref.artifact_id, self.scope_key)
        local_file = info["artifact"]["metadata"]["local_file"]

        self.assertEqual(local_file["preferred_path_kind"], "normalized")
        self.assertTrue(Path(local_file["preferred_path"]).is_file())
        self.assertTrue(Path(local_file["original_path"]).is_file())
        self.assertTrue(Path(local_file["normalized_path"]).is_file())

        exposure = self.manager.select_prompt_exposure(
            self.scope_key,
            self.turn_id,
            "look at the attachment",
            {"supports_vision": False},
        )
        self.assertNotIn(str(self.root), exposure.text)

    def test_current_artifact_refs_prevent_empty_caption_from_falling_back_to_hot_history(self) -> None:
        old_ref = self._register_image(name="old_caption.jpg", turn_id="old-turn")
        current_ref = self._register_image(
            name="fresh.jpg",
            turn_id="current-turn",
            source_metadata={
                "telegram_file_path": "photos/fresh.jpg",
                "source_url": "https://api.telegram.org/file/botFAKE_TOKEN/photos/fresh.jpg",
            },
        )
        provider = ArtifactPromptFragmentProvider(service=self.manager)

        fragments = provider.build_prompt_fragments(
            PromptAssemblyContext(
                event=EventEnvelope(
                    event_kind=EventKind.USER_MESSAGE,
                    source_kind=SourceKind.CHANNEL,
                    payload={"text": "", "artifact_refs": [current_ref.to_dict()]},
                ),
                metadata={
                    "artifact_scope_key": self.scope_key,
                    "artifact_turn_id": "current-turn",
                    "llm_capabilities": {"supports_vision": False},
                },
            )
        )

        self.assertEqual(len(fragments), 1)
        content = fragments[0].content
        self.assertIn(current_ref.artifact_id, content)
        self.assertIn("fresh.jpg", content)
        self.assertNotIn(old_ref.artifact_id, content)
        self.assertNotIn("old_caption.jpg", content)
        self.assertIn("direct_content: unavailable", content)
        self.assertIn("current capability/tool surface", content)
        self.assertIn("local_file:", content)
        self.assertIn("preferred_path:", content)
        self.assertNotIn("source_url", content)
        self.assertNotIn("telegram_file_path", content)
        self.assertNotIn("FAKE_TOKEN", content)
        self.assertNotIn("ocr", content.lower())

    def test_empty_text_without_current_artifact_refs_does_not_expose_hot_artifacts(self) -> None:
        self._register_image(name="old.jpg", turn_id="old-turn")

        exposure = self.manager.select_prompt_exposure(
            self.scope_key,
            "later-turn",
            "",
            {"supports_vision": False},
        )

        self.assertEqual(exposure.text, "")
        self.assertEqual(exposure.inline_parts, ())

    def test_historical_reference_without_current_artifact_refs_does_not_auto_expose_hot_artifacts(self) -> None:
        ref = self._register_image(name="old.jpg", turn_id="old-turn")

        exposure = self.manager.select_prompt_exposure(
            self.scope_key,
            "later-turn",
            "that attachment",
            {"supports_vision": False},
        )

        self.assertEqual(exposure.text, "")
        self.assertEqual(exposure.inline_parts, ())

        hits = self.manager.artifact_search(self.scope_key, query="old", limit=5)
        self.assertEqual([hit.artifact_id for hit in hits], [ref.artifact_id])

    def test_current_image_artifact_refs_with_vision_still_attach_inline(self) -> None:
        ref = self._register_image(name="fresh.jpg", turn_id="current-turn")

        exposure = self.manager.select_prompt_exposure(
            self.scope_key,
            "current-turn",
            "",
            {"supports_vision": True},
            artifact_ids=(ref.artifact_id,),
        )

        self.assertEqual(len(exposure.inline_parts), 1)
        self.assertEqual(exposure.inline_parts[0].artifact_id, ref.artifact_id)
        self.assertIn("visual_content: attached_inline", exposure.text)

    def test_runtime_endpoint_capabilities_drive_image_inline_exposure(self) -> None:
        class _Settings:
            def __init__(self) -> None:
                self.think_levels: dict[str, str] = {}

            def get_active_llm_endpoint_id(self) -> str:
                return "vision"

            def get_think_level(self, endpoint_id: str) -> str | None:
                return self.think_levels.get(endpoint_id)

            def set_think_level(self, endpoint_id: str, level: str) -> None:
                self.think_levels[endpoint_id] = level

        endpoint = SimpleNamespace(
            endpoint_id="vision",
            provider="openrouter",
            model_id="vision-model",
            display_name="Vision Model",
            wire_shape="openai_response",
            base_url="https://example.test/v1",
            auth_kind="api_key_ref",
            credential_ref="TEST_API_KEY",
            context_window=8192,
            max_output_tokens=1024,
            thinking_levels_blob=["off"],
            default_thinking_level="off",
            supports_tools=True,
            supports_streaming=True,
            supports_vision=True,
            input_modalities_blob=["text", "image"],
            output_modalities_blob=["text"],
            priority=0,
            enabled=True,
            capabilities_blob={},
            notes=None,
        )
        runtime = LLMRuntime(
            endpoint_resolver=EndpointResolver(endpoints=(endpoint,)),
            settings_repository=_Settings(),
            endpoint_invoker=SimpleNamespace(),
            config=SimpleNamespace(runtime_root=self.root, llm_endpoint_retry_attempts=1),
        )
        ref = self._register_image(name="runtime-vision.jpg", turn_id="runtime-turn")

        exposure = self.manager.select_prompt_exposure(
            self.scope_key,
            "runtime-turn",
            "",
            runtime.resolve_endpoint_facts(),
            artifact_ids=(ref.artifact_id,),
        )

        self.assertEqual(len(exposure.inline_parts), 1)
        self.assertEqual(exposure.inline_parts[0].artifact_id, ref.artifact_id)
        self.assertIn("visual_content: attached_inline", exposure.text)
        self.assertNotIn("not directly attached or readable", exposure.text)

    def test_expired_artifact_retires_handler_and_reclaims_managed_files(self) -> None:
        ref = self._register_text()
        source_path = self.root / "incoming" / "refund.txt"
        record_before = self.repository.get_record(ref.artifact_id)
        self.assertIsNotNone(record_before)
        managed_root = Path(record_before.original_path).parents[1]
        self.assertTrue(managed_root.is_dir())
        hot = self.repository.list_hot_states(scope_key=self.scope_key)[0]
        expired = ArtifactHotState(
            hot_id=hot.hot_id,
            artifact_id=hot.artifact_id,
            scope_key=hot.scope_key,
            last_accessed_at=hot.last_accessed_at,
            expires_at="2000-01-01T00:00:00+00:00",
            hard_expires_at=hot.hard_expires_at,
            access_count=hot.access_count,
        )
        self.repository.upsert_hot_state(expired)

        exposure = self.manager.select_prompt_exposure(
            self.scope_key,
            self.turn_id,
            "刚才那个附件",
            {"supports_vision": False},
            artifact_ids=(ref.artifact_id,),
        )

        self.assertIn("artifact handlers have retired", exposure.text)
        self.assertIn(ref.artifact_id, exposure.text)
        self.assertIn("managed bytes and representations deleted", exposure.text)
        self.assertEqual(exposure.inline_parts, ())
        record_after = self.repository.get_record(ref.artifact_id)
        self.assertEqual(record_after.status, "retired")
        self.assertEqual(record_after.original_path, "")
        self.assertEqual(record_after.normalized_path, "")
        self.assertEqual(record_after.metadata["managed_cleanup"], "complete")
        self.assertFalse(managed_root.exists())
        self.assertTrue(source_path.is_file())
        self.assertEqual(self.repository.list_representations(ref.artifact_id), ())
        self.assertEqual(self.repository.list_hot_states(scope_key=self.scope_key), ())
        with self.assertRaisesRegex(KeyError, "artifact_handler_retired"):
            self.manager.read(ref.artifact_id, self.scope_key)

    def test_failed_managed_cleanup_stays_retired_and_is_retried(self) -> None:
        ref = self._register_text(name="retry-cleanup.txt")
        record_before = self.repository.get_record(ref.artifact_id)
        managed_root = Path(record_before.original_path).parents[1]
        hot = self.repository.list_hot_states(scope_key=self.scope_key)[0]
        self.repository.upsert_hot_state(
            ArtifactHotState(
                hot_id=hot.hot_id,
                artifact_id=hot.artifact_id,
                scope_key=hot.scope_key,
                last_accessed_at=hot.last_accessed_at,
                expires_at="2000-01-01T00:00:00+00:00",
                hard_expires_at=hot.hard_expires_at,
                access_count=hot.access_count,
            )
        )

        with patch("pal.artifact.service.shutil.rmtree", side_effect=PermissionError("busy")):
            first = self.manager.reap_expired(scope_key=self.scope_key)

        pending = self.repository.get_record(ref.artifact_id)
        self.assertEqual(first, {"retired": 1, "cleaned": 0, "pending": 1})
        self.assertEqual(pending.status, "retiring")
        self.assertEqual(pending.metadata["managed_cleanup"], "pending")
        self.assertTrue(managed_root.is_dir())

        second = self.manager.reap_expired(scope_key=self.scope_key)

        cleaned = self.repository.get_record(ref.artifact_id)
        self.assertEqual(second, {"retired": 0, "cleaned": 1, "pending": 0})
        self.assertEqual(cleaned.metadata["managed_cleanup"], "complete")
        self.assertFalse(managed_root.exists())

    def test_startup_recovery_migrates_legacy_pending_tombstone_once(self) -> None:
        ref = self._register_text(name="legacy-pending.txt")
        record = self.repository.get_record(ref.artifact_id)
        managed_root = Path(record.original_path).parents[1]
        self.repository.upsert_record(
            replace(
                record,
                status="retired",
                metadata={"managed_cleanup": "pending"},
                updated_at="",
            )
        )

        result = self.manager.recover_lifecycle()

        recovered = self.repository.get_record(ref.artifact_id)
        self.assertEqual(result["migrated"], 1)
        self.assertEqual(result["cleaned"], 1)
        self.assertEqual(recovered.status, "retired")
        self.assertEqual(recovered.metadata["managed_cleanup"], "complete")
        self.assertFalse(managed_root.exists())

    def test_missing_hot_state_is_reaped_as_a_retired_handler(self) -> None:
        ref = self._register_text(name="missing-handler.txt")
        record_before = self.repository.get_record(ref.artifact_id)
        managed_root = Path(record_before.original_path).parents[1]
        self.repository.delete_hot_states(ref.artifact_id)

        result = self.manager.reap_expired(scope_key=self.scope_key)

        record_after = self.repository.get_record(ref.artifact_id)
        self.assertEqual(result, {"retired": 1, "cleaned": 1, "pending": 0})
        self.assertEqual(record_after.status, "retired")
        self.assertEqual(record_after.metadata["retirement_reason"], "handler_missing")
        self.assertFalse(managed_root.exists())

    def test_pal_owned_source_cache_is_reclaimed_with_retired_handler(self) -> None:
        stored = ArtifactIngestor(self.root).store_bytes(
            channel_kind="telegram",
            bucket_id=self.turn_id,
            file_name="owned.txt",
            content=b"owned input",
            mime_type="text/plain",
        )
        source_path = Path(stored.local_cached_path)
        source_root = source_path.parent
        ref = self.manager.register_ingested(
            stored,
            scope_key=self.scope_key,
            turn_id=self.turn_id,
            source_channel="telegram",
        )
        hot = self.repository.list_hot_states(scope_key=self.scope_key)[0]
        self.repository.upsert_hot_state(
            ArtifactHotState(
                hot_id=hot.hot_id,
                artifact_id=hot.artifact_id,
                scope_key=hot.scope_key,
                last_accessed_at=hot.last_accessed_at,
                expires_at="2000-01-01T00:00:00+00:00",
                hard_expires_at=hot.hard_expires_at,
                access_count=hot.access_count,
            )
        )

        self.manager.reap_expired(scope_key=self.scope_key)

        self.assertFalse(source_root.exists())
        retired = self.repository.get_record(ref.artifact_id)
        self.assertEqual(retired.status, "retired")
        self.assertNotIn("_owned_source_root", retired.metadata)

    def test_artifact_ingestor_treats_provider_labels_as_path_components(self) -> None:
        stored = ArtifactIngestor(self.root).store_bytes(
            channel_kind="../../telegram",
            bucket_id="../42",
            file_name="../../escape.txt",
            content=b"contained",
            mime_type="text/plain",
        )

        stored_path = Path(stored.local_cached_path).resolve()
        self.assertTrue(stored_path.is_relative_to((self.root / "artifacts").resolve()))
        self.assertEqual(stored_path.name, "escape.txt")
        self.assertEqual(stored_path.read_bytes(), b"contained")
        self.assertFalse((self.root / "escape.txt").exists())

        unicode_stored = ArtifactIngestor(self.root).store_bytes(
            channel_kind="频道" * 100,
            bucket_id="会话" * 100,
            file_name=("图" * 300) + ".png",
            content=b"image",
            mime_type="image/png",
        )
        unicode_path = Path(unicode_stored.local_cached_path)
        self.assertTrue(unicode_path.is_file())
        self.assertLessEqual(len(unicode_path.name.encode("utf-8")), 180)

    def test_serialized_attachment_cannot_claim_source_ownership(self) -> None:
        stored = ArtifactIngestor(self.root).store_bytes(
            channel_kind="socket",
            bucket_id=self.turn_id,
            file_name="borrowed.txt",
            content=b"borrowed input",
            mime_type="text/plain",
        )
        source_path = Path(stored.local_cached_path)
        ref = self.manager.register_ingested(
            {
                **stored.__dict__,
                "owned_by_pal": True,
                "_owned_source_root": str(source_path.parent),
                "file_name": "borrowed.txt",
            },
            scope_key=self.scope_key,
            turn_id=self.turn_id,
            source_channel="socket",
        )
        registered = self.repository.get_record(ref.artifact_id)
        self.assertNotIn("_owned_source_root", registered.metadata)
        hot = self.repository.list_hot_states(scope_key=self.scope_key)[0]
        self.repository.upsert_hot_state(
            ArtifactHotState(
                hot_id=hot.hot_id,
                artifact_id=hot.artifact_id,
                scope_key=hot.scope_key,
                last_accessed_at=hot.last_accessed_at,
                expires_at="2000-01-01T00:00:00+00:00",
                hard_expires_at=hot.hard_expires_at,
                access_count=hot.access_count,
            )
        )

        self.manager.reap_expired(scope_key=self.scope_key)

        self.assertTrue(source_path.is_file())

    def test_orphaned_managed_tree_is_reclaimed_without_touching_known_tree(self) -> None:
        ref = self._register_text(name="known.txt")
        known = self.repository.get_record(ref.artifact_id)
        known_root = Path(known.original_path).parents[1]
        orphan_root = self.root / "artifacts" / "managed" / "orphan_scope" / "art_orphan"
        orphan_root.mkdir(parents=True)
        (orphan_root / "payload.bin").write_bytes(b"orphan")
        loose_file = self.root / "artifacts" / "managed" / "loose.bin"
        loose_file.write_bytes(b"orphan")

        removed = self.manager.reap_orphaned_managed_files()

        self.assertEqual(removed, 2)
        self.assertFalse(orphan_root.exists())
        self.assertFalse(loose_file.exists())
        self.assertTrue(known_root.is_dir())

    def test_orphan_reaper_unlinks_replaced_known_root_symlink_without_following_it(self) -> None:
        ref = self._register_text(name="known-symlink.txt")
        record = self.repository.get_record(ref.artifact_id)
        known_root = Path(record.original_path).parents[1]
        shutil.rmtree(known_root)
        outside = self.root / "outside-artifact"
        outside.mkdir()
        sentinel = outside / "keep.txt"
        sentinel.write_text("keep", encoding="utf-8")
        known_root.symlink_to(outside, target_is_directory=True)

        removed = self.manager.reap_orphaned_managed_files()

        self.assertEqual(removed, 1)
        self.assertFalse(known_root.exists())
        self.assertTrue(sentinel.is_file())

    def test_orphan_reaper_does_not_protect_completed_retired_tombstone_tree(self) -> None:
        ref = self._register_text(name="retired-remnant.txt")
        hot = self.repository.list_hot_states(scope_key=self.scope_key)[0]
        self.repository.upsert_hot_state(
            ArtifactHotState(
                hot_id=hot.hot_id,
                artifact_id=hot.artifact_id,
                scope_key=hot.scope_key,
                last_accessed_at=hot.last_accessed_at,
                expires_at="2000-01-01T00:00:00+00:00",
                hard_expires_at=hot.hard_expires_at,
                access_count=hot.access_count,
            )
        )
        self.manager.reap_expired(scope_key=self.scope_key)
        retired_root = self.root / "artifacts" / "managed" / self.scope_key.replace(":", "_") / ref.artifact_id
        retired_root.mkdir(parents=True)
        (retired_root / "stale.bin").write_bytes(b"stale")

        removed = self.manager.reap_orphaned_managed_files()

        self.assertEqual(removed, 1)
        self.assertFalse(retired_root.exists())

    def test_orphan_cleanup_io_failure_is_deferred_without_aborting_scan(self) -> None:
        orphan_root = self.root / "artifacts" / "managed" / "orphan_scope" / "art_orphan"
        orphan_root.mkdir(parents=True)
        (orphan_root / "payload.bin").write_bytes(b"orphan")

        with self.assertLogs("pal.artifact.service", level="WARNING"):
            with patch("pal.artifact.service.shutil.rmtree", side_effect=PermissionError("busy")):
                recovery = self.manager.recover_lifecycle()

        self.assertEqual(recovery["orphaned_removed"], 0)
        self.assertTrue(orphan_root.is_dir())

    def test_orphan_reaper_refuses_symlinked_managed_root(self) -> None:
        outside = self.root / "outside-managed"
        outside.mkdir()
        sentinel = outside / "keep.txt"
        sentinel.write_text("keep", encoding="utf-8")
        artifacts_root = self.root / "artifacts"
        artifacts_root.mkdir()
        (artifacts_root / "managed").symlink_to(outside, target_is_directory=True)

        with self.assertRaisesRegex(OSError, "symlinked managed artifact root"):
            self.manager.reap_orphaned_managed_files()

        self.assertTrue(sentinel.is_file())

    def test_ingest_does_not_create_managed_tree_before_record_is_published(self) -> None:
        source = self._write_source("db-failure.txt", "keep source")

        with patch.object(
            self.repository,
            "upsert_record",
            side_effect=RuntimeError("database unavailable"),
        ):
            with self.assertRaisesRegex(RuntimeError, "database unavailable"):
                self.manager.register_ingested(
                    source,
                    scope_key=self.scope_key,
                    turn_id=self.turn_id,
                    source_channel="socket",
                )

        managed_root = self.root / "artifacts" / "managed"
        self.assertFalse(managed_root.exists())
        self.assertTrue(source.is_file())

    async def test_retired_handler_tool_result_has_stable_reason(self) -> None:
        ref = self._register_text(name="retired-tool.txt")
        hot = self.repository.list_hot_states(scope_key=self.scope_key)[0]
        self.repository.upsert_hot_state(
            ArtifactHotState(
                hot_id=hot.hot_id,
                artifact_id=hot.artifact_id,
                scope_key=hot.scope_key,
                last_accessed_at=hot.last_accessed_at,
                expires_at="2000-01-01T00:00:00+00:00",
                hard_expires_at=hot.hard_expires_at,
                access_count=hot.access_count,
            )
        )

        result = await ArtifactReadTool(service=self.manager).ainvoke(
            {"artifact_id": ref.artifact_id},
            runtime=_ToolRuntime(self.scope_key),
            turn_id=self.turn_id,
        )

        self.assertEqual(result.status, RuntimeStatus.NOT_FOUND)
        self.assertEqual(result.structured["reason"], "artifact_handler_retired")

    def test_read_only_projection_tombstones_expired_explicit_artifact(self) -> None:
        ref = self._register_image(name="read-only-expired.jpg")
        hot = self.repository.list_hot_states(scope_key=self.scope_key)[0]
        self.repository.upsert_hot_state(
            ArtifactHotState(
                hot_id=hot.hot_id,
                artifact_id=hot.artifact_id,
                scope_key=hot.scope_key,
                last_accessed_at=hot.last_accessed_at,
                expires_at="2000-01-01T00:00:00+00:00",
                hard_expires_at=hot.hard_expires_at,
                access_count=hot.access_count,
            )
        )
        read_only_manager = ArtifactManager(
            runtime_root=self.root,
            repository=self.repository,
            writable=False,
        )

        exposure = read_only_manager.select_prompt_exposure(
            self.scope_key,
            self.turn_id,
            "inspect it",
            {"supports_vision": True},
            artifact_ids=(ref.artifact_id,),
        )

        self.assertIn("status: retired", exposure.text)
        self.assertEqual(exposure.inline_parts, ())
        self.assertEqual(self.repository.get_record(ref.artifact_id).status, "ready")

    def test_read_only_access_validates_but_does_not_refresh_hot_state(self) -> None:
        ref = self._register_text(name="read-only-live.txt")
        hot_before = self.repository.list_hot_states(scope_key=self.scope_key)[0]
        read_only_manager = ArtifactManager(
            runtime_root=self.root,
            repository=self.repository,
            writable=False,
        )

        info = read_only_manager.info(ref.artifact_id, self.scope_key)
        selected = read_only_manager.select(ref.artifact_id, self.scope_key)

        hot_after = self.repository.get_hot_state(hot_before.hot_id)
        self.assertEqual(info["artifact"]["artifact_id"], ref.artifact_id)
        self.assertFalse(selected["ttl_refreshed"])
        self.assertEqual(hot_after, hot_before)
        with self.assertRaisesRegex(RuntimeError, "artifact_manager_read_only"):
            read_only_manager.register_ingested(
                self.root / "does-not-matter.png",
                scope_key=self.scope_key,
                turn_id=self.turn_id,
                source_channel="bunshin",
            )

    async def test_read_only_content_search_reports_no_ttl_refresh(self) -> None:
        ref = self._register_text(name="read-only-search.txt", text="needle here")
        read_only_manager = ArtifactManager(
            runtime_root=self.root,
            repository=self.repository,
            writable=False,
        )

        result = await ArtifactContentSearchTool(service=read_only_manager).ainvoke(
            {"artifact_id": ref.artifact_id, "query": "needle"},
            runtime=_ToolRuntime(self.scope_key),
            turn_id=self.turn_id,
        )

        self.assertEqual(result.status, RuntimeStatus.OK)
        self.assertFalse(result.structured["ttl_refreshed"])

    async def test_audio_without_asr_returns_needs_transcription(self) -> None:
        audio_path = self.root / "incoming" / "voice.mp3"
        audio_path.parent.mkdir(parents=True, exist_ok=True)
        audio_path.write_bytes(b"not really mp3")
        ref = self.manager.register_ingested(
            {"local_cached_path": str(audio_path), "file_name": "voice.mp3", "mime_type": "audio/mpeg"},
            scope_key=self.scope_key,
            turn_id=self.turn_id,
            source_channel="telegram",
        )
        tool = ArtifactTranscribeTool(service=self.manager)

        result = await tool.ainvoke(
            {"artifact_id": ref.artifact_id},
            runtime=_ToolRuntime(self.scope_key),
            turn_id=self.turn_id,
        )

        self.assertEqual(result.status, "unsupported")
        self.assertEqual(result.structured["reason"], "needs_transcription")

    async def test_audio_with_asr_returns_existing_transcript(self) -> None:
        audio_path = self.root / "incoming" / "voice.wav"
        audio_path.parent.mkdir(parents=True, exist_ok=True)
        audio_path.write_bytes(b"not really wav")
        manager = ArtifactManager(
            runtime_root=self.root,
            repository=self.repository,
            transcriber=_FakeTranscriber(),
        )
        ref = manager.register_ingested(
            {"local_cached_path": str(audio_path), "file_name": "voice.wav", "mime_type": "audio/wav"},
            scope_key=self.scope_key,
            turn_id=self.turn_id,
            source_channel="telegram",
        )
        tool = ArtifactTranscribeTool(service=manager)

        result = await tool.ainvoke(
            {"artifact_id": ref.artifact_id},
            runtime=_ToolRuntime(self.scope_key),
            turn_id=self.turn_id,
        )

        self.assertEqual(result.status, "ok")
        self.assertIn("transcribed hello", result.text)

    @unittest.skipUnless(importlib.util.find_spec("PIL") is not None, "Pillow is not installed")
    def test_image_artifact_can_be_serialized_to_openai_chat_data_url(self) -> None:
        from PIL import Image

        image_path = self.root / "incoming" / "tiny.png"
        image_path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (8, 8), color=(10, 20, 30)).save(image_path)
        ref = self.manager.register_ingested(
            image_path,
            scope_key=self.scope_key,
            turn_id=self.turn_id,
            source_channel="socket",
        )
        exposure = self.manager.select_prompt_exposure(
            self.scope_key,
            self.turn_id,
            "",
            {"supports_vision": True},
        )

        self.assertEqual(len(exposure.inline_parts), 1)
        coerced = _openai_messages(
            [{"role": "user", "content": [{"type": "text", "text": "look"}, exposure.inline_parts[0].to_message_part()]}],
            artifact_manager=self.manager,
        )

        image_part = coerced[0]["content"][1]
        self.assertEqual(image_part["type"], "image_url")
        self.assertTrue(image_part["image_url"]["url"].startswith("data:image/"))
        self.assertEqual(exposure.inline_parts[0].artifact_id, ref.artifact_id)
        provider = ArtifactPromptFragmentProvider(service=self.manager)
        compiler = PromptCompiler(_PromptContext(_Registry(provider), self.manager))
        request = compiler.build_canonical_prompt(
            PromptAssemblyContext(
                event=EventEnvelope(
                    event_kind=EventKind.USER_MESSAGE,
                    source_kind=SourceKind.CHANNEL,
                    payload={"text": "what do you see?"},
                ),
                metadata={
                    "artifact_scope_key": self.scope_key,
                    "artifact_turn_id": self.turn_id,
                    "llm_capabilities": {"supports_vision": True},
                },
            )
        )

        self.assertEqual(len(request.messages), 1)
        merged_content = request.messages[0].parts
        merged_text = request.messages[0].text
        self.assertIn("Available Artifacts", merged_text)
        self.assertIn("visual_content: attached_inline", merged_text)
        self.assertIn("answer from vision directly", merged_text)
        self.assertIn("optional_tools: info", merged_text)
        self.assertNotIn("inspect_inline_image", merged_text)
        self.assertNotIn("actions: info, read, search", merged_text)
        self.assertIn("what do you see?", merged_text)
        self.assertEqual(merged_content[0].__class__.__name__, "ImagePartIR")

        merged_coerced = _encode_openai_request(request)
        self.assertTrue(any(part.get("type") == "image_url" for part in merged_coerced[0]["content"]))

        later_request = compiler.build_canonical_prompt(
            PromptAssemblyContext(
                event=EventEnvelope(
                    event_kind=EventKind.USER_MESSAGE,
                    source_kind=SourceKind.CHANNEL,
                    payload={"text": "try this artifact again"},
                ),
                metadata={
                    "artifact_scope_key": self.scope_key,
                    "artifact_turn_id": "later-turn",
                    "llm_capabilities": {"supports_vision": True},
                },
            )
        )
        later_text = later_request.messages[0].text
        self.assertIn("try this artifact again", later_text)
        self.assertNotIn("<runtime_reminder", later_text)
        self.assertEqual(later_request.metadata["runtime_reminder_text"], "")

        no_caption_request = compiler.build_canonical_prompt(
            PromptAssemblyContext(
                event=EventEnvelope(
                    event_kind=EventKind.USER_MESSAGE,
                    source_kind=SourceKind.CHANNEL,
                    payload={"text": ""},
                ),
                metadata={
                    "artifact_scope_key": self.scope_key,
                    "artifact_turn_id": self.turn_id,
                    "llm_capabilities": {"supports_vision": True},
                },
            )
        )
        no_caption_content = no_caption_request.messages[0].parts
        self.assertEqual(no_caption_content[0].__class__.__name__, "ImagePartIR")

    @unittest.skipUnless(importlib.util.find_spec("fitz") is not None, "PyMuPDF is not installed")
    def test_pdf_text_extraction_creates_page_representations(self) -> None:
        import fitz

        pdf_path = self.root / "incoming" / "policy.pdf"
        pdf_path.parent.mkdir(parents=True, exist_ok=True)
        doc = fitz.open()
        page = doc.new_page()
        page.insert_text((72, 72), "Refund policy lives here")
        doc.save(pdf_path)
        doc.close()

        ref = self.manager.register_ingested(pdf_path, scope_key=self.scope_key, turn_id=self.turn_id, source_channel="socket")
        info = self.manager.info(ref.artifact_id, self.scope_key)
        kinds = {item["representation_kind"] for item in info["representations"]}

        self.assertIn("page_text", kinds)
        self.assertIn("chunk_text", kinds)

    async def test_artifact_tools_are_discoverable_and_callable_when_not_resident(self) -> None:
        core = PalCore()
        register_core_with_core(core)
        register_execution_with_core(core.context)
        register_artifact_with_core(core.context, self.manager)
        core.publish_module_capabilities("execution")
        core.publish_module_capabilities("artifact")
        core.context.execution_runtime.register_provider_ref("core:turn_io", _TurnIO(self.scope_key))
        ref = self._register_text("refund-policy.txt", "refund terms are on page one")

        contracts = {
            contract["function"]["name"]: contract["function"]["input_schema"]
            for contract in core.tool_surface.build_llm_tool_contracts()
        }

        self.assertNotIn("list_artifacts", contracts)
        search = await core.context.execution_runtime.execute_tool_async(
            new_tool_call(name="search_tools", args={"query": "artifact refund", "module_name": "artifact", "top_k": 10}),
            turn_id=self.turn_id,
        )
        self.assertTrue(search.ok, search.text)
        hit_names = {hit["alias"] for hit in search.structured["hits"]}
        self.assertIn("search_artifacts", hit_names)
        self.assertIn("read_artifact", hit_names)

        read_tool = await core.context.execution_runtime.execute_tool_async(
            new_tool_call(name="read_tool", args={"name": "read_artifact"}),
            turn_id=self.turn_id,
        )
        self.assertTrue(read_tool.ok, read_tool.text)
        schema = read_tool.structured["input_schema"]
        self.assertIn("artifact_id", schema["properties"])
        self.assertIn("representation", schema["properties"])

        called = await core.context.execution_runtime.execute_tool_async(
            new_tool_call(name="call_tool", args={"name": "read_artifact", "args": {"artifact_id": ref.artifact_id}}),
            turn_id=self.turn_id,
        )
        self.assertEqual(called.status, RuntimeStatus.OK, called.text)
        self.assertIn("refund terms", called.text)

    @unittest.skipUnless(importlib.util.find_spec("PIL") is not None, "Pillow is not installed")
    def test_remote_image_source_is_replaced_by_normalized_local_data_url(self) -> None:
        from PIL import Image

        image_path = self.root / "incoming" / "photo.jpg"
        image_path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (8, 8), color=(10, 20, 30)).save(image_path)

        ref = self.manager.register_ingested(
            {
                "local_cached_path": str(image_path),
                "file_name": "photo.jpg",
                "mime_type": "image/jpeg",
                "source_channel": "telegram",
                "source_metadata": {
                    "telegram_file_path": "photos/file_123.jpg",
                    "source_url": "https://api.telegram.org/file/botFAKE_TOKEN/photos/file_123.jpg",
                },
            },
            scope_key=self.scope_key,
            turn_id=self.turn_id,
            source_channel="telegram",
        )

        exposure = self.manager.select_prompt_exposure(
            self.scope_key, self.turn_id, "",
            {"supports_vision": True},
        )
        self.assertEqual(len(exposure.inline_parts), 1)
        self.assertTrue(exposure.inline_parts[0].source_url.startswith("data:image/jpeg;base64,"))
        self.assertNotIn("FAKE_TOKEN", exposure.inline_parts[0].source_url)

        part_dict = exposure.inline_parts[0].to_message_part()
        self.assertTrue(part_dict["source_url"].startswith("data:image/jpeg;base64,"))
        part_dict["source_url"] = "https://api.telegram.org/file/botFAKE_TOKEN/photos/file_123.jpg"

        coerced = _openai_messages(
            [{"role": "user", "content": [
                {"type": "text", "text": "look"},
                part_dict,
            ]}],
            artifact_manager=self.manager,
        )
        image_part = coerced[0]["content"][1]
        self.assertEqual(image_part["type"], "image_url")
        self.assertTrue(image_part["image_url"]["url"].startswith("data:image/jpeg;base64,"))
        self.assertNotIn("FAKE_TOKEN", image_part["image_url"]["url"])

        # Verify source_url (containing bot token) does NOT appear in LLM-visible text
        info = self.manager.info(ref.artifact_id, self.scope_key)
        info_str = str(info)
        self.assertNotIn("FAKE_TOKEN", info_str)
        self.assertNotIn("source_url", info["artifact"]["metadata"].get("source_metadata", {}))
        self.assertNotIn("source_url", exposure.text)

        mcp_provider = McpManagerPluginProvider(
            runtime_root=self.root,
            core_context=SimpleNamespace(
                port_registry={"artifact:artifact": self.manager},
                execution_runtime=_ToolRuntime(self.scope_key),
            ),
        )
        prepared = mcp_provider._prepare_image_payload(
            {"artifact_id": ref.artifact_id, "mode": "url"},
            turn_id=self.turn_id,
        )
        self.assertEqual(prepared["kind"], "data_url")
        self.assertTrue(prepared["data_url"].startswith("data:image/jpeg;base64,"))
        self.assertNotIn("FAKE_TOKEN", str(prepared))

    @unittest.skipUnless(importlib.util.find_spec("PIL") is not None, "Pillow is not installed")
    def test_image_without_source_url_falls_back_to_base64(self) -> None:
        from PIL import Image

        image_path = self.root / "incoming" / "local.png"
        image_path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (8, 8)).save(image_path)

        self.manager.register_ingested(
            image_path,
            scope_key=self.scope_key,
            turn_id=self.turn_id,
            source_channel="socket",
        )

        exposure = self.manager.select_prompt_exposure(
            self.scope_key, self.turn_id, "",
            {"supports_vision": True},
        )
        self.assertEqual(len(exposure.inline_parts), 1)
        self.assertTrue(exposure.inline_parts[0].source_url.startswith("data:image/png;base64,"))

        with patch.object(self.manager, "to_data_url", wraps=self.manager.to_data_url) as resolver:
            coerced = _openai_messages(
                [{"role": "user", "content": [
                    {"type": "text", "text": "look"},
                    exposure.inline_parts[0].to_message_part(),
                ]}],
                artifact_manager=self.manager,
            )
        resolver.assert_not_called()
        image_part = coerced[0]["content"][1]
        self.assertTrue(image_part["image_url"]["url"].startswith("data:image/"))


class _Registry:
    def __init__(self, *providers) -> None:
        self.providers = providers

    def list_for_prompt(self):
        return list(self.providers)


class _Ports:
    def __init__(self, artifact_manager: ArtifactManager) -> None:
        self.artifact_manager = artifact_manager

    def get(self, key: str):
        return self.artifact_manager if key == "artifact:artifact" else None


class _PromptContext:
    def __init__(self, registry: _Registry, artifact_manager: ArtifactManager) -> None:
        self.prompt_fragment_registry = registry
        self.port_registry = _Ports(artifact_manager)


def _openai_messages(messages, *, artifact_manager: ArtifactManager):
    compiler = PromptCompiler(_PromptContext(_Registry(), artifact_manager))
    request = request_ir_from_prompt(
        messages=compiler._resolve_artifact_images(messages),
        max_output_tokens=1024,
    )
    return _encode_openai_request(request)


def _encode_openai_request(request):
    context = ShapeContext(
        wire_shape=WireShape.OPENAI_COMPLETION,
        endpoint_id="test",
        model_id="test-model",
    )
    payload = codec_for_shape(WireShape.OPENAI_COMPLETION).encode(request, context).payload
    return payload["messages"]


if __name__ == "__main__":
    unittest.main()
