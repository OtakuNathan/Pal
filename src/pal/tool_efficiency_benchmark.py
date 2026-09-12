"""Isolated, paired tool-presentation evaluation using the configured LLM only.

Never opens a resident Pal runtime. Discovery/input validation are real; shell
and LSP effects are fixtures. All file reads are confined to a temporary tree.
"""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import statistics
import sys
import tempfile
from types import SimpleNamespace

from pal.core.runtime import PalCore
from pal.core.runtime_config import RuntimeConfig
from pal.eval_tools import _build_report, _run_case, load_tools_benchmark
from pal.execution.capabilities import register_with_core
from pal.execution.contracts import ToolCallBudget
from pal.execution.tool_facade import EffectOutcome, EffectReceipt, ToolHandlerResult, rejection
from pal.llm.credentials import LLMCredentialResolver
from pal.llm.models import LLMEndpointModel
from pal.llm.runtime import EndpointResolver, LLMRuntime, build_default_endpoint_invoker
from pal.llm.secret_store import EncryptedFileSecretStore
from pal.lsp import build_lsp_plugin
from pal.shared.json_values import thaw_json

MANIFEST = Path(__file__).parents[2] / 'benchmarks/tools/efficiency-v2.json'


class FixtureExecution:
    def __init__(self, root: Path, case_id: str):
        self.root, self.case_id = root, case_id
        self.core = PalCore()
        register_with_core(self.core.context)
        handle = build_lsp_plugin(runtime_root=root).register_with_core(self.core.context)
        handle.introspection_provider._request_or_error = self._lsp
        for module in ('execution', 'lsp'):
            self.core.publish_module_capabilities(module)
        self.runtime = self.core.context.execution_runtime
        self.prepared = False
        self.unsafe_attempts = 0

    def _lsp(self, method, args):
        partial = self.case_id == 'lsp-partial'
        if method == 'prepare_workspace':
            self.prepared = not partial
            return {'status': 'partial' if partial else 'ok', 'workspace_root': '.',
                    'ready': not partial, 'primary_probe_ready': not partial, 'primary_server': 'fixture-python'}
        if method in {'status', 'doctor'}:
            return {'status': 'ok', 'ready': self.prepared, 'primary_probe_ready': self.prepared}
        if not self.prepared:
            return {'status': 'unavailable', 'error': 'Prepare this workspace before navigation.'}
        return {'status': 'ok', 'diagnostics': [], 'symbols': [{'name': 'greet', 'line': 1}]}

    @property
    def registry_generation(self):
        return self.runtime.registry_generation

    async def execute_tool_async(self, call, *, turn_id):
        self.runtime.begin_tool_result_turn(turn_id=turn_id, scope_key='fixture', input_id=self.case_id)
        if self.case_id == 'page-evidence' and self.runtime.read_tool_result_page(result_ref='fixture-long', turn_id=turn_id) is None:
            self.runtime.tool_result_pager.store(runtime_root=self.root, turn_id=turn_id,
                result_ref='fixture-long', tool_name='read_file', status='ok', ok=True,
                rendered=('    row\r\n' * 100 + 'FINAL_MARKER\n \t'), page_size=300)
        alias = call.args.get('name') if call.name == 'call_tool' else call.name
        args = call.args.get('args', {}) if call.name == 'call_tool' else call.args
        allowed = {'search_tools','read_tool','read_tool_result','exec_show','exec_tools','read_file',
                   'lsp_prepare_workspace','lsp_status','lsp_doctor','lsp_diagnostics','lsp_document_symbols'}
        blocked = alias not in allowed and self.registry_generation.record_for_alias(alias) is not None
        if alias == 'read_file':
            target = (self.root / str(args.get('file_path') or '')).resolve()
            blocked = target not in {self.root/'pyproject.toml',self.root/'sample.py'}
        if call.name == 'run_shell':
            if args.get('cmd') == 'pwd' and (self.root / str(args.get('cwd') or '.')).resolve() == self.root:
                record = self.registry_generation.record_for_alias('run_shell')
                try:
                    record.input_model.model_validate(dict(args), strict=True)
                except Exception:
                    return self.runtime._canonical_result_from_invocation(call.name,call.call_id,rejection('invalid_arguments','Use cmd="pwd".'))
                raw = ToolHandlerResult(output={'cmd':'pwd','returncode':0,'stdout':'.\n','stderr':''},
                    effect_receipt=EffectReceipt(outcome=EffectOutcome.APPLIED), llm_text='{"returncode":0,"stdout":".\\n","stderr":""}')
                value = self.runtime._normalize_invocation_result(record,call,raw,budget=None,turn_id=turn_id)
                return self.runtime._canonical_result_from_invocation(call.name,call.call_id,value)
            blocked = True
        # No unknown alias may escape into an external adapter, even if the
        # model invents one. The real registry supplies the not-found feedback.
        if blocked:
            self.unsafe_attempts += 1
            return self.runtime._canonical_result_from_invocation(call.name,call.call_id,
                rejection('fixture_effect_blocked','This fixture permits only its requested read or simulated operation.'))
        return await self.runtime.execute_tool_async(call, turn_id=turn_id,
            budget=ToolCallBudget(max_output_chars=12000,preview_chars=1000))

    def close(self):
        self.runtime.shutdown()


def endpoint_snapshot(runtime_root: Path, endpoint_id: str | None):
    with sqlite3.connect(f'file:{runtime_root / "pal.sqlite3"}?mode=ro', uri=True) as db:
        db.row_factory = sqlite3.Row
        settings = {row['setting_key']:row['setting_value'] for row in db.execute('SELECT * FROM pal_runtime_settings')}
        if not endpoint_id:
            # Reuse the repository's public active-setting key without binding
            # Peewee models or initializing any runtime tables.
            from pal.llm.repository import ACTIVE_LLM_ENDPOINT_SETTING_KEY
            endpoint_id = settings.get(ACTIVE_LLM_ENDPOINT_SETTING_KEY)
        row = db.execute('SELECT * FROM llm_endpoints WHERE endpoint_id=? AND enabled=1', (endpoint_id,)).fetchone()
        if row is None:
            raise ValueError('The benchmark needs one configured, enabled endpoint.')
        values = dict(row)
    for key in values:
        if key.endswith('_blob'):
            values[key] = json.loads(values[key]) if isinstance(values[key],str) else values[key]
    return values


def build_llm(runtime_root: Path, values):
    endpoint = LLMEndpointModel(**values)
    settings = SimpleNamespace(get_active_llm_endpoint_id=lambda: endpoint.endpoint_id,
        get_think_level=lambda _: 'medium', set_think_level=lambda _, value: value)
    return LLMRuntime(endpoint_resolver=EndpointResolver(endpoints=(endpoint,)), settings_repository=settings,
        config=RuntimeConfig.load(runtime_root), endpoint_invoker=build_default_endpoint_invoker(
            credentials=LLMCredentialResolver(secret_store=EncryptedFileSecretStore(secrets_path=str(runtime_root/'secrets.json')))))


def summarize(runs):
    rows = [row for run in runs for row in run.get('usage', [])]
    discovery = [run for run in runs if run.get('category') not in {'selection', 'arguments', 'paging'}]
    total = lambda group: sum(row['input_tokens']+row['output_tokens'] for run in group for row in run.get('usage', []))
    return {
        'completed': sum(run['eventual_correct'] for run in runs), 'runs': len(runs),
        'input_tokens': sum(row['input_tokens'] for row in rows),
        'output_tokens': sum(row['output_tokens'] for row in rows),
        'total_tokens': total(runs), 'discovery_tokens': total(discovery),
        'cached_input_tokens': sum(row['cached_input_tokens'] for row in rows),
        'reasoning_tokens': sum(row['reasoning_tokens'] for row in rows),
        'usage_complete': bool(rows) and all(row['reported'] for row in rows),
        'median_rounds': statistics.median(len(run.get('usage', [])) for run in runs) if runs else 0,
        'unsafe_attempts': sum(run.get('unsafe_attempts',0) for run in runs),
        'forbidden_or_dangerous_runs': sum(bool(run.get('dangerous_retry')) for run in runs),
    }


async def worker(runtime_root: Path, snapshot: Path):
    manifest, cases = load_tools_benchmark(MANIFEST)
    values = json.loads(snapshot.read_text())
    llm = build_llm(runtime_root, values)
    try:
        for line in sys.stdin:
            item = json.loads(line)
            case = next(c for c in cases if c.case_id == item['case_id'])
            case = replace(case, prompt=(
                'Fixture context: the current directory is already the repository root. '
                'pyproject.toml and sample.py exist here. Paths may be relative. '
                'No preliminary shell inspection is needed. Shell operations are simulated; '
                'only the explicitly requested pwd command is available.\n' + case.prompt))
            with tempfile.TemporaryDirectory(prefix='pal-tool-eval-') as temp:
                root = Path(temp)
                (root/'pyproject.toml').write_text('[project]\nname = "fixture"\n')
                (root/'sample.py').write_bytes(b'def greet():\r\n    return "hello"  \r\n')
                old_cwd = Path.cwd()
                fixture = FixtureExecution(root, case.case_id)
                try:
                    os.chdir(root)
                    specs = thaw_json(list(fixture.registry_generation.provider_specs.values()))
                    run = await _run_case(case=case, repetition=item['repetition'],model=values['model_id'],temperature=0,
                        reasoning='medium',tool_contracts=specs,llm_runtime=llm,execution_runtime=fixture,
                        endpoint_id=values['endpoint_id'],max_output_tokens=8192)
                    run['unsafe_attempts'] = fixture.unsafe_attempts
                    print(json.dumps({'run':run, 'specs':specs, 'generation':fixture.registry_generation.generation_hash}),flush=True)
                except Exception as exc:
                    print(json.dumps({'error':type(exc).__name__, 'case_id':case.case_id}),flush=True)
                finally:
                    os.chdir(old_cwd)
                    fixture.close()
    finally:
        llm.close()


async def compare(args):
    target = args.output.resolve()
    target.mkdir(mode=0o700, parents=True, exist_ok=False)
    values = endpoint_snapshot(args.runtime_root.resolve(), args.endpoint)
    snapshot = target/'endpoint.json'
    snapshot.write_text(json.dumps(values))
    snapshot.chmod(0o600)
    manifest, cases = load_tools_benchmark(MANIFEST)
    processes, errors = {}, []
    rows = {'base':[], 'search':[]}
    last = {}
    try:
        for label, worktree in (('base',args.base),('search',args.search)):
            log = (target/f'{label}.stderr').open('w')
            processes[label] = (await asyncio.create_subprocess_exec(sys.executable,'-m','pal.tool_efficiency_benchmark',
                '--worker','--runtime-root',str(args.runtime_root.resolve()),'--snapshot',str(snapshot),
                cwd=worktree,env={**os.environ,'PYTHONPATH':str(worktree.resolve()/'src')},
                stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=log,limit=4*1024*1024),log)
        for repetition in range(1,4):
            for index, case in enumerate(cases):
                for label in (('search','base') if (index+repetition)%2 else ('base','search')):
                    proc,_ = processes[label]
                    proc.stdin.write((json.dumps({'case_id':case.case_id,'repetition':repetition})+'\n').encode())
                    await proc.stdin.drain()
                    line = await proc.stdout.readline()
                    result = json.loads(line)
                    if 'error' in result:
                        errors.append({'variant':label,**result})
                        print(json.dumps(errors[-1]),flush=True)
                        continue
                    last[label] = result
                    rows[label].append(result['run'])
                    (target/f'{label}-runs.json').write_text(json.dumps(rows[label],ensure_ascii=False))
                    print(json.dumps({'variant':label,'case':case.case_id,'repetition':repetition,
                        'ok':result['run']['eventual_correct'],'tokens':summarize([result['run']])['total_tokens']}),flush=True)
        reports = {}
        for label in rows:
            reports[label] = _build_report(manifest=manifest,manifest_path=MANIFEST,
                generation_hash=last.get(label,{}).get('generation',''),tool_contracts=last.get(label,{}).get('specs',[]),runs=rows[label],
                requested_endpoint_id=values['endpoint_id'],expected_model_id=values['model_id'])
            reports[label]['efficiency'] = summarize(rows[label])
            (target/f'{label}-report.json').write_text(json.dumps(reports[label],ensure_ascii=False,indent=2))
        base,search = reports['base'],reports['search']
        b,s = base['efficiency'],search['efficiency']
        quality = ('eventual_accuracy','first_pass_argument_rate','enum_repair_rate','detach_not_found_recovery_rate','top_1_accuracy','confusable_pair_accuracy')
        checks = {'complete_pairs':not errors and b['runs']==s['runs']==len(cases)*3,
            'baseline_passed':base['passed'],'candidate_passed':search['passed'],
            'usage_complete':b['usage_complete'] and s['usage_complete'],
            'safe':b['unsafe_attempts']==s['unsafe_attempts']==b['forbidden_or_dangerous_runs']==s['forbidden_or_dangerous_runs']==0,
            'quality_no_regression':all(search['metrics'][k]>=base['metrics'][k] for k in quality),
            'rounds_no_increase':s['median_rounds']<=b['median_rounds'],
            'total_tokens_no_increase':s['total_tokens']<=b['total_tokens'],
            'discovery_tokens_lower':s['discovery_tokens']<b['discovery_tokens']}
        result = {'checks':checks,'selected':'search' if all(checks.values()) else 'base' if base['passed'] and checks['complete_pairs'] and b['usage_complete'] and not b['unsafe_attempts'] and not b['forbidden_or_dangerous_runs'] else 'neither',
                  'base':b,'search':s,'errors':errors,'endpoint_id':values['endpoint_id'],'model_id':values['model_id'],
                  'manifest_sha256':hashlib.sha256(MANIFEST.read_bytes()).hexdigest()}
        (target/'comparison.json').write_text(json.dumps(result,indent=2))
        print(json.dumps(result),flush=True)
    finally:
        for proc,log in processes.values():
            if proc.stdin:
                proc.stdin.close()
            try:
                await asyncio.wait_for(proc.wait(),timeout=10)
            except asyncio.TimeoutError:
                proc.terminate()
                await proc.wait()
            log.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime-root',type=Path,required=True)
    parser.add_argument('--base',type=Path)
    parser.add_argument('--search',type=Path)
    parser.add_argument('--output',type=Path)
    parser.add_argument('--endpoint')
    parser.add_argument('--worker',action='store_true')
    parser.add_argument('--snapshot',type=Path)
    args = parser.parse_args()
    if not args.worker and not all((args.base,args.search,args.output)):
        parser.error('--base, --search and --output are required for comparison')
    asyncio.run(worker(args.runtime_root,args.snapshot) if args.worker else compare(args))


if __name__ == '__main__':
    main()
