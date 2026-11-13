"""Dashboard access from actual loopback/Tailscale peers or a legacy bearer.

Forwarded headers never establish peer identity. This is a private-network
access policy, not an operator session or an automatic actuation grant.
"""
import hmac
import ipaddress


TAILSCALE_IPV4=ipaddress.ip_network('100.64.0.0/10')
TAILSCALE_IPV6=ipaddress.ip_network('fd7a:115c:a1e0::/48')


def validate_access_config(cfg,token):
    """Validate configuration and return whether trusted-peer access is enabled."""
    trusted=cfg.get('trusted_network_access',True)
    if not isinstance(trusted,bool):
        raise ValueError('web.trusted_network_access must be true or false')
    name=cfg.get('token_env','SOARM_STATION_TOKEN')
    if not isinstance(token,str) or (token and len(token)<24) or (not trusted and len(token)<24):
        raise ValueError(f'Missing/short {name}: configure an operator token of at least 24 characters; '
                         'trusted-network mode may leave it unset.')
    return trusted


def trusted_peer(remote):
    """Classify request.remote, which aiohttp obtains from the socket peer."""
    if not isinstance(remote,str):return False
    try:address=ipaddress.ip_address(remote)
    except ValueError:return False
    if isinstance(address,ipaddress.IPv6Address) and address.ipv4_mapped:
        address=address.ipv4_mapped
    return address.is_loopback or address in (TAILSCALE_IPV4 if address.version==4 else TAILSCALE_IPV6)


def dashboard_access_middleware(cfg,token=''):
    """Create the shared API/media policy without importing ROS or robot code."""
    from aiohttp import web
    trusted=validate_access_config(cfg,token)

    @web.middleware
    async def access(request,handler):
        websocket=request.path.startswith('/ws/')
        if websocket or request.method not in ('GET','HEAD','OPTIONS'):
            origin=request.headers.get('Origin')
            # request.scheme/host describe this connection; never consult forwarded headers.
            if origin is not None and origin!=request.scheme+'://'+request.host:
                raise web.HTTPForbidden(text='Origin mismatch')
        if request.path=='/' or request.path.startswith('/assets/'):
            return await handler(request)
        supplied=request.headers.get('Authorization','')
        if websocket and not supplied:
            protocols=[part.strip() for part in request.headers.get('Sec-WebSocket-Protocol','').split(',')]
            if len(protocols)==2 and protocols[0]=='soarm_station':supplied='Bearer '+protocols[1]
        bearer=bool(token) and hmac.compare_digest(supplied.encode('utf-8'),('Bearer '+token).encode('utf-8'))
        if not ((trusted and trusted_peer(request.remote)) or bearer):
            raise web.HTTPUnauthorized()
        try:return await handler(request)
        except (ValueError,KeyError,TimeoutError) as exc:
            raise web.HTTPBadRequest(text=str(exc))
    return access
