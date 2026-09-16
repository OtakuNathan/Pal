"""Discover worker contributions only from enabled, attached installed plugins."""
from __future__ import annotations

from contextlib import closing
import importlib
from importlib.machinery import PathFinder
import os
from pathlib import Path
import sqlite3
import sys
import tomllib


def worker_extensions(runtime_root, *, database_path=None):
    database = (Path(database_path) if database_path else Path(runtime_root) / "pal.sqlite3").expanduser().resolve()
    if not database.is_file():
        return []
    with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='plugin_bundles'").fetchone():
            return []
        rows = db.execute('SELECT filesystem_path FROM plugin_bundles WHERE enabled=1 AND attached=1').fetchall()
    found = []
    for (directory,) in rows:
        root = Path(directory)
        manifest = root / 'plugin.toml'
        if not manifest.is_file():
            continue
        contribution = tomllib.loads(manifest.read_text()).get('execution', {})
        if contribution.get('role_entrypoint'):
            found.append((root, contribution))
    if len(found) > 1:
        raise RuntimeError('Multiple active worker execution extensions')
    return found


def activate_worker_extension(context, runtime_root, *, database_path=None):
    for root, contribution in worker_extensions(runtime_root, database_path=database_path):
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))
        module, name = contribution['role_entrypoint'].split(':', 1)
        getattr(importlib.import_module(module), name)(context)


def worker_dependency_paths(runtime_root, env, *, database_path=None):
    paths = []
    search = [p for p in env.get('PYTHONPATH', '').split(os.pathsep) if p] + sys.path
    for root, contribution in worker_extensions(runtime_root, database_path=database_path):
        paths.append(root)
        for name in contribution.get('worker_modules', []):
            spec = PathFinder.find_spec(name, [str(root), *search])
            if spec is None or not spec.origin:
                raise RuntimeError(f'Execution extension dependency unavailable: {name}')
            paths.append(Path(spec.origin).resolve().parent if spec.submodule_search_locations else Path(spec.origin).resolve())
    return paths
