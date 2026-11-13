"""ROS-independent tests of the actual shared HTTP/WebSocket access middleware."""
import unittest
from unittest.mock import Mock

from aiohttp import web
from aiohttp.test_utils import TestClient,TestServer,make_mocked_request
from soarm_station.web_access import dashboard_access_middleware,trusted_peer,validate_access_config


TOKEN='legacy-operator-token-at-least-24-characters'


class AccessConfigurationTests(unittest.TestCase):
    def test_default_trusted_mode_needs_no_token_but_locked_mode_does(self):
        self.assertTrue(validate_access_config({},''))
        self.assertTrue(validate_access_config({'trusted_network_access':True},''))
        self.assertFalse(validate_access_config({'trusted_network_access':False},TOKEN))
        with self.assertRaisesRegex(ValueError,'CUSTOM_TOKEN'):
            validate_access_config({'trusted_network_access':False,'token_env':'CUSTOM_TOKEN'},'')
        with self.assertRaisesRegex(ValueError,'SOARM_STATION_TOKEN'):
            validate_access_config({'trusted_network_access':False},'short')
        with self.assertRaisesRegex(ValueError,'true or false'):
            validate_access_config({'trusted_network_access':'false'},TOKEN)

    def test_peer_ranges_are_loopback_and_tailscale_only(self):
        for peer in ('127.0.0.1','127.8.9.10','::1','100.64.0.0','100.104.155.126','100.127.255.255',
                     'fd7a:115c:a1e0::882b:9b7f','fd7a:115c:a1e0:ffff:ffff:ffff:ffff:ffff',
                     '::ffff:127.0.0.1','::ffff:100.104.155.126'):
            with self.subTest(peer=peer):self.assertTrue(trusted_peer(peer))
        for peer in (None,'','localhost','127.0.0.1:8443','100.63.255.255','100.128.0.0','192.168.1.10',
                     '10.0.0.2','172.16.0.2','203.0.113.1','::','fd7a:115c:a1e1::1','fd00::1','::ffff:203.0.113.1'):
            with self.subTest(peer=peer):self.assertFalse(trusted_peer(peer))


class DashboardAccessTests(unittest.IsolatedAsyncioTestCase):
    async def authorize(self,peer,path='/api/status',method='GET',headers=None,config=None,token=''):
        transport=Mock()
        transport.get_extra_info.side_effect=lambda name,default=None:(peer,12345) if name=='peername' and peer is not None else default
        request=make_mocked_request(method,path,headers={'Host':'station.test:8443',**(headers or {})},transport=transport)
        self.assertEqual(request.remote,peer)
        async def handler(req):return web.json_response({'peer':req.remote,'allowed':True})
        return await dashboard_access_middleware(config or {},token)(request,handler)

    async def test_actual_tailscale_peers_access_http_and_websocket_routes_without_tokens(self):
        for peer in ('100.104.155.126','fd7a:115c:a1e0::882b:9b7f','::ffff:100.104.155.126'):
            for path in ('/api/status','/api/cameras/forward.jpg','/ws/camera/forward','/ws/control'):
                with self.subTest(peer=peer,path=path):
                    response=await self.authorize(peer,path,headers={'Sec-WebSocket-Protocol':'soarm_station'})
                    self.assertEqual(response.status,200)

    async def test_forwarded_headers_cannot_grant_or_replace_socket_peer_identity(self):
        headers={'X-Forwarded-For':'100.104.155.126','X-Real-IP':'127.0.0.1',
                 'Forwarded':'for="[fd7a:115c:a1e0::1]";proto=https;host=station.test:8443'}
        for peer in ('203.0.113.9',None):
            with self.subTest(peer=peer):
                with self.assertRaises(web.HTTPUnauthorized):await self.authorize(peer,headers=headers)
        response=await self.authorize('100.104.155.126',headers={'X-Forwarded-For':'203.0.113.9'})
        self.assertEqual(response.status,200)

    async def test_outside_network_needs_a_valid_nonempty_bearer(self):
        for headers in ({},{'Authorization':'Bearer '},{'Authorization':'Bearer wrong'},
                        {'Sec-WebSocket-Protocol':'soarm_station'}):
            with self.subTest(headers=headers):
                with self.assertRaises(web.HTTPUnauthorized):
                    await self.authorize('203.0.113.8',headers=headers)
        response=await self.authorize('203.0.113.8',headers={'Authorization':'Bearer '+TOKEN},token=TOKEN)
        self.assertEqual(response.status,200)
        response=await self.authorize('203.0.113.8','/ws/camera/forward',
            headers={'Sec-WebSocket-Protocol':'soarm_station, '+TOKEN},token=TOKEN)
        self.assertEqual(response.status,200)
        with self.assertRaises(web.HTTPUnauthorized):
            await self.authorize('203.0.113.8',headers={'Authorization':'Bearer non-ASCII-ñ'},token=TOKEN)

    async def test_locked_mode_does_not_bypass_bearer_for_private_peers(self):
        for peer in ('127.0.0.1','::1','100.104.155.126'):
            with self.subTest(peer=peer):
                with self.assertRaises(web.HTTPUnauthorized):
                    await self.authorize(peer,config={'trusted_network_access':False},token=TOKEN)
                response=await self.authorize(peer,config={'trusted_network_access':False},token=TOKEN,
                    headers={'Authorization':'Bearer '+TOKEN})
                self.assertEqual(response.status,200)

    async def test_origin_checks_cover_all_browser_websockets_and_mutations(self):
        for path,method in (('/ws/camera/forward','GET'),('/ws/control','GET'),('/api/hold','POST'),
                            ('/api/permission','POST'),('/api/operate','POST'),('/api/graph/sample','POST')):
            for auth in ({},{'Authorization':'Bearer '+TOKEN}):
                with self.subTest(path=path,auth=bool(auth)):
                    for bad_origin in ('https://other.test','null',''):
                        with self.assertRaises(web.HTTPForbidden):
                            await self.authorize('100.104.155.126',path,method,
                                headers={**auth,'Origin':bad_origin,'X-Forwarded-Host':'other.test','X-Forwarded-Proto':'https'},token=TOKEN)
                    response=await self.authorize('100.104.155.126',path,method,
                        headers={**auth,'Origin':'http://station.test:8443'},token=TOKEN)
                    self.assertEqual(response.status,200)
        # Native/CLI requests without a browser Origin still use the peer/bearer policy.
        self.assertEqual((await self.authorize('127.0.0.1','/api/hold','POST')).status,200)

    async def test_real_loopback_http_and_camera_websocket_open_without_authentication_material(self):
        calls=[]
        async def status(request):return web.json_response({'permission':False,'torque':'ON'})
        async def camera(request):
            ws=web.WebSocketResponse(protocols=['soarm_station'])
            await ws.prepare(request);await ws.send_bytes(b'\xff\xd8test-camera')
            async for _ in ws:pass
            return ws
        async def mutate(request):calls.append(request.path);return web.json_response({'accepted':True})
        app=web.Application(middlewares=[dashboard_access_middleware({},'')])
        app.router.add_get('/api/status',status);app.router.add_get('/ws/camera/forward',camera)
        app.router.add_post('/api/hold',mutate)
        server=TestServer(app)
        await server.start_server()
        async with TestClient(server) as client:
            origin=str(client.make_url('/')).rstrip('/')
            async with client.get('/api/status') as response:
                self.assertEqual(response.status,200)
                self.assertEqual(await response.json(),{'permission':False,'torque':'ON'})
            async with client.ws_connect('/ws/camera/forward',protocols=['soarm_station'],headers={'Origin':origin}) as ws:
                self.assertEqual(ws.protocol,'soarm_station')
                self.assertEqual((await ws.receive(timeout=2)).data,b'\xff\xd8test-camera')
            self.assertEqual(calls,[])  # Reading/connecting grants no motion permission.
            async with client.post('/api/hold',json={},headers={'Origin':'https://other.test'}) as response:
                self.assertEqual(response.status,403)
            self.assertEqual(calls,[])
            async with client.post('/api/hold',json={},headers={'Origin':origin}) as response:
                self.assertEqual(response.status,200)
            self.assertEqual(calls,['/api/hold'])


if __name__=='__main__':unittest.main()
