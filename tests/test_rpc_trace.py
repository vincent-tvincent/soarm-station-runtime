import copy
import json
import struct
import sys
from pathlib import Path
from types import SimpleNamespace

from soarm_station.rpc_trace import RpcTraceCounters,RpcTraceClient,rpc_trace_socket
from soarm_station.rpc_trace_collector import SnapshotReader,LttngSnapshotSource,ProcessScope


class Clock:
    value=10_000_000_000
    def __call__(self):return self.value


def endpoint(c,clock,pid=10,role='server',name='worker',handle=100,birth='a'):
    c.ingest('ros2:rcl_node_init',pid,clock(),{'node_handle':1,'node_name':name,'node_namespace':'/test','process_start':birth})
    fields={'node_handle':1,'service_name':'/test/query','process_start':birth,
            'rmw_service_handle' if role=='server' else 'rmw_client_handle':handle}
    c.ingest('ros2:rcl_service_init' if role=='server' else 'ros2:rcl_client_init',pid,clock(),fields)


def active():
    clock=Clock();c=RpcTraceCounters(clock=clock);c.set_ready(True);c.set_active('viewer',True)
    endpoint(c,clock);endpoint(c,clock,pid=11,role='client',name='client',handle=200)
    return c,clock


def test_four_rpc_phases_have_exact_process_scoped_owners_and_never_export_payloads():
    c,clock=active();clock.value+=100_000_000
    for event,pid,field,handle in [('rmw_send_request',11,'rmw_client_handle',200),
        ('rmw_take_request',10,'rmw_service_handle',100),('rmw_send_response',10,'rmw_service_handle',100),
        ('rmw_take_response',11,'rmw_client_handle',200)]:
        c.ingest('ros2:'+event,pid,clock(),{field:handle,'taken':1,'sequence_number':1,
            'request':'private request','response':'private response'})
    c.ingest('ros2:rmw_take_request',10,clock(),{'rmw_service_handle':100,'taken':0})
    clock.value+=100_000_000
    snapshot=c.snapshot();rows={e['role']:e for e in snapshot['endpoints']}
    assert rows['server']['node_fqn']=='/test/worker' and rows['client']['node_fqn']=='/test/client'
    assert set(rows['server']['channels'])=={'request_received','response_sent'}
    assert set(rows['client']['channels'])=={'request_sent','response_received'}
    assert all(phase['messages']==1 for row in rows.values() for phase in row['channels'].values())
    assert all(phase['rate_hz']==5 for row in rows.values() for phase in row['channels'].values())
    assert 'private request' not in json.dumps(snapshot) and 'private response' not in json.dumps(snapshot)
    assert not any('handle' in key for row in rows.values() for key in row)


def test_idle_zero_requires_mapping_ready_active_measurement_and_observed_interval():
    clock=Clock();c=RpcTraceCounters(clock=clock)
    assert c.snapshot()['state']=='initializing'
    c.set_ready(True);endpoint(c,clock)
    row=c.snapshot()['endpoints'][0]
    assert row['mapping_ready'] and not row['ready']
    assert row['channels']['request_received']['messages'] is None
    c.set_active('viewer',True);clock.value+=1_000_000_000
    phase=c.snapshot()['endpoints'][0]['channels']['request_received']
    assert phase['messages']==0 and phase['rate_hz']==0 and phase['last_event_age_ms'] is None
    assert phase['history'] and all(bucket['messages']==0 for bucket in phase['history'])
    assert all(int(bucket['at_ns'])>=c.active_since for bucket in phase['history'])
    c.set_ready(False,'source stopped')
    assert c.snapshot()['endpoints'][0]['channels']['request_received']['messages'] is None


def test_client_interests_are_scoped_and_new_epoch_does_not_replay_old_snapshot_events():
    c,clock=active();c.set_active('other',True);epoch=c.epoch
    c.set_active('viewer',False);assert c.snapshot()['active'] and c.epoch==epoch
    c.set_active('other',False);clock.value+=2_000_000_000
    c.set_active('new',True);assert c.epoch!=epoch
    c.ingest('ros2:rmw_take_request',10,clock()-1,{'rmw_service_handle':100,'taken':1})
    assert c.snapshot()['endpoints'][0]['channels']['request_received']['messages']==0


def test_losses_are_epoch_scoped_and_current_rates_recover_after_loss_window():
    c,clock=active();c.report_loss(events=2,packets=1)
    snapshot=c.snapshot()
    assert snapshot['counts_lower_bound'] and snapshot['loss']['events']==2
    phase=snapshot['endpoints'][0]['channels']['request_received']
    assert phase['loss_in_window'] and phase['rate_hz'] is None
    clock.value+=6_000_000_000
    phase=c.snapshot()['endpoints'][0]['channels']['request_received']
    assert not phase['loss_in_window'] and phase['rate_hz']==0
    c.set_active('viewer',False);c.set_active('viewer',True)
    assert not c.snapshot()['counts_lower_bound'] and c.snapshot()['loss']['events']==2


def test_unmapped_handles_never_invent_endpoint_or_node_ownership():
    c,clock=active()
    c.ingest('ros2:rmw_send_request',22,clock(),{'rmw_client_handle':200,'sequence_number':1})
    assert c.snapshot()['loss']['unmapped']==1
    assert len(c.snapshot()['endpoints'])==2
    assert all(e['pid']!=22 for e in c.snapshot()['endpoints'])


def test_dead_process_and_pid_reuse_retire_old_maps_before_declaring_coverage():
    class Scope:
        births={10:'a'}
        def __call__(self,pid):return pid in self.births
        def identity(self,pid):return self.births.get(pid)
    clock=Clock();scope=Scope();c=RpcTraceCounters(clock=clock,accept_pid=scope)
    c.set_ready(True);c.set_active('viewer',True);endpoint(c,clock)
    old=c.snapshot()['endpoints'][0]['id'];scope.births[10]='b'
    assert c.snapshot()['endpoints']==[]
    endpoint(c,clock,birth='b')
    assert c.snapshot()['endpoints'][0]['id']!=old
    scope.births.clear();assert c.snapshot()['endpoints']==[]


def test_multiple_context_initialization_does_not_erase_existing_same_process_endpoints():
    c,clock=active()
    c.ingest('ros2:rcl_init',10,clock(),{'context_handle':999,'process_start':'a'})
    assert len(c.snapshot()['endpoints'])==2


def test_history_rate_memory_and_endpoint_maps_are_bounded():
    c,clock=active()
    for i in range(1100):
        c.ingest('ros2:rmw_take_request',10,clock()+i,{'rmw_service_handle':100,'taken':1})
    clock.value+=100_000_000
    phase=c.snapshot()['endpoints'][0]['channels']['request_received']
    assert phase['messages']==1100 and phase['rate_hz'] is None and phase['rate_history_overflow']
    assert len(phase['history'])<=20 and isinstance(phase['history'][0]['at_ns'],str)
    for i in range(300):endpoint(c,clock,pid=1000+i,handle=i)
    assert len(c.endpoints)==256 and c.snapshot()['mapping_limited']


def test_socket_path_and_unavailable_client_are_explicit(tmp_path):
    config={'socket':str(tmp_path/'runtime.sock'),'_path':str(tmp_path/'profile.yaml')}
    assert rpc_trace_socket(config)==str(tmp_path/'runtime.rpc-trace.sock')
    assert rpc_trace_socket({**config,'rpc_trace':{'socket':'trace.sock'}})==str(tmp_path/'trace.sock')
    client=RpcTraceClient(tmp_path/'missing.sock',timeout_s=.01);client.set_active(True)
    assert client.snapshot()['state']=='unavailable';client.close()


META='''/* CTF 1.8 */
trace { byte_order = le;
packet.header := struct {
 integer { size=32; } magic;
 integer { size=8; } uuid[16];
 integer { size=32; } stream_id;
 integer { size=64; } stream_instance_id;
};
};
stream {
packet.context := struct {
 integer { size=64; } timestamp_begin;
 integer { size=64; } timestamp_end;
 integer { size=64; } content_size;
 integer { size=64; } packet_size;
 integer { size=64; } packet_seq_num;
 integer { size=64; } events_discarded;
 integer { size=32; } cpu_id;
};
};
'''


def packet(sequence,discarded=0):
    return struct.pack('<I16sI7QI',0xc1fc1fc1,b'x'*16,0,0,1,2,84*8,84*8,sequence,discarded,0)


def test_standard_ctf_headers_detect_new_losses_without_recounting_snapshot_overlap(tmp_path):
    c,clock=active();reader=SnapshotReader(c)
    (tmp_path/'metadata').write_text(META)
    path=tmp_path/'rpc_0';path.write_bytes(packet(0)+packet(1))
    reader.read_packet_sequences(tmp_path);reader.read_packet_sequences(tmp_path)
    assert c.loss=={'events':0,'packets':0,'unmapped':0}
    path.write_bytes(packet(4,2)+packet(5,2))
    reader.read_packet_sequences(tmp_path);reader.read_packet_sequences(tmp_path)
    assert c.loss=={'events':2,'packets':2,'unmapped':0}


def test_cleanup_commands_target_only_the_owned_session_and_directory(tmp_path):
    commands=[];c=RpcTraceCounters();directory=tmp_path/'owned'
    source=LttngSnapshotSource(c,root=directory,command=lambda args:commands.append(args))
    source.created=True;session=source.session
    unrelated=tmp_path/'other';unrelated.mkdir();(unrelated/'keep').write_text('keep')
    source.close()
    assert commands==[['destroy',session]] and not directory.exists() and (unrelated/'keep').exists()


def test_process_scope_rechecks_liveness_and_start_time_even_inside_cache_window(monkeypatch):
    import soarm_station.rpc_trace_collector as collector
    rows={1:(0,'root'),10:(1,'launch'),11:(10,'first')}
    monkeypatch.setattr(collector,'process_info',lambda pid:rows.get(pid))
    scope=ProcessScope(10);assert scope(11)
    rows[11]=(1,'replacement');assert not scope(11)
    rows.pop(11);assert not scope(11)


def test_babeltrace_serialized_namespace_field_and_snapshot_overlap_are_normalized(monkeypatch,tmp_path):
    c,clock=active();reader=SnapshotReader(c)
    monkeypatch.setattr(reader,'read_packet_sequences',lambda _:None)
    class EventMessage:
        def __init__(self,name,fields,stamp):
            self.event=SimpleNamespace(name=name,payload_field=fields,common_context_field={'vpid':10},
                stream=SimpleNamespace(name='/tmp/capture/64-bit/init_0'))
            self.default_clock_snapshot=SimpleNamespace(value=stamp,
                clock_class=SimpleNamespace(name='monotonic',frequency=1000000000))
    events=[EventMessage('ros2:rcl_node_init',{'node_handle':99,'node_name':'actual','namespace':'/real'},clock()),
        EventMessage('ros2:rcl_service_init',{'node_handle':99,'rmw_service_handle':333,'service_name':'/real/check'},clock()+1),
        EventMessage('ros2:rmw_take_request',{'rmw_service_handle':333,'sequence_number':1,'taken':1},clock()+2)]
    monkeypatch.setitem(sys.modules,'bt2',SimpleNamespace(_EventMessageConst=EventMessage,
        TraceCollectionMessageIterator=lambda _:iter(events)))
    monkeypatch.setattr('soarm_station.rpc_trace_collector.process_info',lambda _:None)
    reader.consume(tmp_path);reader.consume(tmp_path)
    entry=next(item for item in c.snapshot()['endpoints'] if item['service']=='/real/check')
    assert entry['node_fqn']=='/real/actual'
    assert entry['channels']['request_received']['messages']==1


def test_launch_waits_for_trace_arming_and_filters_secret_environment(monkeypatch,tmp_path):
    import soarm_station.launch_support as launch
    import soarm_station.rpc_trace as facade
    calls=[]
    class Process:
        done=False
        def poll(self):return 0 if self.done else None
        def terminate(self):self.done=True
        def wait(self,timeout):return 0
    process=Process()
    def popen(command,**kwargs):calls.append((command,kwargs));return process
    class Client:
        attempts=0
        def __init__(self,*args,**kwargs):pass
        def snapshot(self):
            self.attempts+=1
            return {'mapping_epoch':'source','state':'initializing','trace_ready':False} if self.attempts==1 else {'trace_ready':True}
        def close(self):pass
    monkeypatch.setattr(launch.sys,'platform','linux')
    monkeypatch.setattr(launch.subprocess,'Popen',popen)
    monkeypatch.setattr(facade,'RpcTraceClient',Client)
    owner=launch.start_rpc_trace(tmp_path,{'socket':str(tmp_path/'runtime.sock')},
        {'PRIVATE_TOKEN':'must-not-inherit','PATH':'/usr/bin'},startup_timeout_s=.5)
    assert owner is not None and not process.done
    assert '--parent-pid' in calls[0][0]
    assert 'PRIVATE_TOKEN' not in calls[0][1]['env']
    owner.close();assert process.done


def test_lttng_startup_uses_only_metadata_events_bounded_snapshot_mode_and_restores_selector(monkeypatch,tmp_path):
    commands=[];home=tmp_path/'home';home.mkdir();selector=home/'.lttngrc';selector.write_text('session=unrelated\n')
    monkeypatch.setenv('LTTNG_HOME',str(home))
    monkeypatch.setitem(sys.modules,'bt2',SimpleNamespace(TraceCollectionMessageIterator=object))
    monkeypatch.setattr('soarm_station.rpc_trace_collector.os.cpu_count',lambda:4)
    def command(args):
        commands.append(args)
        if args[0]=='create':selector.write_text('session='+args[1]+'\n')
    source=LttngSnapshotSource(RpcTraceCounters(),root=tmp_path/'owned',command=command)
    source.start()
    assert selector.read_text()=='session=unrelated\n'
    assert '--snapshot' in commands[0] and '--no-output' in commands[0]
    assert source.buffer_bytes==1572864
    enabled=[args for args in commands if args[0]=='enable-event']
    assert len(enabled)==2 and all('ros2:*' not in ','.join(args) for args in enabled)
    assert not any('--live' in args for args in commands)
    source.close()
