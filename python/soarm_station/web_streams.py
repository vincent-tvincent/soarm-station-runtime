"""Read-only camera WebSocket fan-out with an owned receive/close lifecycle."""
import asyncio

from aiohttp import web,WSMsgType


async def stream_camera_frames(request,get_frame,stopping,*,fps=30,heartbeat=10,
                               send_timeout=2.,close_timeout=1.):
    """Stream the existing (JPEG bytes, source identity) cache; never open a camera.

    Receiving concurrently handles client PING/CLOSE frames and lets aiohttp's
    protocol lifecycle finish. JPEGs do not need per-message deflate. A slow or
    disconnected viewer cannot retain this task indefinitely.
    """
    if fps<=0 or send_timeout<=0 or close_timeout<=0:
        raise ValueError('Positive camera stream timing values required')
    ws=web.WebSocketResponse(protocols=['soarm_station'],max_msg_size=1024,
                             heartbeat=heartbeat,compress=False)
    await ws.prepare(request)

    async def receive_control():
        while not ws.closed:
            message=await ws.receive()
            if message.type in (WSMsgType.CLOSE,WSMsgType.CLOSING,WSMsgType.CLOSED,WSMsgType.ERROR):
                return
            # PING/PONG are handled by aiohttp autoping. This camera endpoint
            # has no application input; discard bounded unsolicited messages.

    receiver=asyncio.create_task(receive_control())
    last=None
    try:
        while not ws.closed and not stopping() and not receiver.done():
            frame=get_frame()
            if frame and frame[1]!=last:
                await asyncio.wait_for(ws.send_bytes(frame[0],compress=0),timeout=send_timeout)
                last=frame[1]
            await asyncio.wait((receiver,),timeout=1/fps)
    except (ConnectionError,asyncio.TimeoutError):
        # Normal viewer disconnect or bounded slow-viewer shutdown, not a
        # camera/device failure and not a reason to log an HTTP-handler error.
        pass
    finally:
        receiver.cancel()
        await asyncio.gather(receiver,return_exceptions=True)
        try:
            await asyncio.wait_for(ws.close(drain=False),timeout=close_timeout)
        except (ConnectionError,asyncio.TimeoutError):
            transport=request.transport
            if transport is not None:transport.close()
    return ws
