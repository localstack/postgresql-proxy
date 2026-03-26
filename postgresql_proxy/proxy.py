'''For every configured instance, a Proxy object is created, that starts a listener.
On connect, it initiates a parallel connection to postgresql and pairs them together.
Using selectors, packets are received, intercepted and relayed to the other party.

Protocol:
The challenge is in identifying 3 types of packets:
1. With type and data.
   ex. 1 byte for type identifier, 4 bytes header for header and body length, body. Usually the body is ended with
   0x00 byte as well, that is part of the length.
   The queries are part of this type of packets. A query is b'Q####SELECT whatever\\x00'
2. Without type. They contain just a 4 byte header with packet length. It just so happens that the first byte is 0x00
   just because nothing is that long. These contain information about connection.
   Usually it's the client sending connection information. Ex.
        b'x00x00x00O' - length
        b'x00x03x00x00' - unexplained
        then, separated by x00 is a list of key, value: user, database, application_name, client_encoding, etc
        then, ended by b'x00'
3. Without data. Just the type. Since it's b'N', it might be "null"? The whole packet is this single byte.
   Signals "ok" according to wireshark

Handling:
proxy.py - connections and sockets things
connection.py - parsing and composing packets, launching interceptors
interceptors.py - intercepting for modification
'''

from __future__ import annotations

import logging
import selectors
import socket
import ssl
from types import ModuleType

from postgresql_proxy import connection, config_schema as cfg
from postgresql_proxy.interceptors import ResponseInterceptor, CommandInterceptor

LOG = logging.getLogger("postgresql_proxy")


class SelectorKeyProxy(selectors.SelectorKey):
    fileobj: socket.socket
    data: connection.Connection
    fd: int
    events: int


class Proxy(object):
    def __init__(
        self,
        instance_config: cfg.InstanceSettings,
        plugins: dict[str, ModuleType],
        debug: bool = False,
        ssl_context: ssl.SSLContext | None = None,
    ) -> None:
        self.plugins = plugins
        self.num_clients = 0
        self.instance_config = instance_config
        self.connections = []
        self.selector = selectors.DefaultSelector()
        self.running = True
        self.sock = None
        self.ssl_context = ssl_context

        # this is used to track leftover sockets
        self._debug = debug
        if self._debug:
            self._registered_conn = set()

    def _create_pg_connection(self, address, context):
        redirect_config = self.instance_config.redirect

        pg_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        pg_sock.connect((redirect_config.host, redirect_config.port))
        pg_sock.setblocking(False)

        events = selectors.EVENT_READ
        redirect_config_name = redirect_config.name + '_' + str(self.num_clients)
        pg_conn = connection.Connection(
            pg_sock,
            name=redirect_config_name,
            address=address,
            events=events,
            context=context
        )

        LOG.info("initiated client connection to %s:%s called %s",
                 redirect_config.host, redirect_config.port, redirect_config_name)
        return pg_conn

    def _register_conn(self, conn: connection.Connection):
        try:
            self.selector.register(conn.sock, conn.events, data=conn)
        except Exception as e:
            # potentially already registered - this can happen if file descriptors
            # are reused for new sockets -> try to unregister/re-register
            LOG.debug("exception while trying to register %s: %s", conn.name, e)
            self.selector.modify(conn.sock, conn.events, data=conn)

        if self._debug:
            self._registered_conn.add(f"{conn.name}-{conn.sock.fileno()}")

    def _unregister_conn(self, conn: connection.Connection):
        LOG.debug("closing connection %s", conn.name)
        self.selector.unregister(conn.sock)
        if conn.name.startswith("proxy") and not conn.terminated:
            # send Terminate to PG to not leave it hanging waiting for query
            # the client did not disconnect properly
            # this will cause postgres to close the socket on its side cleanly
            try:
                LOG.debug("try closing connection %s", conn.redirect_conn.name)
                conn.redirect_conn.sock.send(b'X\x00\x00\x00\x04')
                # remove reference to itself
                conn.redirect_conn.redirect_conn = None
            except OSError:
                # OSError includes all socket exceptions + Connection* related exceptions
                LOG.debug("tried closing connection %s: already closed", conn.redirect_conn.name)

        if self._debug:
            self._registered_conn.discard(f"{conn.name}-{conn.sock.fileno()}")

    def accept_wrapper(self, sock: socket.socket):
        """
        This method is called whenever a new client connects to the proxy. It will create a connection to postgres and
        proxy all data between both sockets. It will add a `Connection` object to the SelectorKey, to be able to
        store state and share data between the sockets.
        :param sock: the client socket
        :return:
        """

        # Accept the raw connection
        clientsocket, address = sock.accept()

        # Check if SSL is enabled for this proxy
        if self.ssl_context:
            # Handle SSL negotiation - must happen before setblocking(False)
            clientsocket = self._handle_ssl_negotiation(clientsocket, self.ssl_context)

        clientsocket.setblocking(False)
        self.num_clients += 1
        sock_name = f"{self.instance_config.listen.name}_{self.num_clients}"
        LOG.info(
            "Connection from %s, connection initiated %s (SSL: %s)",
            address,
            sock_name,
            self.ssl_context is not None,
        )

        events = selectors.EVENT_READ
        context = {"instance_config": self.instance_config}

        conn = connection.Connection(
            clientsocket,
            name=sock_name,
            address=address,
            events=events,
            context=context,
        )

        pg_conn = self._create_pg_connection(address, context)

        if (
            self.instance_config.intercept is not None
            and self.instance_config.intercept.responses is not None
        ):
            pg_conn.interceptor = ResponseInterceptor(
                self.instance_config.intercept.responses, self.plugins, context
            )
            pg_conn.redirect_conn = conn

        if (
            self.instance_config.intercept is not None
            and self.instance_config.intercept.commands is not None
        ):
            conn.interceptor = CommandInterceptor(
                self.instance_config.intercept.commands, self.plugins, context
            )
            conn.redirect_conn = pg_conn

        self._register_conn(conn)
        self._register_conn(pg_conn)

    def _handle_ssl_negotiation(
        self, client_socket: socket.socket, ssl_context: ssl.SSLContext
    ) -> socket.socket:
        """
        Handle PostgreSQL SSL negotiation on an accepted socket.

        PostgreSQL SSL flow:
        1. Client sends SSLRequest (8 bytes): length (4) + code 80877103 (4)
        2. Server responds 'S' (SSL supported) or 'N' (not supported)
        3. If 'S', TLS handshake follows
        4. After TLS, normal PostgreSQL protocol begins

        Returns the SSL-wrapped socket if negotiation succeeds, or the original socket.
        """

        # Peek at the first 8 bytes to check for SSLRequest
        # Using MSG_PEEK so we don't consume the data if it's not SSLRequest
        data = client_socket.recv(8, socket.MSG_PEEK)

        if len(data) == 8:
            length = int.from_bytes(data[:4], "big")
            code = int.from_bytes(data[4:8], "big")

            if length == 8 and code == 80877103:  # SSLRequest code
                # Consume the SSLRequest
                client_socket.recv(8)
                # Send 'S' to indicate SSL is supported
                client_socket.send(b"S")
                # Wrap socket with SSL
                ssl_socket = ssl_context.wrap_socket(client_socket, server_side=True)
                LOG.debug("SSL handshake completed for PostgreSQL connection")
                return ssl_socket

        # Not an SSLRequest, return original socket
        return client_socket

    def service_connection(self, key: SelectorKeyProxy, mask):
        """
        This method proxies the messages between socket. It will use properties of the Connection object to
        intercept and decode messages, modifies if needed, then send the message to the redirect_conn once it is
        fully built.
        :param key: SelectorKeyProxy, containing the socket and the Connection object
        :param mask: mask of event, indicating what the socket is ready for
        :return:
        """
        sock = key.fileobj
        conn = key.data
        if mask & selectors.EVENT_READ:
            LOG.debug('%s can receive', conn.name)
            try:
                recv_data = sock.recv(4096)  # Should be ready to read
                if recv_data:
                    LOG.debug('%s received data:\n%s', conn.name, recv_data)
                    conn.received(recv_data)
                else:
                    self._unregister_conn(conn)
                    LOG.debug('%s connection closing %s', conn.name, conn.address)
                    # A file object shall be unregistered prior to being closed.
                    sock.close()
            except OSError as e:
                # it means the socket was closed by peer
                LOG.debug('%s connection closed by peer %s: %s', conn.name, conn.address, e)
                self._unregister_conn(conn)

        next_conn = conn.redirect_conn
        if next_conn and next_conn.out_bytes:
            try:
                while next_conn.out_bytes:
                    LOG.debug('sending to %s:\n%s', next_conn.name, next_conn.out_bytes)
                    sent = next_conn.sock.send(next_conn.out_bytes)
                    next_conn.sent(sent)
            except OSError:
                # If one side is closed, close the other one
                # this can happen in the case where the client disconnects, and postgres still return a response
                # we then read the response then close the PG side of the socket.
                LOG.debug('error sending to %s: connection closed', next_conn.name)
                self._unregister_conn(conn)
                sock.close()

    def listen(self, max_connections: int = 8):
        """
        Listen server socket. On connect, launch a selector polling for socket readiness to listen
        :param max_connections:
        :return:
        """
        lconf = self.instance_config.listen
        ip, port = (lconf.host, lconf.port)
        try:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.sock.bind((ip, port))
            self.sock.listen(max_connections)
            self.sock.setblocking(False)
            self.selector.register(self.sock, selectors.EVENT_READ, data=None)
            while self.running:
                events = self.selector.select(timeout=1)
                if not events:
                    LOG.debug("polling selector...")
                    continue

                for key, mask in events:
                    key: SelectorKeyProxy
                    if key.data is None:
                        # if the data object has not been set, it means the socket has not yet been accepted
                        self.accept_wrapper(key.fileobj)
                    else:
                        # manage the already proxied connections
                        self.service_connection(key, mask)

        except OSError as ex:
            LOG.error("Can't establish PostgreSQL proxy listener on port %s" % port, exc_info=ex)
        except Exception:
            LOG.exception("PostgreSQL proxy quit unexpectedly:")
        finally:
            LOG.info("Closing PostgreSQL proxy on port %s" % port)
            self.selector.unregister(self.sock)
            # this cleans up in case any connection was still opened
            # it should not happen anymore
            if self._debug:
                LOG.debug("Registered connections dangling: %s", self._registered_conn)
            registered_selector_sockets = [skey for i, skey in self.selector.get_map().items()]
            for selector_key in registered_selector_sockets:
                LOG.debug("Connection left: %s", selector_key)
                selector_key: SelectorKeyProxy
                try:
                    self.selector.unregister(selector_key.fileobj)
                    selector_key.fileobj.close()
                except OSError:
                    continue

            self.selector.close()
            self.sock.close()
            self.sock = None

    def stop(self):
        self.running = False


if __name__ == '__main__':
    import importlib
    import yaml
    import os

    path = os.path.dirname(os.path.realpath(__file__))
    config = None
    try:
        with open(path + '/' + 'config.yml', 'r') as fp:
            config = cfg.Config(yaml.load(fp))
    except Exception:
        logging.critical("Could not read config. Aborting.")
        exit(1)

    logging.basicConfig(
        filename=config.settings.general_log,
        level=getattr(logging, config.settings.log_level.upper()),
        format='%(asctime)s : %(levelname)s : %(message)s'
    )

    qlog = logging.getLogger('intercept')
    qformat = logging.Formatter('%(asctime)s : %(message)s')
    qhandler = logging.FileHandler(config.settings.intercept_log, mode = 'w')
    qhandler.setFormatter(qformat)
    qlog.addHandler(qhandler)
    qlog.setLevel(logging.DEBUG)

    print('general log, level {}: {}'.format(config.settings.log_level, config.settings.general_log))
    print('intercept log: {}'.format(config.settings.intercept_log))
    print('further messages directed to log')

    plugins = {}
    for plugin in config.plugins:
        logging.info("Loading module %s", plugin)
        module = importlib.import_module('plugins.' + plugin)
        plugins[plugin] = module

    for instance in config.instances:
        logging.info("Starting proxy instance")
        proxy = Proxy(instance, plugins)
        proxy.listen()
