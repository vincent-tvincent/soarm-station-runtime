"""Snapshot ownership must end before the collector rotates/deletes its files."""
import gc
import sys
import weakref
from types import SimpleNamespace

import pytest

from soarm_station.rpc_trace import RpcTraceCounters
from soarm_station.rpc_trace_collector import SnapshotReader


@pytest.mark.parametrize('exit_mode',['complete','limit','decode_error','constructor_error'])
def test_snapshot_cycles_release_files_on_every_exit_path(tmp_path,monkeypatch,exit_mode):
    files=[];iterators=[];completed_indices=[]
    fixture=tmp_path/'trace';fixture.write_bytes(b'fixture metadata')
    class EventMessage:
        def __init__(self,owner):
            # Event/clock/field wrappers can retain the native graph as well as
            # the iterator; the consumer helper frame must release them first.
            self.owner=owner
            self.event=SimpleNamespace(name='ros2:rmw_send_request',payload_field={
                'rmw_client_handle':20,'sequence_number':owner.index},common_context_field={'vpid':10},
                stream=SimpleNamespace(name='/trace/64-bit/rpc_0'),owner=owner)
            self.default_clock_snapshot=SimpleNamespace(value=10_000_000_000+owner.index,
                clock_class=SimpleNamespace(name='bad-clock' if exit_mode=='decode_error' else 'monotonic',
                                            frequency=1_000_000_000),owner=owner)
    class CyclicIterator:
        def __init__(self,_path):
            self.file=fixture.open('rb');files.append(self.file);iterators.append(weakref.ref(self))
            self.graph=SimpleNamespace(port_listener=self.on_port);self.index=0
            if exit_mode=='constructor_error':
                try:raise ValueError('inner constructor failure')
                except ValueError as error:raise RuntimeError('constructor failure') from error
        def on_port(self):pass
        def __iter__(self):return self
        def __next__(self):
            if self.index>=3:raise StopIteration
            self.index+=1;return EventMessage(self)
        def __del__(self):self.file.close();completed_indices.append(self.index)
    monkeypatch.setitem(sys.modules,'bt2',SimpleNamespace(
        TraceCollectionMessageIterator=CyclicIterator,_EventMessageConst=EventMessage))
    reader=SnapshotReader(RpcTraceCounters(),max_events=1 if exit_mode=='limit' else 100)
    ingested=[]
    monkeypatch.setattr(reader.counters,'ingest',lambda *args:ingested.append(args))
    monkeypatch.setattr(reader,'read_packet_sequences',lambda _:None)
    automatic=gc.isenabled();gc.disable()
    try:
        for _ in range(4):
            if exit_mode=='constructor_error':
                with pytest.raises(RuntimeError,match='constructor failure') as error:reader.consume(tmp_path)
                assert isinstance(error.value.__cause__,ValueError)
            elif exit_mode=='decode_error':
                with pytest.raises(ValueError,match='Trace clock'):reader.consume(tmp_path)
            else:reader.consume(tmp_path)
            assert all(file.closed for file in files), 'Snapshot descriptors remained open after consume returned/raised'
            assert all(reference() is None for reference in iterators)
        assert completed_indices==[0 if exit_mode=='constructor_error' else 3]*4
        # Re-reading the same snapshot stays deduplicated; native cleanup of
        # its remainder must never turn discarded records into counted events.
        assert len(ingested)==({'complete':3,'limit':1}.get(exit_mode,0))
    finally:
        for file in files:file.close()
        if automatic:gc.enable()
        gc.collect()
