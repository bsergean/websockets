"""
The :mod:`websockets.protocol` module handles WebSocket control and data
frames as specified in `sections 4 to 8 of RFC 6455`_.

.. _sections 4 to 8 of RFC 6455: http://tools.ietf.org/html/rfc6455#section-4

"""

import asyncio
import binascii
import codecs
import collections
import enum
import logging
import random
import struct
import warnings
from typing import (
    Any,
    AsyncIterable,
    AsyncIterator,
    Awaitable,
    Deque,
    Iterable,
    List,
    Optional,
    Union,
    cast,
)

from .exceptions import (
    ConnectionClosed,
    ConnectionClosedError,
    ConnectionClosedOK,
    InvalidState,
    PayloadTooBig,
    WebSocketProtocolError,
)
from .extensions.base import Extension
from .framing import *
from .handshake import *
from .http import Headers
from .typing import Data


__all__ = ["WebSocketCommonProtocol"]

logger = logging.getLogger(__name__)


# A WebSocket connection goes through the following four states, in order:


class State(enum.IntEnum):
    CONNECTING, OPEN, CLOSING, CLOSED = range(4)


# In order to ensure consistency, the code always checks the current value of
# WebSocketCommonProtocol.state before assigning a new value and never yields
# between the check and the assignment.


class WebSocketCommonProtocol(asyncio.StreamReaderProtocol):
    """
    This class implements common parts of the WebSocket protocol.

    It assumes that the WebSocket connection is established. The handshake is
    managed in subclasses such as
    :class:`~websockets.server.WebSocketServerProtocol` and
    :class:`~websockets.client.WebSocketClientProtocol`.

    It runs a task that stores incoming data frames in a queue and deals with
    control frames automatically. It sends outgoing data frames and performs
    the closing handshake.

    :class:`WebSocketCommonProtocol` supports asynchronous iteration::

        async for message in websocket:
            await process(message)

    The iterator yields incoming messages. It exits normally when the
    connection is closed with the close code 1000 (OK) or 1001 (going away).
    It raises a :exc:`~websockets.exceptions.ConnectionClosedError` exception
    when the connection is closed with any other status code.

    The ``host``, ``port`` and ``secure`` parameters are simply stored as
    attributes for handlers that need them.

    Once the connection is open, a `Ping frame`_ is sent every
    ``ping_interval`` seconds. This serves as a keepalive. It helps keeping
    the connection open, especially in the presence of proxies with short
    timeouts. Set ``ping_interval`` to ``None`` to disable this behavior.

    .. _Ping frame: https://tools.ietf.org/html/rfc6455#section-5.5.2

    If the corresponding `Pong frame`_ isn't received within ``ping_timeout``
    seconds, the connection is considered unusable and is closed with status
    code 1011. This ensures that the remote endpoint remains responsive. Set
    ``ping_timeout`` to ``None`` to disable this behavior.

    .. _Pong frame: https://tools.ietf.org/html/rfc6455#section-5.5.3

    The ``close_timeout`` parameter defines a maximum wait time in seconds for
    completing the closing handshake and terminating the TCP connection.
    :meth:`close()` completes in at most ``4 * close_timeout`` on the server
    side and ``5 * close_timeout`` on the client side.

    ``close_timeout`` needs to be a parameter of the protocol because
    websockets usually calls :meth:`close()` implicitly:

    - on the server side, when the connection handler terminates,
    - on the client side, when exiting the context manager for the connection.

    To apply a timeout to any other API, wrap it in :func:`~asyncio.wait_for`.

    The ``max_size`` parameter enforces the maximum size for incoming messages
    in bytes. The default value is 1 MiB. ``None`` disables the limit. If a
    message larger than the maximum size is received, :meth:`recv()` will
    raise :exc:`~websockets.exceptions.ConnectionClosedError` and the
    connection will be closed with status code 1009.

    The ``max_queue`` parameter sets the maximum length of the queue that
    holds incoming messages. The default value is ``32``. ``None`` disables
    the limit. Messages are added to an in-memory queue when they're received;
    then :meth:`recv()` pops from that queue. In order to prevent excessive
    memory consumption when messages are received faster than they can be
    processed, the queue must be bounded. If the queue fills up, the protocol
    stops processing incoming data until :meth:`recv()` is called. In this
    situation, various receive buffers (at least in ``asyncio`` and in the OS)
    will fill up, then the TCP receive window will shrink, slowing down
    transmission to avoid packet loss.

    Since Python can use up to 4 bytes of memory to represent a single
    character, each websocket connection may use up to ``4 * max_size *
    max_queue`` bytes of memory to store incoming messages. By default,
    this is 128 MiB. You may want to lower the limits, depending on your
    application's requirements.

    The ``read_limit`` argument sets the high-water limit of the buffer for
    incoming bytes. The low-water limit is half the high-water limit. The
    default value is 64 KiB, half of asyncio's default (based on the current
    implementation of :class:`~asyncio.StreamReader`).

    The ``write_limit`` argument sets the high-water limit of the buffer for
    outgoing bytes. The low-water limit is a quarter of the high-water limit.
    The default value is 64 KiB, equal to asyncio's default (based on the
    current implementation of ``FlowControlMixin``).

    As soon as the HTTP request and response in the opening handshake are
    processed:

    * the request path is available in the :attr:`path` attribute;
    * the request and response HTTP headers are available in the
      :attr:`request_headers` and :attr:`response_headers` attributes,
      which are :class:`~websockets.http.Headers` instances.

    These attributes must be treated as immutable.

    If a subprotocol was negotiated, it's available in the :attr:`subprotocol`
    attribute.

    Once the connection is closed, the status code is available in the
    :attr:`close_code` attribute and the reason in :attr:`close_reason`.

    """

    # There are only two differences between the client-side and server-side
    # behavior: masking the payload and closing the underlying TCP connection.
    # Set is_client = True/False and side = "client"/"server" to pick a side.
    is_client: bool
    side: str = "undefined"

    def __init__(
        self,
        *,
        host: Optional[str] = None,
        port: Optional[int] = None,
        secure: Optional[bool] = None,
        ping_interval: float = 20,
        ping_timeout: float = 20,
        close_timeout: Optional[float] = None,
        max_size: int = 2 ** 20,
        max_queue: int = 2 ** 5,
        read_limit: int = 2 ** 16,
        write_limit: int = 2 ** 16,
        loop: Optional[asyncio.AbstractEventLoop] = None,
        legacy_recv: bool = False,
        timeout: Optional[float] = None,
    ) -> None:
        # Backwards-compatibility: close_timeout used to be called timeout.
        if timeout is None:
            timeout = 10
        else:
            warnings.warn("rename timeout to close_timeout", DeprecationWarning)
        # If both are specified, timeout is ignored.
        if close_timeout is None:
            close_timeout = timeout

        self.host = host
        self.port = port
        self.secure = secure
        self.ping_interval = ping_interval
        self.ping_timeout = ping_timeout
        self.close_timeout = close_timeout
        self.max_size = max_size
        self.max_queue = max_queue
        self.read_limit = read_limit
        self.write_limit = write_limit

        # Store a reference to loop to avoid relying on self._loop, a private
        # attribute of StreamReaderProtocol, inherited from FlowControlMixin.
        if loop is None:
            loop = asyncio.get_event_loop()
        self.loop = loop

        self.legacy_recv = legacy_recv

        # Configure read buffer limits. The high-water limit is defined by
        # ``self.read_limit``. The ``limit`` argument controls the line length
        # limit and half the buffer limit of :class:`~asyncio.StreamReader`.
        # That's why it must be set to half of ``self.read_limit``.
        stream_reader = asyncio.StreamReader(limit=read_limit // 2, loop=loop)
        super().__init__(stream_reader, self.client_connected, loop)

        self.reader: asyncio.StreamReader
        self.writer: asyncio.StreamWriter
        self._drain_lock = asyncio.Lock(loop=loop)

        # This class implements the data transfer and closing handshake, which
        # are shared between the client-side and the server-side.
        # Subclasses implement the opening handshake and, on success, execute
        # :meth:`connection_open()` to change the state to OPEN.
        self.state = State.CONNECTING
        logger.debug("%s - state = CONNECTING", self.side)

        # HTTP protocol parameters.
        self.path: str
        self.request_headers: Headers
        self.response_headers: Headers

        # WebSocket protocol parameters.
        self.extensions: List[Extension] = []
        self.subprotocol: Optional[str] = None

        # The close code and reason are set when receiving a close frame or
        # losing the TCP connection.
        self.close_code: int
        self.close_reason: str

        # Completed when the connection state becomes CLOSED. Translates the
        # :meth:`connection_lost()` callback to a :class:`~asyncio.Future`
        # that can be awaited. (Other :class:`~asyncio.Protocol` callbacks are
        # translated by ``self.stream_reader``).
        self.connection_lost_waiter: asyncio.Future[None] = loop.create_future()

        # Queue of received messages.
        self.messages: Deque[Data] = collections.deque()
        self._pop_message_waiter: Optional[asyncio.Future[None]] = None
        self._put_message_waiter: Optional[asyncio.Future[None]] = None

        # Protect sending fragmented messages.
        self._fragmented_message_waiter: Optional[asyncio.Future[None]] = None

        # Mapping of ping IDs to waiters, in chronological order.
        self.pings: collections.OrderedDict[
            bytes, asyncio.Future[None]
        ] = collections.OrderedDict()

        # Task running the data transfer.
        self.transfer_data_task: asyncio.Task[None]

        # Exception that occurred during data transfer, if any.
        self.transfer_data_exc: Optional[BaseException] = None

        # Task sending keepalive pings.
        self.keepalive_ping_task: asyncio.Task[None]

        # Task closing the TCP connection.
        self.close_connection_task: asyncio.Task[None]

    def client_connected(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """
        Callback when the TCP connection is established.

        Record references to the stream reader and the stream writer to avoid
        using private attributes ``_stream_reader`` and ``_stream_writer`` of
        :class:`~asyncio.StreamReaderProtocol`.

        """
        self.reader = reader
        self.writer = writer

    def connection_open(self) -> None:
        """
        Callback when the WebSocket opening handshake completes.

        Enter the OPEN state and start the data transfer phase.

        """
        # 4.1. The WebSocket Connection is Established.
        assert self.state is State.CONNECTING
        self.state = State.OPEN
        logger.debug("%s - state = OPEN", self.side)
        # Start the task that receives incoming WebSocket messages.
        self.transfer_data_task = self.loop.create_task(self.transfer_data())
        # Start the task that sends pings at regular intervals.
        self.keepalive_ping_task = self.loop.create_task(self.keepalive_ping())
        # Start the task that eventually closes the TCP connection.
        self.close_connection_task = self.loop.create_task(self.close_connection())

    # Public API

    @property
    def local_address(self) -> Any:
        """
        Local address of the connection.

        This is a ``(host, port)`` tuple or ``None`` if the connection hasn't
        been established yet.

        """
        if self.writer is None:
            return None
        return self.writer.get_extra_info("sockname")

    @property
    def remote_address(self) -> Any:
        """
        Remote address of the connection.

        This is a ``(host, port)`` tuple or ``None`` if the connection hasn't
        been established yet.

        """
        if self.writer is None:
            return None
        return self.writer.get_extra_info("peername")

    @property
    def open(self) -> bool:
        """
        This property is ``True`` when the connection is usable.

        It may be used to detect disconnections but this is discouraged per
        the EAFP_ principle. When ``open`` is ``False``, using the connection
        raises a :exc:`~websockets.exceptions.ConnectionClosed` exception.

        .. _EAFP: https://docs.python.org/3/glossary.html#term-eafp

        """
        return self.state is State.OPEN and not self.transfer_data_task.done()

    @property
    def closed(self) -> bool:
        """
        This property is ``True`` once the connection is closed.

        Be aware that both :attr:`open` and :attr:`closed` are ``False`` during
        the opening and closing sequences.

        """
        return self.state is State.CLOSED

    async def wait_closed(self) -> None:
        """
        Wait until the connection is closed.

        This is identical to :attr:`closed`, except it can be awaited.

        This can make it easier to handle connection termination, regardless
        of its cause, in tasks that interact with the WebSocket connection.

        """
        await asyncio.shield(self.connection_lost_waiter)

    async def __aiter__(self) -> AsyncIterator[Data]:
        """
        Iterate on received messages.

        Exit normally when the connection is closed with code 1000 or 1001.

        Raise an exception in other cases.

        """
        try:
            while True:
                yield await self.recv()
        except ConnectionClosedOK:
            return

    async def recv(self) -> Data:
        """
        This coroutine receives the next message.

        It returns a :class:`str` for a text frame and :class:`bytes` for a
        binary frame.

        When the end of the message stream is reached, :meth:`recv` raises
        :exc:`~websockets.exceptions.ConnectionClosed`. Specifically, it
        raises :exc:`~websockets.exceptions.ConnectionClosedOK` after a normal
        connection closure and
        :exc:`~websockets.exceptions.ConnectionClosedError`after a protocol
        error or a network failure.

        .. versionchanged:: 3.0

            :meth:`recv` used to return ``None`` instead. Refer to the
            changelog for details.

        Canceling :meth:`recv` is safe. There's no risk of losing the next
        message. The next invocation of :meth:`recv` will return it. This
        makes it possible to enforce a timeout by wrapping :meth:`recv` in
        :func:`~asyncio.wait_for`.

        .. versionchanged:: 7.0

            Calling :meth:`recv` concurrently raises :exc:`RuntimeError`.

        """
        if self._pop_message_waiter is not None:
            raise RuntimeError(
                "cannot call recv while another coroutine "
                "is already waiting for the next message"
            )

        # Don't await self.ensure_open() here:
        # - messages could be available in the queue even if the connection
        #   is closed;
        # - messages could be received before the closing frame even if the
        #   connection is closing.

        # Wait until there's a message in the queue (if necessary) or the
        # connection is closed.
        while len(self.messages) <= 0:
            pop_message_waiter: asyncio.Future[None] = self.loop.create_future()
            self._pop_message_waiter = pop_message_waiter
            try:
                # If asyncio.wait() is canceled, it doesn't cancel
                # pop_message_waiter and self.transfer_data_task.
                await asyncio.wait(
                    [pop_message_waiter, self.transfer_data_task],
                    loop=self.loop,
                    return_when=asyncio.FIRST_COMPLETED,
                )
            finally:
                self._pop_message_waiter = None

            # If asyncio.wait(...) exited because self.transfer_data_task
            # completed before receiving a new message, raise a suitable
            # exception (or return None if legacy_recv is enabled).
            if not pop_message_waiter.done():
                if self.legacy_recv:
                    return None  # type: ignore
                else:
                    assert self.state in [State.CLOSING, State.CLOSED]
                    # Wait until the connection is closed to raise
                    # ConnectionClosed with the correct code and reason.
                    await self.ensure_open()

        # Pop a message from the queue.
        message = self.messages.popleft()

        # Notify transfer_data().
        if self._put_message_waiter is not None:
            self._put_message_waiter.set_result(None)
            self._put_message_waiter = None

        return message

    async def send(
        self, message: Union[Data, Iterable[Data], AsyncIterable[Data]]
    ) -> None:
        """
        This coroutine sends a message.

        It sends a string (:class:`str`) as a text frame and a bytes-like
        object (:class:`bytes`, :class:`bytearray`, or :class:`memoryview`)
        as a binary frame.

        It also accepts an iterable or an asynchronous iterator of strings or
        bytes-like objects. Each item is treated as a message fragment and
        sent in its own frame. All items must be of the same type, or else
        :meth:`send` will raise a :exc:`TypeError` and the connection will be
        closed.

        It raises a :exc:`TypeError` for other inputs.

        """
        await self.ensure_open()

        # While sending a fragmented message, prevent sending other messages
        # until all fragments are sent.
        while self._fragmented_message_waiter is not None:
            await asyncio.shield(self._fragmented_message_waiter)

        # Unfragmented message -- this case must be handled first because
        # strings and bytes-like objects are iterable.

        if isinstance(message, (str, bytes, bytearray, memoryview)):
            opcode, data = prepare_data(message)
            await self.write_frame(True, opcode, data)

        # Fragmented message -- regular iterator.

        elif isinstance(message, Iterable):

            # Work around https://github.com/python/mypy/issues/6227
            message = cast(Iterable[Data], message)

            iter_message = iter(message)
            try:
                message_chunk = next(iter_message)
            except StopIteration:
                return
            opcode, data = prepare_data(message_chunk)

            self._fragmented_message_waiter = asyncio.Future()
            try:
                # First fragment.
                await self.write_frame(False, opcode, data)

                # Other fragments.
                for message_chunk in iter_message:
                    confirm_opcode, data = prepare_data(message_chunk)
                    if confirm_opcode != opcode:
                        raise TypeError("data contains inconsistent types")
                    await self.write_frame(False, OP_CONT, data)

                # Final fragment.
                await self.write_frame(True, OP_CONT, b"")

            except Exception:
                # We're half-way through a fragmented message and we can't
                # complete it. This makes the connection unusable.
                self.fail_connection(1011)
                raise

            finally:
                self._fragmented_message_waiter.set_result(None)
                self._fragmented_message_waiter = None

        # Fragmented message -- asynchronous iterator

        elif isinstance(message, AsyncIterable):
            # aiter_message = aiter(message) without aiter
            aiter_message = type(message).__aiter__(message)
            try:
                # message_chunk = anext(aiter_message) without anext
                message_chunk = await type(aiter_message).__anext__(aiter_message)
            except StopAsyncIteration:
                return
            opcode, data = prepare_data(message_chunk)

            self._fragmented_message_waiter = asyncio.Future()
            try:
                # First fragment.
                await self.write_frame(False, opcode, data)

                # Other fragments.
                async for message_chunk in aiter_message:
                    confirm_opcode, data = prepare_data(message_chunk)
                    if confirm_opcode != opcode:
                        raise TypeError("data contains inconsistent types")
                    await self.write_frame(False, OP_CONT, data)

                # Final fragment.
                await self.write_frame(True, OP_CONT, b"")

            except Exception:
                # We're half-way through a fragmented message and we can't
                # complete it. This makes the connection unusable.
                self.fail_connection(1011)
                raise

            finally:
                self._fragmented_message_waiter.set_result(None)
                self._fragmented_message_waiter = None

        else:
            raise TypeError("data must be bytes, str, or iterable")

    async def close(self, code: int = 1000, reason: str = "") -> None:
        """
        This coroutine performs the closing handshake.

        It waits for the other end to complete the handshake and for the TCP
        connection to terminate. As a consequence, there's no need to await
        :meth:`wait_closed`; :meth:`close` already does it.

        :meth:`close` is idempotent: it doesn't do anything once the
        connection is closed.

        It's safe to wrap this coroutine in :func:`~asyncio.create_task` since
        errors during connection termination aren't particularly useful.

        ``code`` must be an :class:`int` and ``reason`` a :class:`str`.

        """
        try:
            await asyncio.wait_for(
                self.write_close_frame(serialize_close(code, reason)),
                self.close_timeout,
                loop=self.loop,
            )
        except asyncio.TimeoutError:
            # If the close frame cannot be sent because the send buffers
            # are full, the closing handshake won't complete anyway.
            # Fail the connection to shut down faster.
            self.fail_connection()

        # If no close frame is received within the timeout, wait_for() cancels
        # the data transfer task and raises TimeoutError.

        # If close() is called multiple times concurrently and one of these
        # calls hits the timeout, the data transfer task will be cancelled.
        # Other calls will receive a CancelledError here.

        try:
            # If close() is canceled during the wait, self.transfer_data_task
            # is canceled before the timeout elapses.
            # This helps closing connections when shutting down a server.
            await asyncio.wait_for(
                self.transfer_data_task, self.close_timeout, loop=self.loop
            )
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pass

        # Wait for the close connection task to close the TCP connection.
        await asyncio.shield(self.close_connection_task)

    async def ping(self, data: Optional[bytes] = None) -> Awaitable[None]:
        """
        This coroutine sends a ping.

        It returns a :class:`~asyncio.Future` which will be completed when the
        corresponding pong is received and which you may ignore if you don't
        want to wait.

        A ping may serve as a keepalive or as a check that the remote endpoint
        received all messages up to this point::

            pong_waiter = await ws.ping()
            await pong_waiter   # only if you want to wait for the pong

        By default, the ping contains four random bytes. The content may be
        overridden with the optional ``data`` argument which must be a string
        (which will be encoded to UTF-8) or a bytes-like object.

        """
        await self.ensure_open()

        if data is not None:
            data = encode_data(data)

        # Protect against duplicates if a payload is explicitly set.
        if data in self.pings:
            raise ValueError("already waiting for a pong with the same data")

        # Generate a unique random payload otherwise.
        while data is None or data in self.pings:
            data = struct.pack("!I", random.getrandbits(32))

        self.pings[data] = self.loop.create_future()

        await self.write_frame(True, OP_PING, data)

        return asyncio.shield(self.pings[data])

    async def pong(self, data: bytes = b"") -> None:
        """
        This coroutine sends a pong.

        An unsolicited pong may serve as a unidirectional heartbeat.

        The content may be overridden with the optional ``data`` argument
        which must be a string (which will be encoded to UTF-8) or a
        bytes-like object.

        """
        await self.ensure_open()

        data = encode_data(data)

        await self.write_frame(True, OP_PONG, data)

    # Private methods - no guarantees.

    def connection_closed_exc(self) -> ConnectionClosed:
        exception: ConnectionClosed
        if self.close_code == 1000 or self.close_code == 1001:
            exception = ConnectionClosedOK(self.close_code, self.close_reason)
        else:
            exception = ConnectionClosedError(self.close_code, self.close_reason)
        # Chain to the exception that terminated data transfer, if any.
        exception.__cause__ = self.transfer_data_exc
        return exception

    async def ensure_open(self) -> None:
        """
        Check that the WebSocket connection is open.

        Raise :exc:`~websockets.exceptions.ConnectionClosed` if it isn't.

        """
        # Handle cases from most common to least common for performance.
        if self.state is State.OPEN:
            # If self.transfer_data_task exited without a closing handshake,
            # self.close_connection_task may be closing it, going straight
            # from OPEN to CLOSED.
            if self.transfer_data_task.done():
                await asyncio.shield(self.close_connection_task)
                raise self.connection_closed_exc()
            else:
                return

        if self.state is State.CLOSED:
            raise self.connection_closed_exc()

        if self.state is State.CLOSING:
            # If we started the closing handshake, wait for its completion to
            # get the proper close code and status. self.close_connection_task
            # will complete within 4 or 5 * close_timeout after close(). The
            # CLOSING state also occurs when failing the connection. In that
            # case self.close_connection_task will complete even faster.
            await asyncio.shield(self.close_connection_task)
            raise self.connection_closed_exc()

        # Control may only reach this point in buggy third-party subclasses.
        assert self.state is State.CONNECTING
        raise InvalidState("WebSocket connection isn't established yet")

    async def transfer_data(self) -> None:
        """
        Read incoming messages and put them in a queue.

        This coroutine runs in a task until the closing handshake is started.

        """
        try:
            while True:
                message = await self.read_message()

                # Exit the loop when receiving a close frame.
                if message is None:
                    break

                # Wait until there's room in the queue (if necessary).
                if self.max_queue is not None:
                    while len(self.messages) >= self.max_queue:
                        self._put_message_waiter = self.loop.create_future()
                        try:
                            await asyncio.shield(self._put_message_waiter)
                        finally:
                            self._put_message_waiter = None

                # Put the message in the queue.
                self.messages.append(message)

                # Notify recv().
                if self._pop_message_waiter is not None:
                    self._pop_message_waiter.set_result(None)
                    self._pop_message_waiter = None

        except asyncio.CancelledError as exc:
            self.transfer_data_exc = exc
            # If fail_connection() cancels this task, avoid logging the error
            # twice and failing the connection again.
            raise

        except WebSocketProtocolError as exc:
            self.transfer_data_exc = exc
            self.fail_connection(1002)

        except (ConnectionError, EOFError) as exc:
            # Reading data with self.reader.readexactly may raise:
            # - most subclasses of ConnectionError if the TCP connection
            #   breaks, is reset, or is aborted;
            # - IncompleteReadError, a subclass of EOFError, if fewer
            #   bytes are available than requested.
            self.transfer_data_exc = exc
            self.fail_connection(1006)

        except UnicodeDecodeError as exc:
            self.transfer_data_exc = exc
            self.fail_connection(1007)

        except PayloadTooBig as exc:
            self.transfer_data_exc = exc
            self.fail_connection(1009)

        except Exception as exc:
            # This shouldn't happen often because exceptions expected under
            # regular circumstances are handled above. If it does, consider
            # catching and handling more exceptions.
            logger.error("Error in data transfer", exc_info=True)

            self.transfer_data_exc = exc
            self.fail_connection(1011)

    async def read_message(self) -> Optional[Data]:
        """
        Read a single message from the connection.

        Re-assemble data frames if the message is fragmented.

        Return ``None`` when the closing handshake is started.

        """
        frame = await self.read_data_frame(max_size=self.max_size)

        # A close frame was received.
        if frame is None:
            return None

        if frame.opcode == OP_TEXT:
            text = True
        elif frame.opcode == OP_BINARY:
            text = False
        else:  # frame.opcode == OP_CONT
            raise WebSocketProtocolError("unexpected opcode")

        # Shortcut for the common case - no fragmentation
        if frame.fin:
            return frame.data.decode("utf-8") if text else frame.data

        # 5.4. Fragmentation
        chunks: List[Data] = []
        max_size = self.max_size
        if text:
            decoder_factory = codecs.getincrementaldecoder("utf-8")
            # https://github.com/python/typeshed/pull/2752
            decoder = decoder_factory(errors="strict")  # type: ignore
            if max_size is None:

                def append(frame: Frame) -> None:
                    nonlocal chunks
                    chunks.append(decoder.decode(frame.data, frame.fin))

            else:

                def append(frame: Frame) -> None:
                    nonlocal chunks, max_size
                    chunks.append(decoder.decode(frame.data, frame.fin))
                    max_size -= len(frame.data)

        else:
            if max_size is None:

                def append(frame: Frame) -> None:
                    nonlocal chunks
                    chunks.append(frame.data)

            else:

                def append(frame: Frame) -> None:
                    nonlocal chunks, max_size
                    chunks.append(frame.data)
                    max_size -= len(frame.data)

        append(frame)

        while not frame.fin:
            frame = await self.read_data_frame(max_size=max_size)
            if frame is None:
                raise WebSocketProtocolError("incomplete fragmented message")
            if frame.opcode != OP_CONT:
                raise WebSocketProtocolError("unexpected opcode")
            append(frame)

        # mypy cannot figure out that chunks have the proper type.
        return ("" if text else b"").join(chunks)  # type: ignore

    async def read_data_frame(self, max_size: int) -> Optional[Frame]:
        """
        Read a single data frame from the connection.

        Process control frames received before the next data frame.

        Return ``None`` if a close frame is encountered before any data frame.

        """
        # 6.2. Receiving Data
        while True:
            frame = await self.read_frame(max_size)

            # 5.5. Control Frames
            if frame.opcode == OP_CLOSE:
                # 7.1.5.  The WebSocket Connection Close Code
                # 7.1.6.  The WebSocket Connection Close Reason
                self.close_code, self.close_reason = parse_close(frame.data)
                try:
                    # Echo the original data instead of re-serializing it with
                    # serialize_close() because that fails when the close frame
                    # is empty and parse_close() synthetizes a 1005 close code.
                    await self.write_close_frame(frame.data)
                except ConnectionClosed:
                    # It doesn't really matter if the connection was closed
                    # before we could send back a close frame.
                    pass
                return None

            elif frame.opcode == OP_PING:
                # Answer pings.
                ping_hex = frame.data.hex() or "[empty]"
                logger.debug(
                    "%s - received ping, sending pong: %s", self.side, ping_hex
                )
                await self.pong(frame.data)

            elif frame.opcode == OP_PONG:
                # Acknowledge pings on solicited pongs.
                if frame.data in self.pings:
                    # Acknowledge all pings up to the one matching this pong.
                    ping_id = None
                    ping_ids = []
                    while ping_id != frame.data:
                        ping_id, pong_waiter = self.pings.popitem(last=False)
                        ping_ids.append(ping_id)
                        if not pong_waiter.done():
                            pong_waiter.set_result(None)
                    pong_hex = binascii.hexlify(frame.data).decode() or "[empty]"
                    logger.debug(
                        "%s - received solicited pong: %s", self.side, pong_hex
                    )
                    ping_ids = ping_ids[:-1]
                    if ping_ids:
                        pings_hex = ", ".join(
                            binascii.hexlify(ping_id).decode() or "[empty]"
                            for ping_id in ping_ids
                        )
                        plural = "s" if len(ping_ids) > 1 else ""
                        logger.debug(
                            "%s - acknowledged previous ping%s: %s",
                            self.side,
                            plural,
                            pings_hex,
                        )
                else:
                    pong_hex = binascii.hexlify(frame.data).decode() or "[empty]"
                    logger.debug(
                        "%s - received unsolicited pong: %s", self.side, pong_hex
                    )

            # 5.6. Data Frames
            else:
                return frame

    async def read_frame(self, max_size: int) -> Frame:
        """
        Read a single frame from the connection.

        """
        frame = await Frame.read(
            self.reader.readexactly,
            mask=not self.is_client,
            max_size=max_size,
            extensions=self.extensions,
        )
        logger.debug("%s < %r", self.side, frame)
        return frame

    async def write_frame(
        self, fin: bool, opcode: int, data: bytes, *, _expected_state: int = State.OPEN
    ) -> None:
        # Defensive assertion for protocol compliance.
        if self.state is not _expected_state:  # pragma: no cover
            raise InvalidState(
                f"Cannot write to a WebSocket in the {self.state.name} state"
            )

        frame = Frame(fin, opcode, data)
        logger.debug("%s > %r", self.side, frame)
        frame.write(self.writer.write, mask=self.is_client, extensions=self.extensions)

        try:
            # drain() cannot be called concurrently by multiple coroutines:
            # http://bugs.python.org/issue29930. Remove this lock when no
            # version of Python where this bugs exists is supported anymore.
            async with self._drain_lock:
                # Handle flow control automatically.
                await self.writer.drain()
        except ConnectionError:
            # Terminate the connection if the socket died.
            self.fail_connection()
            # Wait until the connection is closed to raise ConnectionClosed
            # with the correct code and reason.
            await self.ensure_open()

    async def write_close_frame(self, data: bytes = b"") -> None:
        """
        Write a close frame if and only if the connection state is OPEN.

        This dedicated coroutine must be used for writing close frames to
        ensure that at most one close frame is sent on a given connection.

        """
        # Test and set the connection state before sending the close frame to
        # avoid sending two frames in case of concurrent calls.
        if self.state is State.OPEN:
            # 7.1.3. The WebSocket Closing Handshake is Started
            self.state = State.CLOSING
            logger.debug("%s - state = CLOSING", self.side)

            # 7.1.2. Start the WebSocket Closing Handshake
            await self.write_frame(True, OP_CLOSE, data, _expected_state=State.CLOSING)

    async def keepalive_ping(self) -> None:
        """
        Send a Ping frame and wait for a Pong frame at regular intervals.

        This coroutine exits when the connection terminates and one of the
        following happens:

        - :meth:`ping` raises :exc:`ConnectionClosed`, or
        - :meth:`close_connection` cancels :attr:`keepalive_ping_task`.

        """
        if self.ping_interval is None:
            return

        try:
            while True:
                await asyncio.sleep(self.ping_interval, loop=self.loop)

                # ping() raises CancelledError if the connection is closed,
                # when close_connection() cancels self.keepalive_ping_task.

                # ping() raises ConnectionClosed if the connection is lost,
                # when connection_lost() calls abort_keepalive_pings().

                ping_waiter = await self.ping()

                if self.ping_timeout is not None:
                    try:
                        await asyncio.wait_for(
                            ping_waiter, self.ping_timeout, loop=self.loop
                        )
                    except asyncio.TimeoutError:
                        logger.debug("%s ! timed out waiting for pong", self.side)
                        self.fail_connection(1011)
                        break

        except asyncio.CancelledError:
            raise

        except ConnectionClosed:
            pass

        except Exception:
            logger.warning("Unexpected exception in keepalive ping task", exc_info=True)

    async def close_connection(self) -> None:
        """
        7.1.1. Close the WebSocket Connection

        When the opening handshake succeeds, :meth:`connection_open` starts
        this coroutine in a task. It waits for the data transfer phase to
        complete then it closes the TCP connection cleanly.

        When the opening handshake fails, :meth:`fail_connection` does the
        same. There's no data transfer phase in that case.

        """
        try:
            # Wait for the data transfer phase to complete.
            if hasattr(self, "transfer_data_task"):
                try:
                    await self.transfer_data_task
                except asyncio.CancelledError:
                    pass

            # Cancel the keepalive ping task.
            if hasattr(self, "keepalive_ping_task"):
                self.keepalive_ping_task.cancel()

            # A client should wait for a TCP close from the server.
            if self.is_client and hasattr(self, "transfer_data_task"):
                if await self.wait_for_connection_lost():
                    return
                logger.debug("%s ! timed out waiting for TCP close", self.side)

            # Half-close the TCP connection if possible (when there's no TLS).
            if self.writer.can_write_eof():
                logger.debug("%s x half-closing TCP connection", self.side)
                self.writer.write_eof()

                if await self.wait_for_connection_lost():
                    return
                logger.debug("%s ! timed out waiting for TCP close", self.side)

        finally:
            # The try/finally ensures that the transport never remains open,
            # even if this coroutine is canceled (for example).

            # If connection_lost() was called, the TCP connection is closed.
            # However, if TLS is enabled, the transport still needs closing.
            # Else asyncio complains: ResourceWarning: unclosed transport.
            if self.connection_lost_waiter.done() and not self.secure:
                return

            # Close the TCP connection. Buffers are flushed asynchronously.
            logger.debug("%s x closing TCP connection", self.side)
            self.writer.close()

            if await self.wait_for_connection_lost():
                return
            logger.debug("%s ! timed out waiting for TCP close", self.side)

            # Abort the TCP connection. Buffers are discarded.
            logger.debug("%s x aborting TCP connection", self.side)
            # mypy thinks self.writer.transport is a BaseTransport, not a Transport.
            self.writer.transport.abort()  # type: ignore

            # connection_lost() is called quickly after aborting.
            await self.wait_for_connection_lost()

    async def wait_for_connection_lost(self) -> bool:
        """
        Wait until the TCP connection is closed or ``self.close_timeout`` elapses.

        Return ``True`` if the connection is closed and ``False`` otherwise.

        """
        if not self.connection_lost_waiter.done():
            try:
                await asyncio.wait_for(
                    asyncio.shield(self.connection_lost_waiter),
                    self.close_timeout,
                    loop=self.loop,
                )
            except asyncio.TimeoutError:
                pass
        # Re-check self.connection_lost_waiter.done() synchronously because
        # connection_lost() could run between the moment the timeout occurs
        # and the moment this coroutine resumes running.
        return self.connection_lost_waiter.done()

    def fail_connection(self, code: int = 1006, reason: str = "") -> None:
        """
        7.1.7. Fail the WebSocket Connection

        This requires:

        1. Stopping all processing of incoming data, which means cancelling
           :attr:`transfer_data_task`. The close code will be 1006 unless a
           close frame was received earlier.

        2. Sending a close frame with an appropriate code if the opening
           handshake succeeded and the other side is likely to process it.

        3. Closing the connection. :meth:`close_connection` takes care of
           this once :attr:`transfer_data_task` exits after being canceled.

        (The specification describes these steps in the opposite order.)

        """
        logger.debug(
            "%s ! failing %s WebSocket connection with code %d",
            self.side,
            self.state.name,
            code,
        )

        # Cancel transfer_data_task if the opening handshake succeeded.
        # cancel() is idempotent and ignored if the task is done already.
        if hasattr(self, "transfer_data_task"):
            self.transfer_data_task.cancel()

        # Send a close frame when the state is OPEN (a close frame was already
        # sent if it's CLOSING), except when failing the connection because of
        # an error reading from or writing to the network.
        # Don't send a close frame if the connection is broken.
        if code != 1006 and self.state is State.OPEN:

            frame_data = serialize_close(code, reason)

            # Write the close frame without draining the write buffer.

            # Keeping fail_connection() synchronous guarantees it can't
            # get stuck and simplifies the implementation of the callers.
            # Not drainig the write buffer is acceptable in this context.

            # This duplicates a few lines of code from write_close_frame()
            # and write_frame().

            self.state = State.CLOSING
            logger.debug("%s - state = CLOSING", self.side)

            frame = Frame(True, OP_CLOSE, frame_data)
            logger.debug("%s > %r", self.side, frame)
            frame.write(
                self.writer.write, mask=self.is_client, extensions=self.extensions
            )

        # Start close_connection_task if the opening handshake didn't succeed.
        if not hasattr(self, "close_connection_task"):
            self.close_connection_task = self.loop.create_task(self.close_connection())

    def abort_keepalive_pings(self) -> None:
        """
        Raise ConnectionClosed in pending keepalive pings.

        They'll never receive a pong once the connection is closed.

        """
        assert self.state is State.CLOSED
        exc = self.connection_closed_exc()

        for ping in self.pings.values():
            ping.set_exception(exc)

        if self.pings:
            pings_hex = ", ".join(
                binascii.hexlify(ping_id).decode() or "[empty]"
                for ping_id in self.pings
            )
            plural = "s" if len(self.pings) > 1 else ""
            logger.debug(
                "%s - aborted pending ping%s: %s", self.side, plural, pings_hex
            )

    # asyncio.StreamReaderProtocol methods

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        """
        Configure write buffer limits.

        The high-water limit is defined by ``self.write_limit``.

        The low-water limit currently defaults to ``self.write_limit // 4`` in
        :meth:`~asyncio.WriteTransport.set_write_buffer_limits`, which should
        be all right for reasonable use cases of this library.

        This is the earliest point where we can get hold of the transport,
        which means it's the best point for configuring it.

        """
        logger.debug("%s - event = connection_made(%s)", self.side, transport)
        # mypy thinks transport is a BaseTransport, not a Transport.
        transport.set_write_buffer_limits(self.write_limit)  # type: ignore
        super().connection_made(transport)

    def eof_received(self) -> bool:
        """
        Close the transport after receiving EOF.

        Since Python 3.5, `:meth:~StreamReaderProtocol.eof_received` returns
        ``True`` on non-TLS connections.

        See http://bugs.python.org/issue24539 for more information.

        This is inappropriate for websockets for at least three reasons:

        1. The use case is to read data until EOF with self.reader.read(-1).
           Since websockets is a TLV protocol, this never happens.

        2. It doesn't work on TLS connections. A falsy value must be
           returned to have the same behavior on TLS and plain connections.

        3. The websockets protocol has its own closing handshake. Endpoints
           close the TCP connection after sending a close frame.

        As a consequence we revert to the previous, more useful behavior.

        """
        logger.debug("%s - event = eof_received()", self.side)
        super().eof_received()
        return False

    def connection_lost(self, exc: Optional[Exception]) -> None:
        """
        7.1.4. The WebSocket Connection is Closed.

        """
        logger.debug("%s - event = connection_lost(%s)", self.side, exc)
        self.state = State.CLOSED
        logger.debug("%s - state = CLOSED", self.side)
        if not hasattr(self, "close_code"):
            self.close_code = 1006
        if not hasattr(self, "close_reason"):
            self.close_reason = ""
        logger.debug(
            "%s x code = %d, reason = %s",
            self.side,
            self.close_code,
            self.close_reason or "[no reason]",
        )
        self.abort_keepalive_pings()
        # If self.connection_lost_waiter isn't pending, that's a bug, because:
        # - it's set only here in connection_lost() which is called only once;
        # - it must never be canceled.
        self.connection_lost_waiter.set_result(None)
        super().connection_lost(exc)
