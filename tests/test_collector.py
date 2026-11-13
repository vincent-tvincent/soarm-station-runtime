import asyncio
from aiohttp.test_utils import TestClient,TestServer
from soarm_station.collector import create_app
def test_auth_durable_dedup_conflict(tmp_path):
    async def check():
        token='x'*32;client=TestClient(TestServer(create_app(str(tmp_path/'collector.db'),token)));await client.start_server()
        try:
            assert (await client.get('/v1/status')).status==401
            headers={'Authorization':'Bearer '+token};sample={'device_id':'device','runtime_boot_id':'boot','sequence':1,'valid':True}
            for _ in range(2):
                r=await client.post('/v1/samples',json={'samples':[sample]},headers=headers);assert r.status==200;assert (await r.json())['committed'][0]['sequence']==1
            r=await client.get('/v1/status',headers=headers);assert (await r.json())['durable_receipts']==1
            r=await client.post('/v1/samples',json={'samples':[{**sample,'valid':False}]},headers=headers);assert r.status==400
        finally:await client.close()
    asyncio.run(check())
