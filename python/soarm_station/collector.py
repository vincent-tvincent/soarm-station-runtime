import argparse
import hmac
import json
import os
import sqlite3
import ssl
from pathlib import Path
from aiohttp import web

def create_app(database,token):
    if len(token)<24:raise ValueError('Collector token must have at least 24 characters')
    Path(database).parent.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(database);db.execute('PRAGMA journal_mode=WAL');db.execute('PRAGMA synchronous=FULL')
    db.execute('CREATE TABLE IF NOT EXISTS receipts(device_id TEXT,boot_id TEXT,sequence INTEGER,payload TEXT NOT NULL,PRIMARY KEY(device_id,boot_id,sequence))');db.commit()
    @web.middleware
    async def auth(request,handler):
        if not hmac.compare_digest(request.headers.get('Authorization',''),'Bearer '+token):raise web.HTTPUnauthorized()
        return await handler(request)
    app=web.Application(middlewares=[auth],client_max_size=1048576)
    async def upload(request):
        body=await request.json();samples=body.get('samples')
        if not isinstance(samples,list) or not 1<=len(samples)<=128:raise web.HTTPBadRequest(text='Expected 1..128 samples')
        acks=[]
        try:
            with db:
                for s in samples:
                    d,b,n=s['device_id'],s['runtime_boot_id'],s['sequence']
                    if not isinstance(d,str) or not isinstance(b,str) or len(d)>64 or len(b)>64 or not d or not b or type(n)!=int or not 0<n<2**63:
                        raise ValueError('Invalid receipt key')
                    payload=json.dumps(s,sort_keys=True,allow_nan=False)
                    previous=db.execute('SELECT payload FROM receipts WHERE device_id=? AND boot_id=? AND sequence=?',(d,b,n)).fetchone()
                    if previous and previous[0]!=payload:raise ValueError('Conflicting receipt body')
                    db.execute('INSERT OR IGNORE INTO receipts VALUES(?,?,?,?)',(d,b,n,payload))
                    acks.append({'device_id':d,'runtime_boot_id':b,'sequence':n})
        except (KeyError,ValueError,TypeError) as exc:raise web.HTTPBadRequest(text=str(exc))
        return web.json_response({'committed':acks})
    async def status(request):return web.json_response({'durable_receipts':db.execute('SELECT count(*) FROM receipts').fetchone()[0]})
    async def cleanup(app):db.close()
    app.router.add_post('/v1/samples',upload);app.router.add_get('/v1/status',status);app.on_cleanup.append(cleanup)
    return app
def main():
    p=argparse.ArgumentParser();p.add_argument('--database',required=True);p.add_argument('--cert',required=True);p.add_argument('--key',required=True)
    p.add_argument('--host',default='0.0.0.0');p.add_argument('--port',type=int,default=9443);a=p.parse_args()
    context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);context.load_cert_chain(a.cert,a.key)
    web.run_app(create_app(a.database,os.environ['SOARM_STATION_COLLECTOR_TOKEN']),host=a.host,port=a.port,ssl_context=context,print=None)
if __name__=='__main__':main()
