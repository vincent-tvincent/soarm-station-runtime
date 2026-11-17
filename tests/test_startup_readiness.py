import json
from soarm_station.readiness import project_readiness
from soarm_station.leader_gateway import LeaderHostRegistry


def test_capture_and_delivery_are_independent_and_stop_reporting_ready_when_stale():
    config={'arms':[],'cameras':[{'id':'wrist'}]}
    diagnostics={'camera:wrist':{'values':{'status':json.dumps({'ready':True,'width':640,'height':480,'age_ms':5})}},
        '/soarm_station/telemetry_node/storage':{'values':{'storage_state':'ready'}}}
    receipts={'diagnostic:camera:wrist':10,'diagnostic:/soarm_station/telemetry_node/storage':10}
    result=project_readiness(config,{},receipts,diagnostics,[],10)
    assert result['state']=='degraded'
    assert 'delivery' in result['components'][0]['detail']
    receipts['image:wrist']=10
    assert project_readiness(config,{},receipts,diagnostics,[],10)['state']=='ready'
    assert project_readiness(config,{},receipts,diagnostics,[],20)['state']=='degraded'


def test_optional_offline_leader_does_not_block_ready_controller_or_restore_run(tmp_path):
    cache=tmp_path/'hosts.json'
    cache.write_text(json.dumps([{'id':'mac','label':'Operator Mac','devices':[],
                                 'active':{'run_id':'old'},'connected':True}]))
    registry=LeaderHostRegistry(lambda:{},lambda:None,cache_path=cache)
    host=registry.snapshot()['hosts'][0]
    assert host['label']=='Operator Mac' and not host['connected'] and host['active'] is None
    assert host['last_seen_age_ms'] is None
    result=project_readiness({}, {}, {'diagnostic:/soarm_station/telemetry_node/storage':10},
        {'/soarm_station/telemetry_node/storage':{'values':{'storage_state':'ready'}}}, [host],10)
    assert result['state']=='ready'
    assert result['components'][-1]['state']=='offline'
    assert result['components'][-1]['required'] is False


def test_cache_does_not_persist_pose_session_or_control_permission(tmp_path):
    registry=LeaderHostRegistry(lambda:{},lambda:None,cache_path=tmp_path/'hosts.json')
    registry.hosts['mac']={'id':'mac','label':'Mac','platform':'Darwin','devices':[],
        'active':{'operation_id':'old'},'latest_pose':{'joint':4},'permission':True}
    registry.remember()
    assert set(json.loads(registry.cache_path.read_text())[0])=={'id','label','platform','devices'}


def test_stale_controller_feedback_and_storage_fault_are_not_healthy():
    config={'arms':[{'id':'arm'}]}
    result=project_readiness(config,{'arm':{'feedback_age_ms':5000}},
        {'controller:arm':10,'diagnostic:/soarm_station/telemetry_node/storage':10},
        {'/soarm_station/telemetry_node/storage':{'values':{'storage_state':'fault','storage_reason':'I/O error'}}},[],10)
    assert result['state']=='degraded'
    assert [c['state'] for c in result['components']]==['waiting','fault']


def test_source_frame_age_and_elapsed_time_cannot_be_reset_by_fresh_diagnostics():
    config={'arms':[{'id':'arm'}],'cameras':[{'id':'camera'}]}
    receipt={'controller:arm':8.5,'diagnostic:camera:camera':10,'image:camera':9.8}
    diagnostic={'camera:camera':{'values':{'status':json.dumps({'ready':True,'age_ms':1500})}}}
    result=project_readiness(config,{'arm':{'feedback_age_ms':0,'feedback_valid':True}},receipt,diagnostic,[],10)
    assert result['components'][0]['state']=='waiting'
    assert result['components'][1]['state']=='waiting'
    assert 'source frame' in result['components'][1]['detail']
