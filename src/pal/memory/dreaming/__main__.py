"""Offline dreaming on an independent, consistent copy of a runtime."""
from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
from dataclasses import replace
from pathlib import Path

from pal.core.runtime_config import RuntimeConfig
from pal.foundation import PalV2Database
from pal.llm import LLMEndpointRepository, LLMRuntime, LLMCredentialResolver, RuntimeSettingRepository, build_default_endpoint_invoker
from pal.llm import EndpointResolver
from pal.llm.secret_store import EncryptedFileSecretStore
from pal.memory.dreaming.clustering import discover_clusters
from pal.memory.dreaming.contracts import DreamingConfig
from pal.memory.dreaming.service import DreamingService
from pal.memory.embedding import build_ollama_embedding_provider_from_config
from pal.memory.service import MemoryService
from pal.memory.storage import MemoryStorage
from pal.plugins.l3.sqlite_vec import SQLiteVecL3Plugin
from pal.wizard.runtime import ALL_MODELS


def backup(source, destination):
    reader = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    writer = sqlite3.connect(destination)
    try:
        reader.backup(writer)
    finally:
        writer.close()
        reader.close()


async def run(args):
    source_root, target = args.source_root.resolve(), args.output_root.resolve()
    if target.exists() or source_root == target or source_root in target.parents:
        raise ValueError("output root must be a new directory outside the source runtime")
    target.mkdir(parents=True, mode=0o700)
    backup(source_root / "pal.sqlite3", target / "pal.sqlite3")
    source_storage = MemoryStorage(source_root, read_only=True)
    source_memory = source_storage.path(source_storage.current()) if source_storage.catalog_path.exists() else source_root / "pal.sqlite3"
    backup(source_memory, target / "memory-source.sqlite3")
    database = PalV2Database(target / "pal.sqlite3")
    database.initialize(ALL_MODELS)
    storage = MemoryStorage(target)
    storage.migrate(target / "memory-source.sqlite3")
    if source_storage.catalog_path.exists():
        storage.inherit_deletions(source_storage)
    provider = SQLiteVecL3Plugin(service=MemoryService(), repository=storage.open(),
        embedding_provider=build_ollama_embedding_provider_from_config(RuntimeConfig.load(source_root)))
    storage.purge_forgotten(provider.repository, storage.deleted_refs())
    config = DreamingConfig.load(source_root)
    if args.endpoint:
        config = replace(config, endpoint_id=args.endpoint)
    if args.review_endpoint:
        config = replace(config, review_endpoint_id=args.review_endpoint)
    llm = None
    try:
        if args.preprocess_only:
            clusters = await asyncio.to_thread(discover_clusters, provider.repository, config)
            report = {"dry_run": True, "preprocess_only": True, "records": sum(len(c.members) for c in clusters),
                "groups": len(clusters), "groups_with_candidates": sum(len(c.members) > 1 for c in clusters),
                "cluster_sizes": [len(c.members) for c in clusters]}
        else:
            llm = LLMRuntime(endpoint_resolver=EndpointResolver(repository=LLMEndpointRepository()),
                settings_repository=RuntimeSettingRepository(), config=RuntimeConfig.load(source_root),
                endpoint_invoker=build_default_endpoint_invoker(credentials=LLMCredentialResolver(
                    secret_store=EncryptedFileSecretStore(secrets_path=str(source_root / "secrets.json"))), runtime_root=target))
            service = DreamingService(storage=storage, provider=provider, llm=llm, config=config)
            report = {"dry_run": True, **await service.run()}
            comparison_path = target / "comparison.json"
            comparison_path.write_text(json.dumps(service.comparison(report.get("run_id")), ensure_ascii=False, indent=2), encoding="utf-8")
            report["comparison_path"] = str(comparison_path)
        report_path = target / "report.json"
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"report_path": str(report_path), "status": report.get("status", "preprocessed"),
                          "outcome": report.get("report", {}).get("outcome"), "records": report.get("records")}, ensure_ascii=False))
        return 1 if report.get("status") == "failed" else 0
    finally:
        provider.repository.close()
        if llm is not None:
            llm.close()
        database.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--endpoint")
    parser.add_argument("--review-endpoint")
    parser.add_argument("--preprocess-only", action="store_true")
    raise SystemExit(asyncio.run(run(parser.parse_args())))


if __name__ == "__main__":
    main()
