import copy
import json

from soarm_station.graph_rpc import project_rpc_activity,service_activity


def metric(messages=0,rate=0.,*,stamp='9500000000',loss=False):
    return {'messages':messages,'rate_hz':rate,'observed_window_s':5.,'window_s':5.,
        'loss_in_window':loss,'last_event_age_ms':20. if messages else None,
        'history':[{'at_ns':stamp,'messages':messages}]}


def trace(service='/query',*,request=5,response=4):
    return {'schema_version':1,'source':'lttng','state':'ready','active':True,
        'epoch':'counter-one','mapping_epoch':'collector-one','clock_type':'collector_monotonic',
        'captured_at_ns':'10000000000','reason':'','loss':{'events':0,'packets':0},'counts_lower_bound':False,
        'endpoints':[
            {'id':'server-one','service':service,'role':'server','node_fqn':'/server','pid':12,
             'ready':True,'mapping_ready':True,'channels':{'request_received':metric(request,request/5),
                'response_sent':metric(response,response/5)}},
            {'id':'client-one','service':service,'role':'client','node_fqn':'/client','pid':13,
             'ready':True,'mapping_ready':True,'channels':{'request_sent':metric(7,1.4),
                'response_received':metric(3,.6)}}]}


def service(name='/query'):
    return {'name':name,'servers':['node:/server'],'clients':['node:/client']}


def test_service_separates_phases_and_headline_counts_server_messages_once():
    source=trace();source['endpoints'][0]['request']={'secret':'do-not-export'}
    before=copy.deepcopy(source);value=service_activity(service(),source)
    assert value['state']=='watching'
    assert value['messages']==9 and value['rate_hz']==1.8
    assert value['requests']['messages']==5 and value['responses']['messages']==4
    assert value['channels']['request_sent']['messages']==7
    assert value['channels']['response_received']['messages']==3
    assert value['history']==[{'at_ns':'9500000000','messages':9,'bytes':None}]
    assert value['count_semantics']=='server_request_receipts_plus_response_sends'
    assert value['bytes'] is None
    assert {e['node_id'] for e in value['endpoints']}=={'node:/client','node:/server'}
    assert 'do-not-export' not in json.dumps(value) and source==before


def test_ready_zero_is_distinct_from_missing_or_unmapped_instrumentation():
    source=trace(request=0,response=0)
    value=service_activity(service(),source)
    assert value['messages']==0 and value['rate_hz']==0.
    source['endpoints'][0]['mapping_ready']=False
    missing=service_activity(service(),source)
    assert missing['messages'] is None and missing['rate_hz'] is None
    assert missing['coverage']['unmapped_servers']==['node:/server']
    assert next(e for e in missing['endpoints'] if e['role']=='server')['node_id'] is None
    inactive=service_activity(service(),trace(),active=False)
    assert inactive['state']=='inactive' and inactive['messages'] is None


def test_missing_server_does_not_turn_client_receipts_into_server_metrics():
    source=trace();source['endpoints']=source['endpoints'][1:]
    value=service_activity(service(),source)
    assert value['channels']['request_sent']['messages']==7
    assert value['requests']['messages'] is None and value['responses']['messages'] is None
    assert value['messages'] is None and value['rate_hz'] is None


def test_trace_loss_is_explicit_and_cannot_fabricate_idle_zero():
    source=trace(request=0,response=0);source['state']='partial';source['loss']['events']=2
    source['counts_lower_bound']=True
    for endpoint in source['endpoints']:
        for channel in endpoint['channels'].values():channel['loss_in_window']=True
    value=service_activity(service(),source)
    assert value['state']=='partial' and value['counts_lower_bound']
    assert value['messages'] is None and value['rate_hz'] is None and value['history']==[]
    source['endpoints'][0]['channels']['request_received']['messages']=2
    assert service_activity(service(),source)['messages']==2


def test_loss_before_current_counter_epoch_does_not_poison_known_current_idle():
    source=trace(request=0,response=0)
    source['loss']['events']=20;source['epoch']='fresh-counter';source['counts_lower_bound']=False
    value=service_activity(service(),source)
    assert value['messages']==0 and value['rate_hz']==0.
    assert value['coverage']['loss']['events']==20 and not value['counts_lower_bound']


def test_mapping_capacity_and_rate_history_overflow_explain_missing_measurements():
    source=trace(request=0,response=0);source['mapping_limited']=True
    value=service_activity(service(),source)
    assert value['messages'] is None and value['rate_hz'] is None
    assert value['coverage']['mapping_limited']
    source=trace();channel=source['endpoints'][0]['channels']['request_received']
    channel.update(rate_hz=None,rate_history_overflow=True)
    value=service_activity(service(),source)
    assert value['requests']['messages']==5 and value['requests']['rate_hz'] is None
    assert 'Rate history capacity' in value['requests']['reason']


def test_duplicate_endpoint_ids_and_missing_discovered_owners_stay_partial():
    source=trace();source['endpoints'].append(copy.deepcopy(source['endpoints'][0]))
    duplicated=service_activity(service(),source)
    assert duplicated['messages'] is None and duplicated['coverage']['invalid_endpoints']==2
    descriptor=service();descriptor['servers'].append('node:/untraced')
    subset=service_activity(descriptor,trace(request=0,response=0))
    assert subset['messages'] is None and subset['rate_hz'] is None
    assert subset['coverage']['unmapped_servers']==['node:/untraced']


def test_histories_are_bounded_and_reject_future_or_old_clock_values():
    source=trace()
    history=[{'at_ns':'11000000000','messages':900},{'at_ns':'0','messages':700},
             {'at_ns':'9500000000','messages':2}]
    source['endpoints'][0]['channels']['request_received']['history']=history
    source['endpoints'][0]['channels']['response_sent']['history']=[]
    value=service_activity(service(),source)
    assert value['history']==[{'at_ns':'9500000000','messages':2,'bytes':None}]
    assert len(value['history'])<=20


def action_fixture():
    legacy={'state':'watching','measurement_scope':'action_server_publications','messages':10,'rate_hz':2.,
        'status':{'state':'watching','sample_id':'status-one','messages':2,'rate_hz':.4,'last_message_age_ms':5.,
                  'history':[{'at_ns':999_500_000_000,'messages':2}]},
        'feedback':{'state':'watching','sample_id':'feedback-one','messages':8,'rate_hz':1.6,'last_message_age_ms':5.,
                    'history':[{'at_ns':999_500_000_000,'messages':8}]}}
    action={'name':'/task','activity':legacy}
    services=[service('/task/_action/'+suffix) for suffix in ('send_goal','cancel_goal','get_result')]
    source=trace();source['endpoints']=[]
    for row in services:
        for endpoint in trace(row['name'])['endpoints']:
            endpoint['id']+=row['name'];source['endpoints'].append(endpoint)
    return action,services,source


def test_actions_add_distinct_rpc_channels_and_align_display_history_without_changing_legacy():
    action,services,source=action_fixture()
    status=copy.deepcopy(action['activity']['status']);feedback=copy.deepcopy(action['activity']['feedback'])
    project_rpc_activity(services,[action],source,active=True,observer_capture_ns=20_000_000_000,observer_wall_ns=1_000_000_000_000)
    activity=action['activity']
    assert activity['messages']==10 and activity['rate_hz']==2.
    assert activity['status']==status and activity['feedback']==feedback
    assert set(activity['rpc'])=={'state','goal','cancel','result'}
    assert activity['rpc']['goal']['requests']['messages']==5
    assert activity['display']['messages']==37 and activity['display']['rate_hz']==7.4
    assert activity['display']['history_clock_type']=='observer_monotonic'
    assert activity['display']['history']==[{'at_ns':'19500000000','messages':37,'bytes':None}]
    old_id=activity['display']['sample_id'];source['epoch']='counter-two'
    project_rpc_activity(services,[action],source,active=True,observer_capture_ns=20_000_000_000,observer_wall_ns=1_000_000_000_000)
    assert action['activity']['display']['sample_id']!=old_id


def test_action_history_does_not_shift_cached_rpc_events_forward_as_response_age_grows():
    action,services,source=action_fixture()
    project_rpc_activity(services,[action],source,active=True,observer_capture_ns=21_000_000_000,
        observer_wall_ns=1_001_000_000_000,age_ms=1000.)
    assert action['activity']['display']['history']==[{'at_ns':'19500000000','messages':37,'bytes':None}]


def test_actual_collector_contract_preserves_epochs_exact_owners_and_four_phases():
    from soarm_station.rpc_trace import RpcTraceCounters
    now=[10_000_000_000]
    counters=RpcTraceCounters(clock=lambda:now[0])
    for pid,role,node,handle in ((12,'server','server',21),(13,'client','client',22)):
        counters.ingest('ros2:rcl_node_init',pid,now[0],
            {'node_handle':1,'node_name':node,'node_namespace':'/'})
        counters.ingest('ros2:rcl_'+('service' if role=='server' else 'client')+'_init',pid,now[0],
            {'node_handle':1,'service_name':'/query',
             'rmw_service_handle' if role=='server' else 'rmw_client_handle':handle})
    counters.set_ready(True);counters.set_active('graph-test',True)
    now[0]+=500_000_000
    for event,pid,field,handle in (
        ('rmw_send_request',13,'rmw_client_handle',22),
        ('rmw_take_request',12,'rmw_service_handle',21),
        ('rmw_send_response',12,'rmw_service_handle',21),
        ('rmw_take_response',13,'rmw_client_handle',22)):
        counters.ingest('ros2:'+event,pid,now[0],{field:handle,'taken':True})
    value=service_activity(service(),counters.snapshot())
    assert value['state']=='watching' and value['messages']==2
    assert {key:metric['messages'] for key,metric in value['channels'].items()}=={
        'request_sent':1,'request_received':1,'response_sent':1,'response_received':1}
    assert {row['node_id'] for row in value['endpoints']}=={'node:/server','node:/client'}
    previous=value['sample_id']
    counters.set_active('graph-test',False)
    assert service_activity(service(),counters.snapshot(),active=False)['state']=='inactive'
    counters.set_active('graph-test',True);now[0]+=500_000_000
    reset=service_activity(service(),counters.snapshot())
    assert reset['sample_id']!=previous and reset['messages']==0 and reset['rate_hz']==0.
