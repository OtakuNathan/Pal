"""CLI composition for ``pal bunshin efficiency WORKFLOW_ID``.

This module is the terminal sink of the efficiency telemetry feature: it
owns argument parsing, storage location resolution, read-only query
execution, rendering, exit codes, and the operator documentation surface.
It performs no metric derivation or formatting of its own.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import TextIO

from pal.bunshin.config import bunshin_db_path
from pal.bunshin.v2.efficiency_metrics import compute_workflow_metrics
from pal.bunshin.v2.efficiency_report import render_json, render_text
from pal.bunshin.v2.efficiency_store import (
    EfficiencyStoreError,
    WorkflowNotFoundError,
    read_workflow_telemetry,
)


def resolve_db_path(runtime_root: Path) -> Path:
    """Resolve the single authoritative Bunshin v2 storage location.

    The Bunshin v2 workflow database location is defined by
    ``pal.bunshin.config.bunshin_db_path(runtime_root)`` — the same source
    the Bunshin manager and ``BunshinV2WorkflowService`` use to write it.
    The ``PAL_BUNSHIN_RUNTIME_DB_PATH`` environment variable never applies
    here: it overrides the separate ``pal.sqlite3`` runtime database, not
    the Bunshin v2 store.

    Args:
        runtime_root: Pal runtime root supplied by the ``--runtime-root``
            CLI flag, identical to every other ``pal`` subcommand.

    Returns:
        The database path under the runtime root. No existence check or
        creation is performed; read-only open failures remain the store's
        responsibility.

    Errors:
        Never raises; resolution is a pure path computation.
    """
    return bunshin_db_path(Path(runtime_root))


def register_bunshin_subparser(subparsers: argparse._SubParsersAction) -> argparse.ArgumentParser:
    """Register the ``pal bunshin`` command group with its ``efficiency``
    subcommand on the existing Pal CLI parser.

    Args:
        subparsers: The argparse subparsers action of the Pal CLI.

    Returns:
        The ``bunshin`` argument parser, with an ``efficiency`` subcommand
        accepting one positional ``workflow_id``, an optional ``--json``
        flag, and the same required ``--runtime-root`` flag convention as
        every other ``pal`` subcommand.

    Errors:
        Never raises for well-formed parsers.
    """
    bunshin_parser = subparsers.add_parser(
        "bunshin",
        help="Inspect Bunshin v2 workflow data",
    )
    bunshin_subparsers = bunshin_parser.add_subparsers(
        dest="bunshin_command",
        required=True,
    )
    efficiency_parser = bunshin_subparsers.add_parser(
        "efficiency",
        help="Report workflow efficiency telemetry read-only",
    )
    efficiency_parser.add_argument(
        "workflow_id",
        help="Workflow identifier to report on",
    )
    efficiency_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit a machine-readable JSON document instead of text",
    )
    efficiency_parser.add_argument(
        "--runtime-root",
        type=Path,
        required=True,
        help="Pal runtime root holding the Bunshin v2 storage",
    )
    efficiency_parser.set_defaults(command="bunshin", bunshin_command="efficiency")
    return bunshin_parser


def run_efficiency_command(
    args: argparse.Namespace,
    *,
    stdout: TextIO,
    stderr: TextIO,
) -> int:
    """Execute ``pal bunshin efficiency`` end to end.

    Resolves the Bunshin v2 storage path via ``resolve_db_path(args.runtime_root)``
    (delegating to ``pal.bunshin.config.bunshin_db_path``), opens it strictly
    read-only, reads telemetry, computes metrics, and writes the text or JSON
    report to ``stdout``.

    Args:
        args: Namespace with ``runtime_root`` (Path), ``workflow_id`` (str),
            and ``json`` (bool) attributes.
        stdout: Destination stream for the report.
        stderr: Destination stream for diagnostics.

    Returns:
        Process exit code: ``0`` on a successful report (including reports
        that honestly list unavailable legacy metrics), ``1`` when the
        workflow is unknown or storage cannot be opened read-only.

    Errors:
        Never raises to the caller; all failures are reported on
        ``stderr`` with a nonzero exit code. The command never writes to
        Bunshin storage and never mutates workflow state.
    """
    db_path = resolve_db_path(args.runtime_root)
    try:
        records = read_workflow_telemetry(db_path, args.workflow_id)
        metrics = compute_workflow_metrics(records)
    except WorkflowNotFoundError:
        stderr.write(f"Unknown workflow id: {args.workflow_id}\n")
        return 1
    except EfficiencyStoreError as exc:
        stderr.write(f"efficiency: bunshin storage read failed: {exc}\n")
        return 1
    except ValueError as exc:
        stderr.write(f"efficiency: malformed telemetry row: {exc}\n")
        return 1
    except Exception as exc:  # unexpected store-side failure: never escape
        stderr.write(f"efficiency: unexpected error: {exc}\n")
        return 1

    report = render_json(metrics) if args.json else render_text(metrics)
    stdout.write(report)
    return 0
