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
        self.assertEqual(cfg.max_tool_results_per_message_chars, 200_000)
        self.assertEqual(cfg.stagnation_repeat_threshold, 3)
        self.assertEqual(cfg.llm_base_retry_delay_ms, 500)
        self.assertEqual(cfg.keep_recent_tool_messages, 10)
        self.assertEqual(cfg.l1_tool_result_max_chars, 8_000)
        self.assertEqual(cfg.l1_tool_result_preview_chars, 4_000)

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
                '[budget]\ndefault_max_result_size_chars = 99_000\nmax_tool_results_per_message_chars = 150_000\n',
                encoding="utf-8",
            )
            cfg = RuntimeConfig.load(Path(tmpdir))
            self.assertEqual(cfg.default_max_result_size_chars, 99_000)
            self.assertEqual(cfg.max_tool_results_per_message_chars, 150_000)
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
                '[llm]\nbase_retry_delay_ms = 1000\nendpoint_retry_attempts = 5\n',
                encoding="utf-8",
            )
            cfg = RuntimeConfig.load(Path(tmpdir))
            self.assertEqual(cfg.llm_base_retry_delay_ms, 1000)
            self.assertEqual(cfg.llm_endpoint_retry_attempts, 5)

    def test_load_reads_memory_section(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.toml"
            config_path.write_text(
                '[memory]\nkeep_recent_tool_messages = 20\nl1_tool_result_max_chars = 12_000\nl1_tool_result_preview_chars = 6_000\n',
                encoding="utf-8",
            )
            cfg = RuntimeConfig.load(Path(tmpdir))
            self.assertEqual(cfg.keep_recent_tool_messages, 20)
            self.assertEqual(cfg.l1_tool_result_max_chars, 12_000)
            self.assertEqual(cfg.l1_tool_result_preview_chars, 6_000)

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
