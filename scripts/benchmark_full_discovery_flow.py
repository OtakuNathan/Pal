"""Autonomous discovery and final tool/mode selection across the authored catalog.

The purpose-derived cases lack business arguments. Final select_tool is an
EVALUATION-ONLY decision, not a business invocation. Discovery uses the production
matcher; mode/alias rejections use the production resolver. Static resident and
role unions are separate. They do not reconstruct live availability or a single
role's permissions. Dynamic MCP/external plugins and business success are excluded.
"""
from __future__ import annotations
import argparse
import ast
import asyncio
import hashlib
import importlib
import json
from pathlib import Path
import random
import statistics
import sys
import traceback
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.benchmark_full_discovery import build_catalog
from scripts.benchmark_discovery_flow import prompt_messages
from scripts.replay_purpose_discovery import load_matcher
from pal.core import PalCore
from pal.execution.runtime import ExecutionRuntime
from pal.execution.tool_facade import InvocationMode, RejectedResult
from pal.execution.tool_presentation import compact_input_contract
from pal.shared.tool_protocol import new_tool_call

ROOT = Path(__file__).resolve().parents[1]
NEGATIVES = [
 ('negative-git', 'Find a dedicated Git repository status tool. Do not substitute shell execution.', None),
 ('negative-sql', 'Find a dedicated SQL database query tool. Do not substitute shell execution or LLM configuration inspection.', None),
 ('negative-kubernetes', 'Find a dedicated Kubernetes cluster health tool. Do not substitute shell execution or browser health.', None),
 ('negative-channel-delete', 'Find a dedicated tool to delete an already delivered channel attachment. Sending a file does not satisfy this request.', None),
 ('negative-plugin-backup', 'Find a dedicated tool to back up plugin application data. Do not substitute install, uninstall or reload.', None),
 ('negative-checkbox-delete', 'Find a dedicated tool to remove a checkbox DOM element. Do not substitute changing its checked state or generic code execution.', None),
 ('negative-llm-delete', 'Find a dedicated tool to permanently delete an LLM endpoint configuration. Disabling or switching it does not satisfy the request.', None),
 ('negative-profile-delete', 'Find a dedicated tool to permanently delete a builtin Bunshin profile definition. Resetting an override does not satisfy the request.', None),
 ('negative-memory-send', 'Find a memory-domain tool that sends an email attachment. Do not substitute channel or shell tools.', None),
 ('negative-artifact-restart', 'Find an artifact-domain tool that restarts a channel endpoint. Do not substitute a channel or plugin tool.', None),
]
PROBES = [
 ('probe-typo', "Find the tool to inspect browser state. First search with domain='browser', query='inspectt status', then follow the returned discovery information.", 'inspect_browser_status'),
 ('probe-domain', "Find the tool that reloads an enabled plugin's Python implementation. First search with domain='lsp', query='reload plugin', then follow the returned discovery information.", 'manage_plugin'),
 ('probe-mode', "Select the plugin inventory tool. First use select_tool(name='list_plugins', invocation_mode='direct'), then correct your decision from the result.", 'list_plugins'),
 ('probe-unknown-alias', "Find the tool for the currently active LLM endpoint. First use select_tool(name='llm_status', invocation_mode='indirect'), then follow the result to find a real tool.", 'inspect_llm'),
]


def _ast_value(node, module):
    if isinstance(node, ast.Name):
        return getattr(module, node.id)
    if isinstance(node, ast.Attribute):
        return getattr(_ast_value(node.value, module), node.attr)
    return ast.literal_eval(node)


def build_flow_catalog():
    declarations, records, cases, _ = build_catalog()
    trees = {}
    for declaration in declarations:
        alias = declaration.alias
        if records[alias].get('input_contract') is not None:
            continue  # Same first-provider choice as the original catalog.
        if declaration.module_id == 'emotion':
            from pal.emotion.runtime import emotion_contract
            model, _ = emotion_contract()
            records[alias].update(input_contract=compact_input_contract(model.model_json_schema()),
                                  invocation_mode='direct', failure_next_steps=declaration.guidance.failure_next_steps)
            continue
        if declaration.generated_spec_key:
            from pal.bunshin.scoped_execution import _WORKSPACE_TOOL_SPECS
            spec = _WORKSPACE_TOOL_SPECS[declaration.generated_spec_key]
            records[alias].update(
                input_contract=compact_input_contract(spec['InputModel'].model_json_schema()),
                invocation_mode='direct',
                failure_next_steps=declaration.guidance.failure_next_steps,
            )
            continue
        path = ROOT/declaration.source
        if path not in trees:
            trees[path] = [n for n in ast.walk(ast.parse(path.read_text()))
                           if (isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                               and n.func.id == 'capability_action')
                           or (isinstance(n, ast.Dict) and any(isinstance(k, ast.Constant)
                               and k.value == 'alias' for k in n.keys))]
        node = next(n for n in trees[path] if n.lineno == declaration.line)
        module = importlib.import_module('.'.join(path.relative_to(ROOT/'src').with_suffix('').parts))
        if isinstance(node, ast.Call):
            fields = {k.arg:k.value for k in node.keywords}
        else:
            fields = {k.value:v for k,v in zip(node.keys,node.values) if isinstance(k,ast.Constant)}
        from pal.execution.tool_facade import EmptyToolInput
        model = _ast_value(fields['InputModel'],module) if 'InputModel' in fields else EmptyToolInput
        schema = compact_input_contract(model.model_json_schema())
        if 'scope' in fields and _ast_value(fields['scope'],module)=='endpoint':
            # Published endpoint aliases add their target selector at compile time.
            schema.setdefault('properties',{}).setdefault('name', {'type':'string','description':'Endpoint name.'})
            schema['required'] = sorted(set(schema.get('required',[])) | {'name'})
        if declaration.role_scoped:
            mode = 'direct'  # scoped_execution._tool_descriptor uses DIRECT.
        elif 'execution' in fields:
            mode = _ast_value(fields['execution'],module).invocation_mode.value
        elif declaration.namespace == 'introspection':
            mode = 'indirect'  # capability_compiler._default_execution.
        else:
            raise ValueError(f'Missing authored execution semantics: {alias}')
        records[alias].update(input_contract=schema,invocation_mode=mode,
            failure_next_steps=declaration.guidance.failure_next_steps)
    generations = {}
    for group in ('resident_union','role_union'):
        selected = {c['alias']:records[c['alias']] for c in cases if c['group']==group}
        modes = {alias:SimpleNamespace(alias=alias,execution=SimpleNamespace(invocation_mode=InvocationMode(r['invocation_mode']))) for alias,r in selected.items()}
        generations[group] = SimpleNamespace(search_records=selected,
            direct_aliases={a:r for a,r in modes.items() if r.execution.invocation_mode==InvocationMode.DIRECT},
            indirect_aliases={a:r for a,r in modes.items() if r.execution.invocation_mode==InvocationMode.INDIRECT})
    for c in cases:
        c['category']='catalog'
    extras = [dict(alias=expected,case_id=key,task=task,group='resident_union',category=category)
              for category,source in [('negative',NEGATIVES),('probe',PROBES)] for key,task,expected in source]
    return records, cases+extras, generations


def tool_contract(record):
    return {key:value for key,value in record.items() if key in {
        'alias','domain','purpose','use_when','do_not_use_when','failure_next_steps',
        'invocation_mode','input_contract','module_id','namespace','family'}}


def manifest_prompt(messages):
    """Transport message IDs must not invalidate identical prompt snapshots."""
    from pal.llm.serde import message_to_payload
    return [{k:v for k,v in message_to_payload(m).items() if k!='message_id'} for m in messages]


def resolve_selection(generation,name,mode):
    if not isinstance(name,str) or not name.strip() or mode not in {'direct','indirect'}:
        return {'kind':'rejected','error_code':'invalid_arguments','effect':'not_started',
                'usage_hint':'select_tool requires an exact name and direct/indirect invocation_mode.'}
    result = ExecutionRuntime._resolve_invocation_record(generation,new_tool_call(name=name,args={}),
        invocation_mode=InvocationMode(mode))
    if isinstance(result,RejectedResult):
        payload = result.model_dump(mode='json')
        # Keep the real resolver's alias/mode facts and affordances. There is no
        # business handler. The evaluator's decision tool records corrected mode.
        if payload['error_code']=='wrong_invocation_mode':
            payload['selection_retry']={'name':name,'invocation_mode':payload['details']['correct_invocation_mode']}
        return payload
    return {'kind':'complete','selected_name':name,'invocation_mode':mode}


def summarize(rows,cases):
    result={}
    for category in ('catalog','negative','probe'):
        for group in ('all','resident_union','role_union'):
            subset=[r for r in rows if r['category']==category and (group=='all' or r['group']==group)]
            expected=sum(c['category']==category and (group=='all' or c['group']==group) for c in cases)
            if not expected:
                continue
            result[f'{category}:{group}']=dict(completed=len(subset),expected=expected,
                final_correct=sum(bool(r.get('correct')) for r in subset),
                first_search_recall=sum(bool(r.get('first_search_recall')) for r in subset),
                first_contract_recall=sum(bool(r.get('first_contract_recall')) for r in subset),
                first_contract_top1=sum(bool(r.get('first_contract_top1')) for r in subset),
                first_contract_mrr=statistics.mean(r.get('first_contract_rr', 0) for r in subset) if subset else 0,
                first_discovery_visible=sum(bool(r.get('first_discovery_visible')) for r in subset),
                first_selection_correct=sum(bool(r.get('first_selection_correct')) for r in subset),
                wrong_selections=sum('selected_name' in r and not r.get('correct') for r in subset),
                abstentions=sum(bool(r.get('abstained')) for r in subset),
                errors=sum(bool(r.get('error')) for r in subset),
                directory_seen=sum(any(s.get('directory') for s in r.get('searches',[])) for r in subset),
                rejection_runs=sum(bool(r.get('rejections')) for r in subset),
                recovered_runs=sum(bool(r.get('rejections')) and bool(r.get('correct')) for r in subset),
                mean_rounds=statistics.mean(len(r.get('responses',[])) for r in subset) if subset else 0,
                mean_contract_reads=statistics.mean(len(r.get('reads', [])) for r in subset) if subset else 0,
                search_response_chars=sum(s.get('response_chars', 0) for r in subset for s in r.get('searches', [])),
                max_search_response_chars=max((s.get('response_chars', 0) for r in subset for s in r.get('searches', [])), default=0),
                input_tokens=sum(u['input_tokens'] for r in subset for u in r.get('usage',[])),
                output_tokens=sum(u['output_tokens'] for r in subset for u in r.get('usage',[])))
    return result


async def run_case(llm,values,case,generation,search,schema,prompt,*,max_output_tokens=8192,discovery_description=None):
    from jsonschema import Draft202012Validator
    from pal.llm.conversions import message_ir_from_dict,tool_definition_ir_from_dict
    from pal.llm.ir import GenerationPolicyIR,LLMRequestIR
    from pal.llm.serde import message_to_payload
    from pal.eval_tools import _eval_tool_result_message
    row=dict(**case,searches=[],reads=[],usage=[],responses=[],rejections=[],correct=False)
    definitions={
        'search_tools':{'type':'function','function':{'name':'search_tools','description':
            discovery_description or 'Choose the strict owner domain and describe the task in query. Purpose and tool-authored auxiliary terms/intents rank up to three full contracts by default; action/object naming is not required. A candidate is not proof that every requested feature exists: compare its complete contract and unmatched_terms with the original request before invoking. Exact aliases remain supported. On a lexical miss, spelling recovery can return suggestions with full contracts. Domain alone or directory_offset browses up to 16 names and brief purposes; read a plausible directory contract. Domains stay strict; omit domain only if the owner is unknown. Do not search indefinitely for this capability. Try a reasonable change of query or owner and inspect relevant inventory if useful. If those checks reveal no suitable callable, it may be unavailable; explain that possibility and any uncertainty. Reuse unchanged inventory and complete contracts.', 'parameters':schema}},
        'read_tool':{'type':'function','function':{'name':'read_tool','description':'Read an exact visible alias contract.',
            'parameters':{'type':'object','properties':{'name':{'type':'string'}},'required':['name'],'additionalProperties':False}}},
        'select_tool':{'type':'function','function':{'name':'select_tool','description':
            'EVALUATION ONLY: submit the final tool name and its direct/indirect invocation_mode for the original task. No business arguments are needed and no business tool executes. A valid decision ends the case; rejected mode/name decisions may be corrected.',
            'parameters':{'type':'object','properties':{'name':{'type':'string','minLength':1,'pattern':r'\S'},'invocation_mode':{'type':'string','enum':['direct','indirect']}},'required':['name','invocation_mode'],'additionalProperties':False}}},
    }
    if 'directory_offset' not in schema['properties']:
        definitions['search_tools']['function']['description']='Search for tools with short action/object words and an optional strict domain. Positive purpose text supplies relevance. Review fuzzy suggestions; shorten or list tools on misses.'
    messages=list(prompt)
    messages.append(message_ir_from_dict({'role':'system','content':
        'This is an isolated discovery/selection evaluation. Search, browse or read contracts as useful, then '
        'submit your final choice with select_tool. The original task is to identify a tool, not execute it. '
        'Do not invent business arguments or ask for missing task IDs. If the requested dedicated capability '
        'does not exist, state that and do not select a substitute. No extra retry hint will be supplied. '
        f'The visible surface for this case is {case["group"]}; role_union is a synthetic union of authored role tools, not resident tools.'}))
    messages.append(message_ir_from_dict({'role':'user','content':case['task']}))
    validators={name:Draft202012Validator(spec['function']['parameters']) for name,spec in definitions.items()}
    for _ in range(6):
        outcome=await asyncio.wait_for(llm.agenerate(LLMRequestIR(messages=tuple(messages),
            tools=tuple(tool_definition_ir_from_dict(s) for s in definitions.values()),model_hint=values['model_id'],
            policy=GenerationPolicyIR(max_output_tokens=max_output_tokens,temperature=0),
            metadata={'preferred_endpoint_id':values['endpoint_id'],'endpoint_fallback_policy':'none',
                      'think_level':'medium','case_id':case.get('case_id',case['alias'])})),timeout=150)
        row['usage'].append({k:getattr(outcome.response.usage,k,0) for k in ('input_tokens','output_tokens','reported')})
        if outcome.finish_reason.value=='error':
            metadata=outcome.response.message.metadata
            row['error']='endpoint_failure'
            row['endpoint_failure']={key:metadata.get(key) for key in ('failure_kind','error_type','failure_subsystem')}
            row['responses'].append({'role':'assistant','finish_reason':'error','endpoint_failure':row['endpoint_failure']})
            return row
        if outcome.preferred_endpoint_id!=values['endpoint_id'] or outcome.preferred_model_id!=values['model_id']:
            raise RuntimeError('Endpoint/model drift')
        row['responses'].append(message_to_payload(outcome.response.message))
        messages.append(outcome.response.message)
        calls=list(outcome.tool_calls or ())
        if not calls:
            row['abstained']=True
            row['correct']=case['alias'] is None and bool(row['searches'] or row.get('inventory_seen')) and bool(outcome.response.message.text.strip())
            return row
        for call in calls:
            name=call.name;args=dict(call.args)
            if name not in definitions or not validators[name].is_valid(args):
                data={'kind':'rejected','error_code':'invalid_arguments','effect':'not_started',
                    'usage_hint':'Use one of the exposed discovery/selection tools with its declared schema.'}
                row['rejections'].append(dict(call=name,args=args,result=data))
            elif name=='search_tools':
                data=search(generation,args)
                # Strip index metadata but retain callable contract evidence.
                data['hits']=[{k:v for k,v in h.items() if k not in {'search_text','input_shape'}} for h in data['hits']]
                for suggestion in data.get('suggestions', []):
                    if 'contract' in suggestion:
                        suggestion['contract'] = {k: v for k, v in suggestion['contract'].items()
                                                  if k not in {'search_text', 'input_shape'}}
                contracts = [h['alias'] for h in data['hits']] + [s['alias'] for s in data.get('suggestions', []) if s.get('contract')]
                row['searches'].append(dict(args=args,hits=[h['alias'] for h in data['hits']],
                    suggestions=[s['alias'] for s in data.get('suggestions',[])],directory=data.get('directory'),available_domains=data.get('available_domains'),
                    contracts=contracts,
                    response_chars=len(json.dumps(data, ensure_ascii=False))))
                if len(row['searches'])==1:
                    row['first_search_recall']=case['alias'] in row['searches'][0]['hits']
                    position = contracts.index(case['alias']) + 1 if case['alias'] in contracts else 0
                    row['first_contract_recall'] = bool(position)
                    row['first_contract_top1'] = position == 1
                    row['first_contract_rr'] = 1 / position if position else 0
                    row['first_discovery_visible']=case['alias'] is not None and (
                        row['first_contract_recall'] or any(t['alias']==case['alias'] for t in (data.get('directory') or {}).get('tools', [])))
            elif name=='read_tool':
                row['reads'].append(args['name'])
                record=generation.search_records.get(args['name'])
                data=tool_contract(record) if record else {'kind':'rejected','error_code':'unknown_tool','effect':'not_started',
                    'affordances':[{'tool':'search_tools','arguments':{'query':args['name']},'reason':'Search visible aliases.'}]}
            else:
                data=resolve_selection(generation,args['name'],args['invocation_mode'])
                if 'first_selection_correct' not in row:
                    row['first_selection_correct']=data['kind']=='complete' and args['name']==case['alias']
                if data['kind']=='complete':
                    row.update(selected_name=args['name'],selected_mode=args['invocation_mode'],correct=args['name']==case['alias'])
                    return row  # No correctness oracle is fed back for a wrong, valid alias.
                row['rejections'].append(dict(call=name,args=args,result=data))
            messages.append(_eval_tool_result_message(call,SimpleNamespace(
                ok=data.get('kind')!='rejected',status='rejected' if data.get('kind')=='rejected' else 'ok',
                structured=data,llm_text=json.dumps(data,ensure_ascii=False))))
    row['error']='round_limit'
    return row


async def main(args):
    from pal.execution.generated_tool_models import ExecutionToolSearchExecutionDiscoveryCapabilityMixinSearchInput as SearchInput
    from pal.llm.serde import message_to_payload
    values=__import__('pal.tool_efficiency_benchmark',fromlist=['endpoint_snapshot']).endpoint_snapshot(args.runtime_root,args.endpoint)
    from pal.tool_efficiency_benchmark import build_llm
    records,cases,generations=build_flow_catalog()
    if args.category!='all':cases=[c for c in cases if c['category']==args.category]
    selected_ids = set(args.case_id or ())
    if selected_ids:
        known = {c.get('case_id', c['alias']) for c in cases}
        if selected_ids - known:
            raise ValueError('Unknown case IDs: ' + ', '.join(sorted(selected_ids - known)))
        cases = [c for c in cases if c.get('case_id', c['alias']) in selected_ids]
    if args.repetitions > 1:
        cases = [{**c, 'repetition': repetition} for repetition in range(args.repetitions) for c in cases]
    schemas={'current':SearchInput.model_json_schema()}
    schemas['before']=json.loads(json.dumps(schemas['current']))
    schemas['before']['properties'].pop('directory_offset',None)
    for schema in schemas.values():
        for key in ('namespace','family','module_name','tags','facets'):
            schema['properties'].pop(key,None)
    prior=json.loads((args.baseline/'prior-fragments.json').read_text())
    prompts={v:prompt_messages('candidate' if v=='current' else 'prior_prompt','',prior,None)[:-1] for v in ('before','current')}
    searches={'current':ExecutionRuntime._search_generation,'before':load_matcher(args.baseline/'prior-runtime.py')}
    variants=['before','current'] if args.variant=='paired' else [args.variant]
    args.output.mkdir(parents=True,exist_ok=args.resume)
    manifest=dict(scope=__doc__,model=values['model_id'],endpoint=values['endpoint_id'],cases=cases,
        variants=variants,max_output_tokens=args.max_output_tokens,catalog_sha256=hashlib.sha256(json.dumps(records,sort_keys=True).encode()).hexdigest(),
        schemas=schemas,prompts={v:manifest_prompt(p) for v,p in prompts.items()},
        source_sha256={str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in [Path(__file__),ROOT/'src/pal/execution/runtime.py',args.baseline/'prior-runtime.py']})
    if args.resume:
        if json.loads((args.output/'manifest.json').read_text())!=json.loads(json.dumps(manifest)):
            raise ValueError('Cannot resume with changed cases/catalog/prompt/model/source')
    else:(args.output/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2))
    (args.output/'catalog.json').write_text(json.dumps(records,ensure_ascii=False,indent=2))
    rows=json.loads((args.output/'runs.json').read_text()) if args.resume else []
    semaphore=asyncio.Semaphore(args.concurrency)
    llm=build_llm(args.runtime_root,values)
    stopped=False;failures=0
    def save():
        (args.output/'runs.json').write_text(json.dumps(rows,ensure_ascii=False,indent=2))
        report=dict(model=values['model_id'],endpoint=values['endpoint_id'],stopped=stopped,
            metrics={v:summarize([r for r in rows if r['variant']==v],cases) for v in variants})
        (args.output/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
    async def one(variant,case):
        nonlocal failures,stopped
        key=case.get('case_id',case['alias'])
        if any(r['variant']==variant and r['case_id']==key and r.get('repetition',0)==case.get('repetition',0) for r in rows):return
        async with semaphore:
            if stopped:return
            try:
                row=await run_case(llm,values,case,generations[case['group']],searches[variant],schemas[variant],prompts[variant],max_output_tokens=args.max_output_tokens)
                if row.get('error')=='endpoint_failure':failures+=1
                else:failures=0
            except Exception as exc:
                row=dict(**case,error=type(exc).__name__,failure_frames=[{'file':f.filename, 'line':f.lineno, 'function':f.name} for f in traceback.extract_tb(exc.__traceback__)[-6:]]);failures+=1
            if failures>=3:stopped=True
            row.update(variant=variant,case_id=key);rows.append(row);save()
            print(json.dumps(dict(completed=len(rows),variant=variant,case=key,correct=row.get('correct'),error=row.get('error')),ensure_ascii=False),flush=True)
    try:
        await asyncio.gather(*(one(v,cases[0]) for v in variants))
        if not stopped:
            rng=random.Random(20261012);pending=[(v,c) for c in cases[1:] for v in variants];rng.shuffle(pending)
            await asyncio.gather(*(one(v,c) for v,c in pending))
    finally:save();llm.close()

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime-root',type=Path,required=True)
    parser.add_argument('--endpoint',required=True)
    parser.add_argument('--baseline',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--variant',choices=['before','current','paired'],default='paired')
    parser.add_argument('--category',choices=['all','catalog','negative','probe'],default='all')
    parser.add_argument('--concurrency',type=int,default=4)
    parser.add_argument('--max-output-tokens',type=int,default=8192)
    parser.add_argument('--resume',action='store_true')
    parser.add_argument('--case-id',action='append',help='Fixed authored alias or negative/probe case ID; repeat to select several.')
    parser.add_argument('--repetitions',type=int,default=1)
    asyncio.run(main(parser.parse_args()))
