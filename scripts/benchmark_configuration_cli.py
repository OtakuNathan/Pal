"""Real-model configuration flows against isolated temporary owners, never ~/.pal.

Production CLI subprocesses, core configuration RPC and catalog service are used.
Skill discovery/injection and read-only business results are fixture projections;
no real search HTTP requests, production configuration or lifecycle actions run.
Only one validated pal configuration command and optional fixture stdin redirect
are executable per run_shell call. Arbitrary shell execution is rejected.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import redirect_stderr, redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import shlex
import sys
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from pal.core import PalCore
from pal.main import _build_parser
from pal.control.configuration import CoreConfigurationService
from pal.bunshin.catalog import BunshinCatalogService
from pal.bunshin.families import BunshinFamilyRegistry
from pal.bunshin.profiles import BunshinProfileRegistry
from pal.bunshin.ipc import start_manager_server, cleanup_manager_endpoint
from pal.foundation import PalV2Database
from pal.foundation.sidecar import dispatch_sidecar_request, pack_sidecar_message, read_sidecar_message
from pal.llm.models import PalRuntimeSettingModel
from pal.llm.repository import RuntimeSettingRepository
from pal.packages.process import runtime_lease
from pal.skill.builtin_skills import PAL_SELF_MAINTENANCE_MANUAL
from pal.web_search.repository import WebSearchProviderRepository
from pal.web_search.models import WebSearchProviderModel
from pal.web_search.service import WebSearchService
from pal.web_search.capabilities import WebSearchIntrospectionProvider
from pal.shared import IntrospectionCall
from pal.tool_efficiency_benchmark import endpoint_snapshot, build_llm
from pal.llm.conversions import message_ir_from_dict, tool_definition_ir_from_dict
from pal.llm.ir import GenerationPolicyIR, LLMRequestIR
from pal.llm.serde import message_to_payload
from pal.eval_tools import _eval_tool_result_message
from scripts.benchmark_full_discovery_flow import build_flow_catalog, tool_contract
from scripts.benchmark_discovery_flow import prompt_messages


CASES = ('search-settings', 'search-auth', 'search-selection', 'core-disable',
         'profile-patch', 'family-reset', 'unavailable-owner')


def definition(name, description, properties, required):
    return tool_definition_ir_from_dict({'type': 'function', 'function': {
        'name': name, 'description': description, 'parameters': {'type': 'object',
        'properties': properties, 'required': required, 'additionalProperties': False}}})


def validate_command(command, root):
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    tokens = list(lexer)
    stdin_path = None
    if '<' in tokens:
        index = tokens.index('<')
        if index != len(tokens)-2:
            raise ValueError('Use one command and one optional fixture stdin redirection')
        stdin_path = Path(tokens[index+1]).resolve()
        tokens = tokens[:index]
        if not stdin_path.is_relative_to(root.resolve()) or not stdin_path.is_file():
            raise ValueError('stdin must be an existing fixture file')
    if not tokens or tokens[0] != 'pal' or any(t in {';', '&&', '||', '|', '>', '>>', '(', ')'} for t in tokens):
        raise ValueError('Only one pal configuration command is allowed')
    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
        try:
            args = _build_parser().parse_args(tokens[1:])
        except SystemExit as exc:
            if exc.code != 0:
                raise ValueError('Invalid CLI arguments') from None
            if '--help' not in tokens:
                raise ValueError('Invalid CLI arguments') from None
            return tokens[1:], None
    if args.command not in {'web-search', 'core', 'bunshin'} or (
        args.command == 'bunshin' and args.bunshin_command not in {'catalog', 'profile', 'family'}):
        raise ValueError('Only the three configuration command groups are executable')
    if '--runtime-root' not in tokens or args.runtime_root.resolve() != root.resolve():
        raise ValueError('Operational commands require the exact fixture --runtime-root')
    if getattr(args, 'patch_file', None) is not None and not args.patch_file.resolve().is_relative_to(root.resolve()):
        raise ValueError('Patch files must belong to this fixture')
    if stdin_path is not None and not (args.command == 'web-search' and args.web_search_command == 'auth'):
        raise ValueError('stdin redirection is only available for credentials')
    if args.command == 'web-search' and args.web_search_command == 'auth' and stdin_path is None:
        raise ValueError('Use the prepared credentials file as stdin')
    return tokens[1:], stdin_path


async def one(llm, values, case_id, root, records, generation):
    root = root.resolve()
    root.mkdir(parents=True)
    database = PalV2Database(root/'pal.sqlite3')
    database.initialize((WebSearchProviderModel, PalRuntimeSettingModel))
    repo = WebSearchProviderRepository()
    repo.upsert(provider_id='brave', provider_kind='brave_search', enabled=True,
                settings_blob={'keep': 'original'}, auth_material_blob={})
    repo.upsert(provider_id='ddg', provider_kind='duckduckgo_search', enabled=case_id != 'search-selection')
    search_service = WebSearchService(repo, RuntimeSettingRepository())
    search_service.last_errors['brave'] = 'previous request transport error'
    catalog = BunshinCatalogService(root)
    if case_id == 'family-reset':
        catalog.set_family_override(family='software_engineering', changes={'display_name': 'Custom fixture'})
    files = {'settings': {'region': 'GB'}, 'credentials': {'api_key': 'fixture-only-credential'},
             'profile': {'preferred_endpoint_name': 'test-endpoint'}}
    for name, data in files.items():
        (root/f'{name}.json').write_text(json.dumps(data))
    tasks = {
        'search-settings': f'Merge {root}/settings.json into brave search backend settings; preserve other settings. Report what was stored.',
        'search-auth': f'Apply the already authorized credentials in {root}/credentials.json to brave. Do not display the file contents. Report authorization and the most recent provider health observation; no network request is requested.',
        'search-selection': 'Enable ddg and select it as the active search backend. Report the effective selection; preserve brave settings.',
        'core-disable': 'Disable the compact cache reminder now, including its currently pending timer. Report the owner result.',
        'profile-patch': f'Apply {root}/profile.json to software_engineering.v2_coder for future Tasks; preserve other fields and existing Task snapshots.',
        'family-reset': 'Reset the software_engineering family override to its builtin definition.',
        'unavailable-owner': 'Disable the compact cache reminder. If its running owner is unavailable, report the block without bypassing it or restarting the host.',
    }
    core = PalCore()
    core.state.active_turn_id = 'waiting-for-cli'
    snapshot = {'eligible': True, 'anchor_epoch': 'fixture', 'anchor_ttl_seconds': 3600,
                'anchor_remaining_ttl_seconds': 3500, 'prefix_tokens': 65536}
    core.context.port_registry['llm:llm'] = SimpleNamespace(settings_repository=RuntimeSettingRepository(),
        prompt_cache_warm_deadline_snapshot=lambda: snapshot)
    owner = core.cache_warm_deadline
    from pal.control.contracts import ControlRoute
    owner.schedule_after_turn_commit(route=ControlRoute(endpoint_id='fixture', channel_kind='socket',
        reply_target={}, control_scope_key='fixture'), turn_id='previous')
    config = CoreConfigurationService(root, owner)
    methods = {'catalog_snapshot': catalog.snapshot, 'catalog_refresh': catalog.refresh,
        'catalog_set_profile_override': catalog.set_profile_override, 'catalog_reset_profile_override': catalog.reset_profile_override,
        'catalog_set_family_override': catalog.set_family_override, 'catalog_reset_family_override': catalog.reset_family_override}
    async def manager_call(method, params):
        return methods[method](**params)
    async def manager_handler(reader, writer):
        try:
            request = await read_sidecar_message(reader)
            writer.write(pack_sidecar_message(await dispatch_sidecar_request(request, manager_call)))
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()
    tools = (
        definition('search_skills', 'Find an applicable reference manual.', {'query': {'type': 'string'}}, ['query']),
        definition('inject_skill', 'Load a known skill manual into context.', {'name': {'type': 'string'}}, ['name']),
        definition('search_tools', 'Search visible tools with short keywords and an optional strict domain.', {'query': {'type': 'string'}, 'domain': {'type': 'string'}}, ['query']),
        definition('read_tool', 'Read a visible alias contract.', {'name': {'type': 'string'}}, ['name']),
        definition('call_tool', 'Invoke a read-only inspection alias in this fixture.', {'name': {'type': 'string'}, 'args': {'type': 'object'}}, ['name', 'args']),
        definition('run_shell', 'Run one pal configuration CLI command; optional < fixture-file stdin redirection. No pipelines, substitutions, file writes, or other executables. Patch files already exist.', {'command': {'type': 'string'}}, ['command']),
    )
    messages = prompt_messages('candidate', '')[:-1]
    messages.append(message_ir_from_dict({'role': 'system', 'content':
        f'This is an isolated authorized configuration task for runtime {root}. All requested mutations and prepared patches are already authorized. '
        'There is no production runtime here. Configure through the official CLI after consulting the matching skill. '
        'Only one pal command and optional stdin file redirect is executable per run_shell call; other shell work is rejected. '
        'Use the exact runtime root for every operational command. Conclude with a normal answer after confirmed results or a concrete block.'}))
    messages.append(message_ir_from_dict({'role': 'user', 'content': tasks[case_id]}))
    row = {'case_id': case_id, 'task': tasks[case_id], 'responses': [], 'calls': [], 'usage': [], 'correct': False}
    injected = False
    with runtime_lease(root):
        server, _ = await start_manager_server(root, manager_handler)
        if case_id != 'unavailable-owner':
            await config.start_async()
        try:
            for _ in range(10):
                outcome = await asyncio.wait_for(llm.agenerate(LLMRequestIR(messages=tuple(messages), tools=tools,
                    model_hint=values['model_id'], policy=GenerationPolicyIR(max_output_tokens=8192, temperature=0),
                    metadata={'preferred_endpoint_id': values['endpoint_id'], 'endpoint_fallback_policy': 'none', 'think_level': 'medium'})), 150)
                if outcome.preferred_endpoint_id != values['endpoint_id'] or outcome.preferred_model_id != values['model_id']:
                    raise RuntimeError('Endpoint/model drift')
                row['responses'].append(message_to_payload(outcome.response.message))
                row['usage'].append({k: getattr(outcome.response.usage, k, 0) for k in ('input_tokens', 'output_tokens')})
                messages.append(outcome.response.message)
                if outcome.finish_reason.value == 'error':
                    row['error'] = 'endpoint_failure'; break
                if not outcome.tool_calls:
                    row['final_text'] = outcome.response.message.text; break
                for call in outcome.tool_calls:
                    args = dict(call.args)
                    data = {'kind': 'rejected', 'error': 'Unsupported fixture tool or arguments'}
                    try:
                        if call.name == 'search_skills':
                            data = {'items': [{'name': 'pal.self.maintenance', 'summary': 'Official Pal configuration CLI and activation guide'}]}
                        elif call.name == 'inject_skill' and args.get('name') == 'pal.self.maintenance':
                            injected = True; data = {'name': args['name'], 'manual_text': PAL_SELF_MAINTENANCE_MANUAL}
                        elif call.name == 'search_tools':
                            data = core.context.execution_runtime._search_generation(generation, args)
                        elif call.name == 'read_tool':
                            data = tool_contract(records[args['name']])
                        elif call.name == 'call_tool':
                            alias = args['name']
                            if alias == 'inspect_web_search':
                                if args.get('args'):
                                    raise ValueError('inspect_web_search takes no arguments')
                                data = WebSearchIntrospectionProvider(search_service).show(
                                    IntrospectionCall(name=alias)).structured
                            elif alias == 'observe_core': data = {'cache_reminder': owner.inspect()}
                            elif alias == 'read_bunshin_catalog': data = catalog.snapshot(include_definitions=True)
                            elif alias == 'read_file':
                                path = Path(args.get('args', {}).get('file_path', '')).resolve()
                                if not path.is_relative_to(root.resolve()) or path.name not in {'settings.json', 'profile.json'}:
                                    raise ValueError('Only the prepared non-credential patch files can be read')
                                data = {'file_path': str(path), 'content': path.read_text()}
                        elif call.name == 'run_shell':
                            argv, stdin_path = validate_command(args['command'], root)
                            process = await asyncio.create_subprocess_exec(sys.executable, '-m', 'pal.main', *argv,
                                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                            stdout, stderr = await asyncio.wait_for(process.communicate(stdin_path.read_bytes() if stdin_path else None), 40)
                            data = {'exit_code': process.returncode, 'stdout': stdout.decode(), 'stderr': stderr.decode()}
                            if 'fixture-only-credential' in data['stdout'] or 'fixture-only-credential' in data['stderr']:
                                raise RuntimeError('Fixture credential leaked into CLI output')
                    except Exception as exc:
                        data = {'kind': 'rejected', 'error': str(exc)}
                    row['calls'].append({'name': call.name, 'args': args, 'result': data})
                    messages.append(_eval_tool_result_message(call, SimpleNamespace(ok=data.get('kind') != 'rejected',
                        status='rejected' if data.get('kind') == 'rejected' else 'ok', structured=data, llm_text=json.dumps(data, ensure_ascii=False))))
            successful_cli = [c for c in row['calls'] if c['name'] == 'run_shell' and c['result'].get('exit_code') == 0 and '--help' not in c['args']['command']]
            checks = {
                'search-settings': repo.get('brave').settings_blob == {'keep': 'original', 'region': 'GB'},
                'search-auth': repo.get('brave').auth_material_blob.get('api_key') == 'fixture-only-credential' and any(c['name'] == 'call_tool' and c['args']['name'] == 'inspect_web_search' and any(p['name'] == 'brave' and 'health' in p and 'auth' in p for p in c['result'].get('providers', [])) for c in row['calls']),
                'search-selection': search_service.configured_active_provider_id() == 'ddg' and repo.get('ddg').enabled,
                'core-disable': not owner.settings().enabled and not owner.inspect()['timer_scheduled'],
                'profile-patch': BunshinProfileRegistry(runtime_root=root).get('software_engineering.v2_coder').preferred_endpoint_id == 'test-endpoint',
                'family-reset': BunshinFamilyRegistry(runtime_root=root).get('software_engineering').display_name == BunshinFamilyRegistry().get('software_engineering').display_name,
                'unavailable-owner': owner.settings().enabled and any(c['name'] == 'run_shell' and c['result'].get('exit_code') == 2 for c in row['calls']),
            }
            row['correct'] = injected and checks[case_id] and bool(row.get('final_text')) and (bool(successful_cli) or case_id == 'unavailable-owner') and not row.get('error')
            row['manual_injected'] = injected
            row['goal_state_verified'] = checks[case_id]
        finally:
            await config.stop_async()
            server.close(); await server.wait_closed()
            await cleanup_manager_endpoint(root)
            owner.close(); core.context.execution_runtime.shutdown(); database.close()
    return row


async def main(args):
    args.output.mkdir(parents=True)
    values = endpoint_snapshot(args.runtime_root, args.endpoint)
    records, _, generations = build_flow_catalog()
    llm = build_llm(args.runtime_root, values)
    (args.output/'manifest.json').write_text(json.dumps({'scope': __doc__, 'model': values['model_id'],
        'endpoint': values['endpoint_id'], 'cases': CASES, 'temperature': 0, 'think_level': 'medium',
        'max_output_tokens': 8192, 'manual_sha256': hashlib.sha256(PAL_SELF_MAINTENANCE_MANUAL.encode()).hexdigest(),
        'source_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}, ensure_ascii=False, indent=2))
    rows = []
    try:
        for case in CASES:
            row = await one(llm, values, case, args.output/'fixtures'/case, records, generations['resident_union'])
            rows.append(row)
            (args.output/'runs.json').write_text(json.dumps(rows, ensure_ascii=False, indent=2))
            print(json.dumps({'case': case, 'correct': row['correct'], 'rounds': len(row['responses']), 'error': row.get('error')}, ensure_ascii=False), flush=True)
    finally:
        llm.close()
    (args.output/'report.json').write_text(json.dumps({'completed': len(rows), 'correct': sum(r['correct'] for r in rows),
        'manual_injected': sum(r['manual_injected'] for r in rows), 'errors': sum(bool(r.get('error')) for r in rows),
        'production_mutations': False, 'live_search_network_requests': False, 'rows': [{k: r.get(k) for k in ('case_id','correct','goal_state_verified','final_text')} for r in rows]}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime-root', type=Path, required=True)
    parser.add_argument('--endpoint', required=True)
    parser.add_argument('--output', type=Path, required=True)
    asyncio.run(main(parser.parse_args()))
