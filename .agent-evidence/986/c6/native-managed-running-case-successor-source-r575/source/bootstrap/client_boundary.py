'Trusted test client policy; no OS/native/browser containment claim.'
import socket
from urllib.parse import urlsplit


def exact_url(value, backend_port, frontend_port, paths):
    parsed = urlsplit(value)
    if (parsed.scheme not in {'http', 'ws'} or parsed.hostname != '127.0.0.1'
            or parsed.username is not None or parsed.password is not None
            or parsed.port not in {backend_port, frontend_port}
            or (parsed.port, parsed.path) not in paths):
        raise PermissionError('unowned_control_url')


def install_controller_boundary(backend_port, frontend_port):
    allowed = {('127.0.0.1', backend_port), ('127.0.0.1', frontend_port)}
    original = socket.socket.connect
    original_getaddrinfo = socket.getaddrinfo

    def connect(self, address):
        if self.family != socket.AF_INET or self.type & 0xf != socket.SOCK_STREAM or address not in allowed:
            raise PermissionError('unowned_control_tuple')
        return original(self, address)

    def denied(*args, **kwargs):
        raise PermissionError('unowned_client_network_primitive')

    def getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
        if (host, port) not in allowed or family not in {0, socket.AF_INET} or type not in {0, socket.SOCK_STREAM}:
            raise PermissionError('unowned_client_name_resolution')
        return original_getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM,
                                    proto, flags | socket.AI_NUMERICHOST | socket.AI_NUMERICSERV)

    socket.socket.connect = connect
    # No DNS lookup or connect_ex path: caller uses literal fixture IPv4.
    socket.socket.connect_ex = denied
    socket.socket.sendto = denied
    socket.socket.sendmsg = denied
    socket.getaddrinfo = getaddrinfo


async def install_browser_boundary(context, backend_port, frontend_port, paths):
    async def route_request(route):
        try:
            exact_url(route.request.url, backend_port, frontend_port, paths)
        except (PermissionError, ValueError):
            await route.abort()
        else:
            await route.continue_()

    async def route_websocket(route):
        try:
            exact_url(route.url, backend_port, frontend_port, paths)
        except (PermissionError, ValueError):
            await route.close()
        else:
            await route.connect_to_server()

    if not hasattr(context, 'route_web_socket'):
        raise RuntimeError('browser_websocket_interception_unavailable')
    await context.route('**/*', route_request)
    await context.route_web_socket('**/*', route_websocket)
    # Caller must create context with service_workers='block', and install these
    # routes before creating pages. This function opens no URL.
