"""Camera WebSocket lifecycle regressions; no ROS or physical camera required."""
import asyncio
import types
import unittest
from unittest.mock import Mock,patch

from aiohttp import web,WSMsgType
from aiohttp.test_utils import TestClient,TestServer
from soarm_station.web_streams import stream_camera_frames


class CameraStreamTests(unittest.IsolatedAsyncioTestCase):
    async def client(self,*,idle=False):
        state={'stop':False,'sequence':0};finished=asyncio.Event()
        def frame():
            if idle:return None
            state['sequence']+=1
            return b'\xff\xd8fake-jpeg',state['sequence']
        async def handler(request):
            try:
                return await stream_camera_frames(request,frame,lambda:state['stop'],fps=60,
                                                  heartbeat=.05,send_timeout=.2,close_timeout=.2)
            finally:finished.set()
        app=web.Application();app.router.add_get('/ws/camera/test',handler)
        server=TestServer(app);await server.start_server()
        client=TestClient(server);await client.start_server()
        self.addAsyncCleanup(client.close)
        return client,state,finished

    async def test_stream_survives_multiple_heartbeat_periods_without_jpeg_deflate(self):
        client,state,finished=await self.client()
        with self.assertNoLogs('aiohttp.server',level='ERROR'):
            async with client.ws_connect('/ws/camera/test',protocols=['soarm_station'],compress=15) as ws:
                self.assertEqual(ws.protocol,'soarm_station');self.assertEqual(ws.compress,0)
                end=asyncio.get_running_loop().time()+.55;frames=0
                while asyncio.get_running_loop().time()<end:
                    message=await ws.receive(timeout=1)
                    self.assertEqual(message.type,WSMsgType.BINARY)
                    self.assertEqual(message.data,b'\xff\xd8fake-jpeg');frames+=1
                self.assertGreater(frames,10);self.assertFalse(ws.closed)
            await asyncio.wait_for(finished.wait(),1)

    async def test_client_ping_is_consumed_and_answered_while_frames_are_sent(self):
        client,state,finished=await self.client()
        async with client.ws_connect('/ws/camera/test',protocols=['soarm_station'],autoping=False) as ws:
            await ws.ping(b'viewer-probe')
            end=asyncio.get_running_loop().time()+.5;answered=False
            while asyncio.get_running_loop().time()<end:
                message=await ws.receive(timeout=.5)
                if message.type==WSMsgType.PING:await ws.pong(message.data)
                elif message.type==WSMsgType.PONG and message.data==b'viewer-probe':answered=True;break
                else:self.assertEqual(message.type,WSMsgType.BINARY)
            self.assertTrue(answered,'A send-only camera handler never consumes/answers this PING')
        await asyncio.wait_for(finished.wait(),1)

    async def test_idle_camera_client_close_finishes_without_waiting_for_another_frame(self):
        client,state,finished=await self.client(idle=True)
        with self.assertNoLogs('aiohttp.server',level='ERROR'):
            ws=await client.ws_connect('/ws/camera/test',protocols=['soarm_station'])
            await ws.close()
            await asyncio.wait_for(finished.wait(),.5)

    async def test_abrupt_viewer_disconnect_does_not_log_a_handler_failure(self):
        client,state,finished=await self.client()
        with self.assertNoLogs('aiohttp.server',level='ERROR'):
            ws=await client.ws_connect('/ws/camera/test',protocols=['soarm_station'])
            self.assertEqual((await ws.receive(timeout=1)).type,WSMsgType.BINARY)
            ws._response.connection.transport.abort()
            await asyncio.wait_for(finished.wait(),1)
            await ws.close()

    async def test_owner_shutdown_closes_the_waiting_receiver(self):
        client,state,finished=await self.client(idle=True)
        async with client.ws_connect('/ws/camera/test',protocols=['soarm_station']) as ws:
            state['stop']=True
            message=await ws.receive(timeout=1)
            self.assertIn(message.type,(WSMsgType.CLOSE,WSMsgType.CLOSING,WSMsgType.CLOSED))
        await asyncio.wait_for(finished.wait(),.5)

    async def test_blocked_send_and_close_are_bounded_and_receiver_is_canceled(self):
        class BlockedSocket:
            closed=False
            receive_canceled=False
            async def prepare(self,request):pass
            async def receive(self):
                try:await asyncio.Future()
                finally:self.receive_canceled=True
            async def send_bytes(self,data,compress):
                self.compress=compress;await asyncio.Future()
            async def close(self,drain):await asyncio.Future()
        socket=BlockedSocket();transport=Mock();request=types.SimpleNamespace(transport=transport)
        with patch('soarm_station.web_streams.web.WebSocketResponse',return_value=socket) as factory:
            result=await asyncio.wait_for(stream_camera_frames(request,lambda:(b'jpeg',1),lambda:False,
                                                               send_timeout=.02,close_timeout=.02),1)
        self.assertIs(result,socket);self.assertTrue(socket.receive_canceled)
        self.assertEqual(socket.compress,0);self.assertFalse(factory.call_args.kwargs['compress'])
        transport.close.assert_called_once()


if __name__=='__main__':unittest.main()
