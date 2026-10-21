"""Export the authoritative file engine for standalone workers.

Only imports are rewritten. Algorithms, authorization and diagnostics remain
byte-for-byte source copies. --check detects either source or vendor drift.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]
MODULES = ('file_read', 'file_edit', 'file_write', 'file_state',
           'file_tool_contracts', 'session_state', 'file_registry')


def export(destination: Path, *, check: bool = False) -> None:
    sources = {f'{name}.py': ROOT / f'src/pal/execution/{name}.py' for name in MODULES}
    sources.update({'contracts.py': ROOT / 'src/pal/execution/file_core_contracts.py',
                    'diagnostics.py': ROOT / 'src/pal/foundation/diagnostics.py'})
    files = {'__init__.py': '"""Generated Pal file engine; see SOURCE.json. Do not edit."""\n'}
    files['LICENSE'] = (ROOT / 'LICENSE').read_text()
    hashes = {}
    for name, path in sources.items():
        source = path.read_text()
        hashes[str(path.relative_to(ROOT))] = hashlib.sha256(source.encode()).hexdigest()
        for module in MODULES:
            source = source.replace(f'from pal.execution.{module} import ', f'from .{module} import ')
        source = source.replace('from pal.execution.contracts import ', 'from .contracts import ')
        source = source.replace('from pal.execution.tool_facade import ', 'from .contracts import ')
        source = source.replace('from pal.shared import RuntimeStatus', 'from .contracts import RuntimeStatus')
        source = source.replace('from pal.shared.diagnostics import ', 'from .diagnostics import ')
        if 'from pal.' in source or 'import pal.' in source:
            raise ValueError(f'Unexpected Pal dependency in {path}')
        files[name] = source
    # Source hashes also identify an uncommitted development revision exactly.
    revision = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
    manifest = {'pal_revision': revision, 'source_sha256': hashes}
    if check and (destination / 'SOURCE.json').exists():
        # A later unrelated Pal commit does not change the exported engine.
        manifest['pal_revision'] = json.loads((destination / 'SOURCE.json').read_text())['pal_revision']
    files['SOURCE.json'] = json.dumps(manifest, indent=2, sort_keys=True) + '\n'
    if check:
        changed = [name for name, text in files.items()
                   if not (destination / name).exists() or (destination / name).read_text() != text]
        if changed:
            raise SystemExit('File engine export differs: ' + ', '.join(changed))
        return
    destination.mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        (destination / name).write_text(text)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('destination', type=Path)
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    export(args.destination, check=args.check)
