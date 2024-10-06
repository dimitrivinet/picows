import asyncio
import base64
import os
import sys

import picows
import pytest
import async_timeout

from tests.utils import create_client_ssl_context, create_server_ssl_context, \
    TextFrame, CloseFrame, BinaryFrame, ServerAsyncContext, TIMEOUT, \
    materialize_frame

if os.name == 'nt':
    @pytest.fixture(
        params=(
            "asyncio",
        ),
    )
    def event_loop_policy(request):
        if sys.version_info >= (3, 10):
            return asyncio.DefaultEventLoopPolicy()
        else:
            return asyncio.WindowsSelectorEventLoopPolicy()
else:
    import uvloop

    @pytest.fixture(
        params=(
            "asyncio",
            "uvloop",
        ),
    )
    def event_loop_policy(request):
        if request.param == 'asyncio':
            return asyncio.DefaultEventLoopPolicy()
        elif request.param == 'uvloop':
            return uvloop.EventLoopPolicy()
        else:
            assert False, "unknown loop"


@pytest.fixture(params=["plain", "ssl"])
async def echo_server(request):
    class PicowsServerListener(picows.WSListener):
        def on_ws_connected(self, transport: picows.WSTransport):
            self._transport = transport

        def on_ws_frame(self, transport: picows.WSTransport, frame: picows.WSFrame):
            if frame.msg_type == picows.WSMsgType.CLOSE:
                self._transport.send_close(frame.get_close_code(), frame.get_close_message())
                self._transport.disconnect()
            else:
                self._transport.send(frame.msg_type, frame.get_payload_as_bytes(), frame.fin, frame.rsv1)

    use_ssl = request.param == "ssl"
    server = await picows.ws_create_server(lambda _: PicowsServerListener(),
                                           "127.0.0.1",
                                           0,
                                           ssl=create_server_ssl_context() if use_ssl else None,
                                           websocket_handshake_timeout=0.5)

    async with ServerAsyncContext(server):
        yield f"{'wss' if use_ssl else 'ws'}://127.0.0.1:{server.sockets[0].getsockname()[1]}/"


@pytest.fixture()
async def echo_client(echo_server):
    class PicowsClientListener(picows.WSListener):
        transport: picows.WSTransport
        msg_queue: asyncio.Queue
        is_paused: bool

        def on_ws_connected(self, transport: picows.WSTransport):
            self.transport = transport
            self.msg_queue = asyncio.Queue()
            self.is_paused = False

        def on_ws_frame(self, transport: picows.WSTransport, frame: picows.WSFrame):
            self.msg_queue.put_nowait(materialize_frame(frame))

        def pause_writing(self):
            self.is_paused = True

        def resume_writing(self):
            self.is_paused = False

        async def get_message(self):
            async with async_timeout.timeout(TIMEOUT):
                item = await self.msg_queue.get()
                self.msg_queue.task_done()
                return item

    (_, client) = await picows.ws_connect(PicowsClientListener, echo_server,
                                          ssl_context=create_client_ssl_context(),
                                          websocket_handshake_timeout=0.5)
    yield client

    # Teardown client
    client.transport.send_close(picows.WSCloseCode.GOING_AWAY, b"poka poka")
    try:
        # Gracefull shutdown, expect server to disconnect us because we have sent close message
        async with async_timeout.timeout(TIMEOUT):
            await client.transport.wait_disconnected()
    finally:
        client.transport.disconnect()


@pytest.mark.parametrize("msg_size", [0, 1, 2, 3, 4, 5, 6, 7, 8, 64, 256 * 1024])
async def test_echo(echo_client, msg_size):
    msg = os.urandom(msg_size)
    echo_client.transport.send(picows.WSMsgType.BINARY, msg, False, False)
    frame = await echo_client.get_message()
    assert frame.msg_type == picows.WSMsgType.BINARY
    assert frame.payload_as_bytes == msg
    assert frame.payload_as_bytes_from_mv == msg
    assert not frame.fin
    assert not frame.rsv1

    msg = base64.b64encode(msg)
    echo_client.transport.send(picows.WSMsgType.TEXT, msg, True, True)
    frame = await echo_client.get_message()
    assert frame.msg_type == picows.WSMsgType.TEXT
    assert frame.payload_as_ascii_text == msg.decode("ascii")
    assert frame.payload_as_utf8_text == msg.decode("utf8")
    assert frame.fin
    assert frame.rsv1

    # Check send defaults
    echo_client.transport.send(picows.WSMsgType.BINARY, msg)
    frame = await echo_client.get_message()
    assert frame.fin
    assert not frame.rsv1

    # Check ping
    echo_client.transport.send_ping(b"hi")
    frame = await echo_client.get_message()
    assert frame.msg_type == picows.WSMsgType.PING
    assert frame.payload_as_bytes == b"hi"

    # Check pong
    echo_client.transport.send_pong(b"hi")
    frame = await echo_client.get_message()
    assert frame.msg_type == picows.WSMsgType.PONG
    assert frame.payload_as_bytes == b"hi"

    # Test non-bytes like send
    with pytest.raises(TypeError):
        echo_client.transport.send(picows.WSMsgType.BINARY, "hi")


async def test_close(echo_client):
    echo_client.transport.send_close(picows.WSCloseCode.GOING_AWAY, b"goodbye")
    frame = await echo_client.get_message()
    assert frame.msg_type == picows.WSMsgType.CLOSE
    assert frame.close_code == picows.WSCloseCode.GOING_AWAY
    assert frame.close_message == b"goodbye"


async def test_client_handshake_timeout(echo_server):
    # Set unreasonably small timeout
    with pytest.raises(asyncio.TimeoutError):
        (_, client) = await picows.ws_connect(picows.WSListener, echo_server,
                                              ssl_context=create_client_ssl_context(),
                                              websocket_handshake_timeout=0.00001)


async def test_server_handshake_timeout():
    server = await picows.ws_create_server(lambda _: picows.WSListener(),
                                           "127.0.0.1", 0, websocket_handshake_timeout=0.1)

    async with ServerAsyncContext(server):
        # Give some time for server to start
        await asyncio.sleep(0.1)

        client_reader, client_writer = await asyncio.open_connection("127.0.0.1", server.sockets[0].getsockname()[1])
        assert not client_reader.at_eof()
        await asyncio.sleep(0.2)
        assert client_reader.at_eof()


@pytest.mark.parametrize("request_path", ["/v1/ws", "/v1/ws?key=blablabla&data=fhhh"])
async def test_request_path_and_params(request_path):
    def listener_factory(request: picows.WSUpgradeRequest):
        assert request.method == b"GET"
        assert request.path == request_path.encode()
        assert request.version == b"HTTP/1.1"

        return picows.WSListener()

    server = await picows.ws_create_server(listener_factory,
                                           "127.0.0.1", 0, websocket_handshake_timeout=0.1)
    async with ServerAsyncContext(server):
        url = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}{request_path}"
        (transport, _) = await picows.ws_connect(picows.WSListener, url)
        transport.disconnect()


async def test_route_not_found():
    server = await picows.ws_create_server(lambda _: None, "127.0.0.1", 0)
    async with ServerAsyncContext(server):
        url = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}/"

        with pytest.raises(picows.WSError, match="404 Not Found"):
            (_, client) = await picows.ws_connect(picows.WSListener, url)


async def test_server_internal_error():
    def factory_listener(r):
        raise RuntimeError("oops")

    server = await picows.ws_create_server(factory_listener, "127.0.0.1", 0)
    async with ServerAsyncContext(server):
        url = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}/"

        with pytest.raises(picows.WSError, match="500 Internal Server Error"):
            (_, client) = await picows.ws_connect(picows.WSListener, url)


async def test_server_bad_request():
    server = await picows.ws_create_server(lambda _: picows.WSListener(),
                                           "127.0.0.1", 0)

    async with ServerAsyncContext(server):
        r, w = await asyncio.open_connection("127.0.0.1", server.sockets[0].getsockname()[1])

        w.write(b"zzzz\r\nasdfasdf\r\n\r\n")
        resp_header = await r.readuntil(b"\r\n\r\n")
        assert b"400 Bad Request" in resp_header
        async with async_timeout.timeout(TIMEOUT):
            await r.read()
        assert r.at_eof()


async def test_ws_on_connected_throw():
    class ServerClientListener(picows.WSListener):
        def on_ws_connected(self, transport: picows.WSTransport):
            raise RuntimeError("exception from on_ws_connected")

    server = await picows.ws_create_server(lambda _: ServerClientListener(),
                                           "127.0.0.1", 0)
    async with ServerAsyncContext(server):
        url = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}"
        (transport, _) = await picows.ws_connect(picows.WSListener, url)
        async with async_timeout.timeout(TIMEOUT):
            await transport.wait_disconnected()


@pytest.mark.parametrize("disconnect_on_exception", [True, False])
async def test_ws_on_frame_throw(disconnect_on_exception):
    class ServerClientListener(picows.WSListener):
        def on_ws_frame(self, transport: picows.WSTransport, frame: picows.WSFrame):
            raise RuntimeError("exception from on_ws_frame")

    server = await picows.ws_create_server(lambda _: ServerClientListener(),
                                           "127.0.0.1",
                                           0,
                                           disconnect_on_exception=disconnect_on_exception)

    async with ServerAsyncContext(server):
        url = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}/"

        (transport, _) = await picows.ws_connect(picows.WSListener, url)
        transport.send(picows.WSMsgType.BINARY, b"halo")

        try:
            if disconnect_on_exception:
                async with async_timeout.timeout(TIMEOUT):
                    await transport.wait_disconnected()
            else:
                with pytest.raises(asyncio.TimeoutError):
                    async with async_timeout.timeout(TIMEOUT):
                        await transport.wait_disconnected()
        finally:
            transport.disconnect()


async def test_stress(echo_client):
    # Heuristic check if picows direct write works smoothly together with
    # loop transport write. We have to fill socket system buffers first
    # and then loop Transport.write kicks in. Only after that we get pause_writing

    echo_client.transport.underlying_transport.set_write_buffer_limits(256, 128)

    msg1 = os.urandom(307)
    msg2 = os.urandom(311)
    msg3 = os.urandom(313)

    total_batches = 0
    while not echo_client.is_paused:
        echo_client.transport.send(picows.WSMsgType.BINARY, msg1)
        echo_client.transport.send(picows.WSMsgType.BINARY, msg2)
        echo_client.transport.send(picows.WSMsgType.BINARY, msg3)
        total_batches += 1

    # Add extra batch to make sure we utilize loop buffers above high watermark
    echo_client.transport.send(picows.WSMsgType.BINARY, msg1)
    echo_client.transport.send(picows.WSMsgType.BINARY, msg2)
    echo_client.transport.send(picows.WSMsgType.BINARY, msg3)
    total_batches += 1

    for i in range(total_batches * 3):
        async with async_timeout.timeout(TIMEOUT):
            frame = await echo_client.get_message()

        if i % 3 == 0:
            assert frame.payload_as_bytes == msg1
        elif i % 3 == 1:
            assert frame.payload_as_bytes == msg2
        else:
            assert frame.payload_as_bytes == msg3

    with pytest.raises(asyncio.TimeoutError):
        async with async_timeout.timeout(TIMEOUT):
            frame = await echo_client.get_message()

    assert not echo_client.is_paused


async def test_native_exc_conversion(echo_client):
    if echo_client.transport.is_secure:
        pytest.skip("skipped for secure connections")

    # make server disconnect us
    echo_client.transport.send_close(picows.WSCloseCode.GOING_AWAY)
    await echo_client.get_message()
    await asyncio.sleep(0.1)
    msg = os.urandom(256)
    with pytest.raises(OSError):
        echo_client.transport.send(picows.WSMsgType.BINARY, msg)
        await asyncio.sleep(0.1)
        echo_client.transport.send(picows.WSMsgType.BINARY, msg)
