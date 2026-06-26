from __future__ import annotations

import asyncio
import importlib.util
import shutil
import tempfile
import unittest
from pathlib import Path

from pal.artifact import (
    ArtifactHotState,
    ArtifactHotStateModel,
    ArtifactManager,
    ArtifactRecordModel,
    ArtifactRepository,
    ArtifactRepresentationModel,
    register_with_core as register_artifact_with_core,
)
from pal.artifact.tools import ArtifactTranscribeTool
from pal.artifact.prompt import ArtifactPromptFragmentProvider
from pal.core import PalCore, register_with_core as register_core_with_core
from pal.core.prompt_compiler import PromptCompiler
from pal.execution import register_with_core as register_execution_with_core
from pal.foundation import EventEnvelope, PalV2Database
from pal.llm.contracts import CanonicalToolCall
from pal.llm.runtime import _coerce_messages_for_openai_chat
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

        compiler = PromptCompiler(type("Context", (), {"prompt_fragment_registry": _Registry(provider)})())
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
        self.assertEqual(request.messages[-1]["role"], "user")
        self.assertIsInstance(request.messages[-1]["content"], list)
        text_parts = [
            str(part.get("text") or "")
            for part in request.messages[-1]["content"]
            if isinstance(part, dict) and part.get("type") == "text"
        ]
        self.assertIn('<runtime_context_update kind="artifact">', text_parts[0])
        self.assertIn("Available Artifacts", text_parts[1])
        self.assertEqual(text_parts[-2], "看看这个附件")
        self.assertIn("<runtime_reminder", text_parts[-1])

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

    def test_expired_artifact_is_not_exposed_by_default(self) -> None:
        self._register_text()
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
        )

        self.assertEqual(exposure.text, "")
        self.assertEqual(exposure.inline_parts, ())

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
        coerced = _coerce_messages_for_openai_chat(
            [{"role": "user", "content": [{"type": "text", "text": "look"}, exposure.inline_parts[0].to_message_part()]}],
            artifact_manager=self.manager,
            supports_vision=True,
        )

        image_part = coerced[0]["content"][1]
        self.assertEqual(image_part["type"], "image_url")
        self.assertTrue(image_part["image_url"]["url"].startswith("data:image/"))
        self.assertEqual(exposure.inline_parts[0].artifact_id, ref.artifact_id)
        raw_base64_coerced = _coerce_messages_for_openai_chat(
            [{"role": "user", "content": [{"type": "text", "text": "look"}, exposure.inline_parts[0].to_message_part()]}],
            artifact_manager=self.manager,
            supports_vision=True,
            image_url_format="raw_base64",
        )
        raw_url = raw_base64_coerced[0]["content"][1]["image_url"]["url"]
        self.assertFalse(raw_url.startswith("data:image/"))
        self.assertNotIn(",", raw_url)

        provider = ArtifactPromptFragmentProvider(service=self.manager)
        compiler = PromptCompiler(type("Context", (), {"prompt_fragment_registry": _Registry(provider)})())
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
        merged_content = request.messages[0]["content"]
        self.assertIsInstance(merged_content, list)
        merged_text = "\n".join(str(part.get("text") or "") for part in merged_content if isinstance(part, dict))
        self.assertIn("Available Artifacts", merged_text)
        self.assertIn("visual_content: attached_inline", merged_text)
        self.assertIn("answer from vision directly", merged_text)
        self.assertIn("optional_tools: info", merged_text)
        self.assertNotIn("inspect_inline_image", merged_text)
        self.assertNotIn("actions: info, read, search", merged_text)
        self.assertIn("what do you see?", merged_text)
        self.assertEqual(merged_content[0]["type"], "artifact_image")
        self.assertTrue(any(part.get("type") == "artifact_image" for part in merged_content if isinstance(part, dict)))

        merged_coerced = _coerce_messages_for_openai_chat(
            request.messages,
            artifact_manager=self.manager,
            supports_vision=True,
        )
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
        later_content = later_request.messages[0]["content"]
        self.assertIsInstance(later_content, list)
        later_text_parts = [
            str(part.get("text") or "")
            for part in later_content
            if isinstance(part, dict) and part.get("type") == "text"
        ]
        self.assertEqual(later_text_parts[-2], "try this artifact again")
        self.assertIn("<runtime_reminder", later_text_parts[-1])

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
        no_caption_content = no_caption_request.messages[0]["content"]
        self.assertIsInstance(no_caption_content, list)
        self.assertEqual(no_caption_content[0]["type"], "artifact_image")

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
        core.publish_module_capabilities("artifact")
        core.context.execution_runtime.register_provider_ref("core:turn_io", _TurnIO(self.scope_key))
        ref = self._register_text("refund-policy.txt", "refund terms are on page one")

        contracts = {
            contract["function"]["name"]: contract["function"]["parameters"]
            for contract in core.tool_surface.build_llm_tool_contracts()
        }

        self.assertNotIn("list_artifacts", contracts)
        search = await core.context.execution_runtime.execute_tool_async(
            CanonicalToolCall(name="search_tools", args={"query": "artifact refund", "module_id": "artifact", "top_k": 10}),
            turn_id=self.turn_id,
        )
        self.assertTrue(search.ok, search.text)
        hit_names = {hit["name"] for hit in search.structured["hits"]}
        self.assertIn("search_artifacts", hit_names)
        self.assertIn("read_artifact", hit_names)

        read_tool = await core.context.execution_runtime.execute_tool_async(
            CanonicalToolCall(name="read_tool", args={"name": "read_artifact"}),
            turn_id=self.turn_id,
        )
        self.assertTrue(read_tool.ok, read_tool.text)
        schema = read_tool.structured["capability"]["parameters_schema"]
        self.assertIn("artifact_id", schema["properties"])
        self.assertIn("representation", schema["properties"])

        called = await core.context.execution_runtime.execute_tool_async(
            CanonicalToolCall(name="call_tool", args={"name": "read_artifact", "args": {"artifact_id": ref.artifact_id}}),
            turn_id=self.turn_id,
        )
        self.assertEqual(called.status, RuntimeStatus.OK, called.text)
        self.assertIn("refund terms", called.text)

    @unittest.skipUnless(importlib.util.find_spec("PIL") is not None, "Pillow is not installed")
    def test_image_source_url_passthrough_to_openai_chat(self) -> None:
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
        self.assertEqual(
            exposure.inline_parts[0].source_url,
            "https://api.telegram.org/file/botFAKE_TOKEN/photos/file_123.jpg",
        )

        part_dict = exposure.inline_parts[0].to_message_part()
        self.assertEqual(part_dict["source_url"], "https://api.telegram.org/file/botFAKE_TOKEN/photos/file_123.jpg")

        coerced = _coerce_messages_for_openai_chat(
            [{"role": "user", "content": [
                {"type": "text", "text": "look"},
                exposure.inline_parts[0].to_message_part(),
            ]}],
            artifact_manager=self.manager,
            supports_vision=True,
        )
        image_part = coerced[0]["content"][1]
        self.assertEqual(image_part["type"], "image_url")
        self.assertEqual(
            image_part["image_url"]["url"],
            "https://api.telegram.org/file/botFAKE_TOKEN/photos/file_123.jpg",
        )
        self.assertFalse(image_part["image_url"]["url"].startswith("data:"))

        # Verify source_url (containing bot token) does NOT appear in LLM-visible text
        info = self.manager.info(ref.artifact_id, self.scope_key)
        info_str = str(info)
        self.assertNotIn("FAKE_TOKEN", info_str)
        self.assertNotIn("source_url", info["artifact"]["metadata"].get("source_metadata", {}))
        self.assertNotIn("source_url", exposure.text)

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
        self.assertEqual(exposure.inline_parts[0].source_url, "")

        coerced = _coerce_messages_for_openai_chat(
            [{"role": "user", "content": [
                {"type": "text", "text": "look"},
                exposure.inline_parts[0].to_message_part(),
            ]}],
            artifact_manager=self.manager,
            supports_vision=True,
        )
        image_part = coerced[0]["content"][1]
        self.assertTrue(image_part["image_url"]["url"].startswith("data:image/"))


class _Registry:
    def __init__(self, *providers) -> None:
        self.providers = providers

    def list_for_prompt(self):
        return list(self.providers)


if __name__ == "__main__":
    unittest.main()
