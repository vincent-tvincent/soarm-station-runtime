"""Operator protocol exercised through mocked HTTP/WSS, without any devices."""
import asyncio
import io
import json
from types import SimpleNamespace

import pytest

from soarm_station import operator


@pytest.mark.parametrize('permission_accepted',[True,False])
def test_operator_uses_persistent_intent_and_exact_scoped_cleanup(monkeypatch,permission_accepted):
    calls=[];sent=[];session_headers=[]
    state={'controller_session':'session','runtime_boot_id':'boot','gripper_percent':50.}
    class Response:
        def __init__(self,data):self.data=data
        async def __aenter__(self):return self
        async def __aexit__(self,*_):pass
        def raise_for_status(self):pass
        async def json(self):return self.data
    class Socket:
        async def __aenter__(self):return self
        async def __aexit__(self,*_):pass
        async def send_json(self,data):sent.append(data)
        async def __aiter__(self):
            yield SimpleNamespace(type=SimpleNamespace(value=1),data=json.dumps({'type':'grant','grant':{
                'operation_id':'owned-operation','controller_session':'session','runtime_boot_id':'boot',
                'window_id':'fresh-window','operation_epoch':1,'window_valid_for_ms':250,
                'reason':'Expired or unknown input window'}}))
            await asyncio.Event().wait()
    class Session:
        def __init__(self,headers):session_headers.append(headers)
        async def __aenter__(self):return self
        async def __aexit__(self,*_):pass
        def get(self,url,**kwargs):return Response({'arms':{'arm1':state}})
        def post(self,url,**kwargs):
            calls.append((url,kwargs['json']))
            return Response({'accepted':permission_accepted,'reason':'not admitted'})
        def ws_connect(self,*args,**kwargs):return Socket()
    monkeypatch.setattr(operator,'ClientSession',Session)
    monkeypatch.setattr(operator.ssl,'create_default_context',lambda **_:object())
    monkeypatch.delenv('SOARM_STATION_TOKEN',raising=False)
    monkeypatch.setattr(operator.sys,'stdin',io.StringIO('{"positions":{"joint":0.1},"gripper":50}\n'))
    args=SimpleNamespace(ca=None,url='https://test.invalid',arm='arm1',leader=None,
        seconds=None,hz=100,hold_measured=False)
    if permission_accepted:asyncio.run(operator.run(args))
    else:
        with pytest.raises(RuntimeError,match='not admitted'):asyncio.run(operator.run(args))
    assert session_headers==[{}]
    grant=calls[0][1];cleanup=calls[-1][1]
    assert calls[-1][0].endswith('/api/hold')
    assert cleanup=={'arm_id':'arm1','controller_session':'session','operator_session_id':grant['operator_session_id'],
                    'operation_id':'owned-operation' if permission_accepted else None}
    if permission_accepted:
        assert sent[0]['input_semantics']=='persistent_target' and sent[0]['duration_s'] is None
        assert any(item.get('frame_kind')=='COMMAND' for item in sent)
    else:assert sent==[]
