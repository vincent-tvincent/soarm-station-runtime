"""Project payload-free local ROS tracing into service and action activity.

Service headlines count server request receipts plus response sends (handled
messages), never both client/server copies of a call. Phase counters remain
separate. Unmapped, inactive and lost observations must not become fake zeros.
"""
import copy
import hashlib
import json
import math
from collections import Counter,defaultdict

PHASES=('request_sent','request_received','response_sent','response_received')
ROLE_PHASES={'client':('request_sent','response_received'),'server':('request_received','response_sent')}
BUCKET_NS=500_000_000
HISTORY_BUCKETS=20


def _number(value,integer=False):
    return (not isinstance(value,bool) and isinstance(value,int if integer else (int,float))
            and value>=0 and (isinstance(value,int) or math.isfinite(value)))


def _ns(value):
    if _number(value,integer=True):return value
    if isinstance(value,str) and 0<len(value)<=24 and value.isascii() and value.isdecimal():return int(value)
    return None


def _identity(*parts):
    return 'rpc:'+hashlib.sha256(json.dumps(parts,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def _empty(reason,state='unavailable'):
    return {'state':state,'source':'lttng','sample_id':None,'messages':None,'rate_hz':None,
        'bytes':None,'bytes_per_second':None,'last_message_age_ms':None,'history':[],
        'history_clock_type':'collector_monotonic','counts_lower_bound':False,
        'reason':reason,'payload_export':False}


def _reason(metrics):
    return '; '.join(dict.fromkeys(m.get('reason','') for m in metrics if m.get('reason')))[:512]


def _sum_known(metrics,key):
    values=[m[key] for m in metrics if _number(m.get(key),integer=key=='messages')]
    if not values:return None
    total=sum(values)
    return total if total>0 or len(values)==len(metrics) else None


def _merge_history(components,target_capture):
    """Align durations inside each source clock, then bin on the target clock."""
    if target_capture is None:return []
    end=target_capture//BUCKET_NS;first=max(0,end-HISTORY_BUCKETS+1);buckets=defaultdict(int)
    for metric,source_capture,extra_age_ns in components:
        if source_capture is None:continue
        for row in metric.get('history',[])[-HISTORY_BUCKETS:]:
            stamp=_ns(row.get('at_ns'))
            if stamp is None or not _number(row.get('messages'),integer=True):continue
            age=source_capture-stamp+extra_age_ns
            if not 0<=age<HISTORY_BUCKETS*BUCKET_NS:continue
            aligned=target_capture-age
            if aligned<0:continue
            bucket=aligned//BUCKET_NS
            if first<=bucket<=end:buckets[bucket]+=row['messages']
    return [{'at_ns':str(bucket*BUCKET_NS),'messages':messages,'bytes':None}
            for bucket,messages in sorted(buckets.items())]


def _combine(metrics,identity,source_capture,*,missing=False):
    if not metrics:return _empty('No mapped traced endpoints')
    states=[m.get('state') for m in metrics]
    complete=not missing and all(state=='watching' for state in states)
    measured=any(state in ('watching','partial') for state in states)
    state='watching' if complete else 'partial' if measured else 'inactive' if all(s=='inactive' for s in states) else 'unavailable'
    result=_empty(_reason(metrics),state)
    result.update(sample_id=identity,messages=_sum_known(metrics,'messages'),rate_hz=_sum_known(metrics,'rate_hz'),
        counts_lower_bound=missing or any(m.get('counts_lower_bound',False) for m in metrics))
    if missing:
        if result['messages']==0:result['messages']=None
        if result['rate_hz']==0:result['rate_hz']=None
        result['reason']='Some discovered endpoints have no ready trace mapping'
    ages=[m['last_message_age_ms'] for m in metrics if _number(m.get('last_message_age_ms'))]
    result['last_message_age_ms']=min(ages) if ages else None
    result['history']=_merge_history([(m,source_capture,0) for m in metrics],source_capture)
    return result


def _context(trace,active,age_ms):
    trace=trace if isinstance(trace,dict) else {}
    state=trace.get('state','unavailable')
    if not active:state='inactive'
    epoch=trace.get('epoch')
    if not isinstance(epoch,str) or not epoch or len(epoch)>128:epoch=None
    capture=_ns(trace.get('captured_at_ns'))
    loss=trace.get('loss') if isinstance(trace.get('loss'),dict) else {}
    # Loss totals may predate this counter epoch. The collector scopes this
    # flag to the active epoch; absent certainty must not become a fake zero.
    bound=trace.get('counts_lower_bound')
    counts_lower_bound=bound if type(bound)==bool else True
    reason=str(trace.get('reason') or ('No active graph watch lease' if not active else ''))[:512]
    if state in ('ready','partial') and (epoch is None or capture is None):
        state='unavailable';reason='Trace snapshot has no valid counter epoch/capture'
    clock_id=trace.get('mapping_epoch')
    if not isinstance(clock_id,str) or not clock_id or len(clock_id)>128:clock_id=epoch
    return {'state':state,'epoch':epoch,'capture':capture,'clock_id':clock_id,
        'reason':reason,'loss':{key:loss.get(key) if _number(loss.get(key),integer=True) else None for key in ('events','packets','unmapped')},
        'lower_bound':counts_lower_bound,'age_ms':age_ms if _number(age_ms) else 0.,'trace':trace}


def _channel(raw,endpoint,phase,ctx,mapping_ready):
    state=ctx['state']
    if state not in ('ready','partial'):
        return _empty(ctx['reason'] or 'Trace collector is not ready','inactive' if state=='inactive' else 'unavailable')
    if not mapping_ready:return _empty('Endpoint instrumentation/mapping is not ready')
    if not isinstance(raw,dict) or not _number(raw.get('messages'),integer=True):
        return _empty('Trace phase counter is unavailable')
    result=_empty('','watching')
    lower=ctx['lower_bound'] or raw.get('loss_in_window') is True
    result.update(sample_id=_identity(ctx['epoch'],endpoint,phase),messages=raw['messages'],
        counts_lower_bound=lower,clock_id=ctx['clock_id'],captured_at_ns=str(ctx['capture']))
    if lower:
        result['state']='partial';result['reason']='Trace loss: cumulative counts are lower bounds'
        if result['messages']==0:result['messages']=None
    observed=raw.get('observed_window_s');rate=raw.get('rate_hz')
    if _number(observed) and observed>0 and raw.get('loss_in_window') is False and _number(rate):
        result['rate_hz']=rate
    result['observed_window_s']=observed if _number(observed) else None
    result['window_s']=raw.get('window_s') if _number(raw.get('window_s')) else None
    result['loss_in_window']=raw.get('loss_in_window') if type(raw.get('loss_in_window'))==bool else None
    result['rate_history_overflow']=raw.get('rate_history_overflow') is True
    if result['rate_history_overflow']:
        result['state']='partial';result['reason']='Rate history capacity reached; cumulative receipts remain measured'
    age=raw.get('last_event_age_ms')
    result['last_message_age_ms']=age+ctx['age_ms'] if _number(age) else None
    for row in (raw.get('history') if isinstance(raw.get('history'),list) else [])[-HISTORY_BUCKETS:]:
        if not isinstance(row,dict):continue
        stamp=_ns(row.get('at_ns'))
        if (stamp is not None and stamp<=ctx['capture'] and ctx['capture']-stamp<HISTORY_BUCKETS*BUCKET_NS
                and _number(row.get('messages'),integer=True) and (row['messages']>0 or not lower)):
            result['history'].append({'at_ns':str(stamp),'messages':row['messages'],'bytes':None})
    return result


def service_activity(service,trace,*,active=True,age_ms=0.):
    """Normalize exact local trace endpoints for one discovered service."""
    ctx=_context(trace,active,age_ms);name=service['name']
    all_endpoints=ctx['trace'].get('endpoints',[])
    all_endpoints=all_endpoints if isinstance(all_endpoints,list) else []
    ids=Counter(e.get('id') for e in all_endpoints if isinstance(e,dict) and isinstance(e.get('id'),str))
    endpoints=[];invalid=0
    for endpoint in all_endpoints[:1024]:
        if not isinstance(endpoint,dict) or endpoint.get('service')!=name:continue
        identifier=endpoint.get('id');role=endpoint.get('role');fqn=endpoint.get('node_fqn')
        if not isinstance(identifier,str) or not identifier or len(identifier)>256 or ids[identifier]!=1 or role not in ROLE_PHASES:
            invalid+=1;continue
        mapped=isinstance(fqn,str) and fqn.startswith('/') and len(fqn)<=512 and endpoint.get('mapping_ready',True) is True
        ready=mapped and endpoint.get('ready') is True and ctx['state'] in ('ready','partial')
        channels=endpoint.get('channels') if isinstance(endpoint.get('channels'),dict) else {}
        values={phase:_channel(channels.get(phase),identifier,phase,ctx,ready) for phase in ROLE_PHASES[role]}
        endpoints.append({'id':identifier,'node_id':'node:'+fqn if mapped else None,
            'node_fqn':fqn if mapped else None,'role':role,
            'state':_combine(list(values.values()),_identity(ctx['epoch'],identifier),ctx['capture'])['state'],
            'pid':endpoint.get('pid') if _number(endpoint.get('pid'),integer=True) else None,
            'mapping_ready':mapped and endpoint.get('mapping_ready',True) is True,
            'ready':ready,'channels':values})
    endpoints.sort(key=lambda e:e['id'])
    expected={role:set(service.get('clients' if role=='client' else 'servers',[])) for role in ROLE_PHASES}
    mapped={role:{e['node_id'] for e in endpoints if e['role']==role and e['ready']} for role in ROLE_PHASES}
    missing={role:sorted(expected[role]-mapped[role]) for role in ROLE_PHASES}
    channels={}
    for phase in PHASES:
        role='client' if phase in ROLE_PHASES['client'] else 'server'
        values=[e['channels'][phase] for e in endpoints if e['role']==role]
        identity=_identity(ctx['epoch'],name,phase,[(e['id'],e['channels'][phase].get('sample_id')) for e in endpoints if e['role']==role])
        channels[phase]=_combine(values,identity,ctx['capture'],
            missing=bool(missing[role]) or invalid>0 or ctx['trace'].get('mapping_limited') is True)
        if not values:
            channels[phase]['reason']=ctx['reason'] or 'No mapped traced '+role+' endpoints'
            if ctx['state']=='inactive':channels[phase]['state']='inactive'
    requests,responses=channels['request_received'],channels['response_sent']
    headline=_combine([requests,responses],_identity(ctx['epoch'],name,'server_messages',requests.get('sample_id'),responses.get('sample_id')),ctx['capture'])
    headline.update(measurement_scope='local_traced_service_server_messages',
        count_semantics='server_request_receipts_plus_response_sends',requests=requests,responses=responses,
        channels=channels,endpoints=endpoints,clock_id=ctx['clock_id'],
        captured_at_ns=str(ctx['capture']) if ctx['capture'] is not None else None,
        snapshot_age_ms=ctx['age_ms'],coverage={'scope':'local_traced_processes','provider_state':ctx['state'],
            'unmapped_servers':missing['server'],'unmapped_clients':missing['client'],
            'invalid_endpoints':invalid,'provider_endpoint_limit_exceeded':len(all_endpoints)>1024,
            'mapping_limited':ctx['trace'].get('mapping_limited') is True,
            'loss':ctx['loss'],'counts_lower_bound':ctx['lower_bound']})
    return headline


def project_rpc_activity(services,actions,trace,*,active,observer_capture_ns,observer_wall_ns,age_ms=0.):
    """Mutate only additive activity projections on already-copied graph objects."""
    ctx=_context(trace,active,age_ms);by_name={}
    for service in services:
        service['activity']=service_activity(service,trace,active=active,age_ms=age_ms)
        by_name[service['name']]=service['activity']
    for action in actions:
        legacy=action.setdefault('activity',{})
        rpc={phase:copy.deepcopy(by_name.get(action['name']+'/_action/'+suffix,
             _empty('Action RPC service is not discovered')))
             for phase,suffix in (('goal','send_goal'),('cancel','cancel_goal'),('result','get_result'))}
        rpc_metrics=list(rpc.values())
        rpc['state']=_combine(rpc_metrics,_identity(ctx['epoch'],action['name'],'rpc'),ctx['capture'])['state']
        legacy['rpc']=rpc
        legacy['requests']={'state':rpc['state'],'measurement_scope':'separate_goal_cancel_result_services',
            'reason':'Goal, cancel and result request/response observations are separated in rpc'}
        publications=[legacy.get('status',_empty('Status topic unavailable')),
                      legacy.get('feedback',_empty('Feedback topic unavailable'))]
        contributors=publications+rpc_metrics
        identity=_identity(action['name'],'display',[m.get('sample_id') for m in contributors])
        display=_combine(contributors,identity,None)
        display.update(source='combined_observer_metadata',measurement_scope='action_publications_and_server_rpc_messages',
            count_semantics='status_plus_feedback_plus_server_rpc_request_response_messages',
            history_clock_type='observer_monotonic',
            history=_merge_history(
                [(m,observer_wall_ns,0) for m in publications]+
                [(m,ctx['capture'],int(ctx['age_ms']*1e6)) for m in rpc_metrics],observer_capture_ns))
        legacy['display']=display
    return {'source':'lttng','state':ctx['state'],'epoch':ctx['epoch'],
        'mapping_epoch':ctx['clock_id'],'clock_type':'collector_monotonic',
        'captured_at_ns':str(ctx['capture']) if ctx['capture'] is not None else None,
        'snapshot_age_ms':ctx['age_ms'],'reason':ctx['reason'],'loss':ctx['loss'],
        'counts_lower_bound':ctx['lower_bound'],'mapping_limited':ctx['trace'].get('mapping_limited') is True,
        'measurement_scope':'local_traced_processes',
        'payload_export':False}
