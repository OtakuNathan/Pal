from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from pal.core.runtime_config import RuntimeConfig


class RuntimeConfigTests(unittest.TestCase):
    def test_defaults_returns_default_values(self) -> None:
        cfg = RuntimeConfig.defaults()
        self.assertEqual(cfg.max_lines_to_read, 2_000)
        self.assertEqual(cfg.default_max_output_tokens, 25_000)
        self.assertEqual(cfg.default_max_result_size_chars, 50_000)
        self.assertEqual(cfg.stagnation_repeat_threshold, 3)
        self.assertEqual(cfg.llm_base_retry_delay_ms, 500)
        self.assertEqual(cfg.llm_max_output_recovery_attempts, 3)
        self.assertEqual(cfg.llm_request_timeout_seconds, 600.0)
        self.assertEqual(cfg.llm_compaction_timeout_seconds, 180.0)
        self.assertEqual(cfg.shutdown_compaction_timeout_seconds, 75.0)
        self.assertEqual(cfg.llm_stream_wall_timeout_seconds, 1_800.0)
        self.assertEqual(cfg.llm_stream_cleanup_timeout_seconds, 2.0)
        self.assertEqual(
            cfg.llm_wait_status_seconds,
            (120.0, 300.0, 600.0, 1_200.0),
        )
        self.assertEqual(cfg.llm_compaction_retry_attempts, 3)
        self.assertEqual(cfg.embedding_ollama_remote_base_urls, ())
        self.assertEqual(cfg.embedding_ollama_model_name, "bge-m3")
        self.assertEqual(cfg.embedding_ollama_remote_timeout_seconds, 8.0)
        self.assertEqual(cfg.embedding_ollama_local_timeout_seconds, 120.0)
        self.assertEqual(cfg.embedding_ollama_fallback_cooldown_seconds, 3600.0)

    def test_load_without_runtime_root_returns_defaults(self) -> None:
        cfg = RuntimeConfig.load(None)
        self.assertEqual(cfg.default_max_result_size_chars, 50_000)

    def test_load_with_missing_config_returns_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = RuntimeConfig.load(Path(tmpdir))
            self.assertEqual(cfg.default_max_result_size_chars, 50_000)
            self.assertEqual(cfg.stagnation_repeat_threshold, 3)

    def test_load_reads_read_section(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.toml"
            config_path.write_text(
                '[read]\nmax_lines_to_read = 1024\ndefault_max_output_tokens = 8192\nmax_output_size_bytes = 131072\n',
                encoding="utf-8",
            )
            cfg = RuntimeConfig.load(Path(tmpdir))
            self.assertEqual(cfg.max_lines_to_read, 1024)
            self.assertEqual(cfg.default_max_output_tokens, 8192)
            self.assertEqual(cfg.max_output_size_bytes, 131072)

    def test_load_reads_budget_section(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.toml"
            config_path.write_text(
                '[budget]\ndefault_max_result_size_chars = 99_000\n',
                encoding="utf-8",
            )
            cfg = RuntimeConfig.load(Path(tmpdir))
            self.assertEqual(cfg.default_max_result_size_chars, 99_000)
            self.assertEqual(cfg.stagnation_repeat_threshold, 3)

    def test_load_reads_stagnation_section(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.toml"
            config_path.write_text(
                '[stagnation]\nrepeat_threshold = 7\noscillation_window = 10\n',
                encoding="utf-8",
            )
            cfg = RuntimeConfig.load(Path(tmpdir))
            self.assertEqual(cfg.stagnation_repeat_threshold, 7)
            self.assertEqual(cfg.stagnation_oscillation_window, 10)
            self.assertEqual(cfg.default_max_result_size_chars, 50_000)

    def test_load_reads_llm_section(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.toml"
            config_path.write_text(
                '[llm]\nbase_retry_delay_ms = 1000\nendpoint_retry_attempts = 5\ncompaction_retry_attempts = 4\nmax_output_recovery_attempts = 2\nrequest_timeout_seconds = 90.5\ncompaction_timeout_seconds = 45.0\nshutdown_compaction_timeout_seconds = 6.0\nstream_wall_timeout_seconds = 1200\nstream_cleanup_timeout_seconds = 1.5\nwait_status_seconds = [600, 120, -1, 300, 120]\n',
                encoding="utf-8",
            )
            cfg = RuntimeConfig.load(Path(tmpdir))
            self.assertEqual(cfg.llm_base_retry_delay_ms, 1000)
            self.assertEqual(cfg.llm_endpoint_retry_attempts, 5)
            self.assertEqual(cfg.llm_compaction_retry_attempts, 4)
            self.assertEqual(cfg.llm_max_output_recovery_attempts, 2)
            self.assertEqual(cfg.llm_request_timeout_seconds, 90.5)
            self.assertEqual(cfg.llm_compaction_timeout_seconds, 45.0)
            self.assertEqual(cfg.shutdown_compaction_timeout_seconds, 6.0)
            self.assertEqual(cfg.llm_stream_wall_timeout_seconds, 1_200.0)
            self.assertEqual(cfg.llm_stream_cleanup_timeout_seconds, 1.5)
            self.assertEqual(cfg.llm_wait_status_seconds, (120.0, 300.0, 600.0))

    def test_load_reads_memory_section(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.toml"
            config_path.write_text(
                '[memory]\nembedding_ollama_remote_base_urls = ["http://mac.local:11434", "192.168.31.145:11434"]\nembedding_ollama_model_name = "bge-m3"\nembedding_ollama_remote_timeout_seconds = 3.5\n',
                encoding="utf-8",
            )
            cfg = RuntimeConfig.load(Path(tmpdir))
            self.assertEqual(cfg.embedding_ollama_remote_base_urls, ("http://mac.local:11434", "192.168.31.145:11434"))
            self.assertEqual(cfg.embedding_ollama_model_name, "bge-m3")
            self.assertEqual(cfg.embedding_ollama_remote_timeout_seconds, 3.5)

    def test_load_ignores_invalid_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.toml"
            config_path.write_text(
                '[budget]\ndefault_max_result_size_chars = "not_a_number"\n',
                encoding="utf-8",
            )
            cfg = RuntimeConfig.load(Path(tmpdir))
            self.assertEqual(cfg.default_max_result_size_chars, 50_000)

    def test_load_handles_corrupt_toml(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.toml"
            config_path.write_text("this is not valid toml {{{{", encoding="utf-8")
            cfg = RuntimeConfig.load(Path(tmpdir))
            self.assertEqual(cfg.default_max_result_size_chars, 50_000)


if __name__ == "__main__":
    unittest.main()
