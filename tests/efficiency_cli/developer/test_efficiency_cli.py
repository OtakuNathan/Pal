"""Developer tests for the ``pal bunshin efficiency`` CLI composition.

The sibling modules (store, metrics, report) are bound to parallel role
invocations; these tests exercise the CLI's own contract — argument
wiring, storage path resolution, error-to-exit-code mapping, and output
routing — by substituting deterministic adapters for the dependency calls
while keeping the production import surface intact.
"""

from __future__ import annotations

import argparse
import io
from pathlib import Path

import pytest

import pal.bunshin.efficiency_cli as cli
from pal.bunshin.config import bunshin_db_path
from pal.bunshin.v2.efficiency_store import (
    StorageUnavailableError,
    WorkflowNotFoundError,
)


def _args(tmp_path: Path, workflow_id: str = "wf-1", json_flag: bool = False) -> argparse.Namespace:
    return argparse.Namespace(
        runtime_root=tmp_path,
        workflow_id=workflow_id,
        json=json_flag,
    )


def _streams() -> tuple[io.StringIO, io.StringIO]:
    return io.StringIO(), io.StringIO()


def test_resolve_db_path_delegates_to_config(tmp_path: Path) -> None:
    assert cli.resolve_db_path(tmp_path) == bunshin_db_path(tmp_path)


def test_bunshin_subparser_registers_efficiency(tmp_path: Path) -> None:
    parser = argparse.ArgumentParser(prog="pal")
    subparsers = parser.add_subparsers(dest="command", required=True)
    returned = cli.register_bunshin_subparser(subparsers)

    assert returned.prog.endswith("bunshin")

    args = parser.parse_args(
        ["bunshin", "efficiency", "wf-42", "--runtime-root", str(tmp_path)]
    )
    assert args.command == "bunshin"
    assert args.bunshin_command == "efficiency"
    assert args.workflow_id == "wf-42"
    assert args.runtime_root == tmp_path
    assert args.json is False

    json_args = parser.parse_args(
        ["bunshin", "efficiency", "wf-42", "--runtime-root", str(tmp_path), "--json"]
    )
    assert json_args.json is True

    with pytest.raises(SystemExit):
        parser.parse_args(["bunshin", "efficiency", "wf-42"])


def test_pal_main_parser_wires_bunshin_efficiency(tmp_path: Path) -> None:
    from pal.main import _build_parser

    parser = _build_parser()
    args = parser.parse_args(
        ["bunshin", "efficiency", "wf-7", "--runtime-root", str(tmp_path)]
    )
    assert args.command == "bunshin"
    assert args.bunshin_command == "efficiency"
    assert args.workflow_id == "wf-7"


def test_text_report_success_and_read_only_call(tmp_path: Path, monkeypatch) -> None:
    calls: list[tuple[Path, str]] = []
    records = object()
    metrics = object()

    def fake_read(db_path: Path, workflow_id: str):
        calls.append((db_path, workflow_id))
        return records

    monkeypatch.setattr(cli, "read_workflow_telemetry", fake_read)
    monkeypatch.setattr(cli, "compute_workflow_metrics", lambda r: metrics)
    monkeypatch.setattr(cli, "render_text", lambda m: "TEXT-REPORT")
    monkeypatch.setattr(cli, "render_json", lambda m: "JSON-REPORT")

    stdout, stderr = _streams()
    exit_code = cli.run_efficiency_command(_args(tmp_path), stdout=stdout, stderr=stderr)

    assert exit_code == 0
    assert stdout.getvalue() == "TEXT-REPORT"
    assert stderr.getvalue() == ""
    assert calls == [(cli.resolve_db_path(tmp_path), "wf-1")]


def test_json_flag_selects_json_renderer(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(cli, "read_workflow_telemetry", lambda db_path, workflow_id: object())
    monkeypatch.setattr(cli, "compute_workflow_metrics", lambda records: object())
    monkeypatch.setattr(cli, "render_text", lambda metrics: "TEXT-REPORT")
    monkeypatch.setattr(cli, "render_json", lambda metrics: "JSON-REPORT")

    stdout, stderr = _streams()
    exit_code = cli.run_efficiency_command(
        _args(tmp_path, json_flag=True), stdout=stdout, stderr=stderr
    )

    assert exit_code == 0
    assert stdout.getvalue() == "JSON-REPORT"
    assert stderr.getvalue() == ""


def test_unavailable_metrics_still_exit_zero(tmp_path: Path, monkeypatch) -> None:
    # Exit code 0 must not depend on metric availability; the renderer owns
    # how unavailability is presented.
    monkeypatch.setattr(cli, "read_workflow_telemetry", lambda db_path, workflow_id: object())
    monkeypatch.setattr(cli, "compute_workflow_metrics", lambda records: object())
    monkeypatch.setattr(cli, "render_text", lambda metrics: "report with unavailable lines")
    monkeypatch.setattr(cli, "render_json", lambda metrics: "report")

    stdout, stderr = _streams()
    exit_code = cli.run_efficiency_command(_args(tmp_path), stdout=stdout, stderr=stderr)

    assert exit_code == 0
    assert "unavailable" in stdout.getvalue()


def test_unknown_workflow_names_id_and_exits_one(tmp_path: Path, monkeypatch) -> None:
    def fake_read(db_path: Path, workflow_id: str):
        raise WorkflowNotFoundError(f"no role invocations for {workflow_id}")

    monkeypatch.setattr(cli, "read_workflow_telemetry", fake_read)

    stdout, stderr = _streams()
    exit_code = cli.run_efficiency_command(
        _args(tmp_path, workflow_id="missing-wf"), stdout=stdout, stderr=stderr
    )

    assert exit_code == 1
    assert stdout.getvalue() == ""
    assert len(stderr.getvalue().splitlines()) == 1
    assert "missing-wf" in stderr.getvalue()


def test_storage_unavailable_exits_one_without_creating_file(
    tmp_path: Path, monkeypatch
) -> None:
    def fake_read(db_path: Path, workflow_id: str):
        raise StorageUnavailableError(f"cannot open {db_path} read-only")

    monkeypatch.setattr(cli, "read_workflow_telemetry", fake_read)

    stdout, stderr = _streams()
    exit_code = cli.run_efficiency_command(_args(tmp_path), stdout=stdout, stderr=stderr)

    assert exit_code == 1
    assert stdout.getvalue() == ""
    assert stderr.getvalue() != ""
    assert not cli.resolve_db_path(tmp_path).exists()


def test_malformed_row_reports_diagnostic_and_exits_one(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(cli, "read_workflow_telemetry", lambda db_path, workflow_id: object())

    def broken_compute(records):
        raise ValueError("non-integer token column")

    monkeypatch.setattr(cli, "compute_workflow_metrics", broken_compute)

    stdout, stderr = _streams()
    exit_code = cli.run_efficiency_command(_args(tmp_path), stdout=stdout, stderr=stderr)

    assert exit_code == 1
    assert stdout.getvalue() == ""
    assert "malformed" in stderr.getvalue()


def test_unexpected_store_error_does_not_escape(tmp_path: Path, monkeypatch) -> None:
    def fake_read(db_path: Path, workflow_id: str):
        raise RuntimeError("unexpected store failure")

    monkeypatch.setattr(cli, "read_workflow_telemetry", fake_read)

    stdout, stderr = _streams()
    exit_code = cli.run_efficiency_command(_args(tmp_path), stdout=stdout, stderr=stderr)

    assert exit_code == 1
    assert stdout.getvalue() == ""
    assert stderr.getvalue() != ""


def test_command_never_closes_borrowed_streams(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(cli, "read_workflow_telemetry", lambda db_path, workflow_id: object())
    monkeypatch.setattr(cli, "compute_workflow_metrics", lambda records: object())
    monkeypatch.setattr(cli, "render_text", lambda metrics: "TEXT-REPORT")

    class TrackingStream(io.StringIO):
        closed_by_command = False

        def close(self) -> None:
            self.closed_by_command = True
            super().close()

    stdout = TrackingStream()
    stderr = TrackingStream()
    exit_code = cli.run_efficiency_command(_args(tmp_path), stdout=stdout, stderr=stderr)

    assert exit_code == 0
    assert stdout.closed_by_command is False
    assert stderr.closed_by_command is False
