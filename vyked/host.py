import asyncio
import logging
from functools import partial
import signal
import os

from vyked.bus import MessageBus
from vyked.services import HTTPService, TCPService
from .protocol_factory import get_vyked_protocol
from aiohttp.web import Application

_logger = logging.getLogger(__name__)


class Host:

    registry = None
    pubsub = None
    name = None
    ronin = True
    _host_id = None
    _tcp_service = None
    _http_service = None
    _bus = MessageBus()

    @classmethod
    def _set_process_name(cls):
        from setproctitle import setproctitle
        setproctitle('{}_{}'.format(cls.name, cls._host_id))

    @classmethod
    def _stop(cls, signame: str):
        _logger.info('\ngot signal {} - exiting'.format(signame))
        asyncio.get_event_loop().stop()

    @classmethod
    def attach_service(cls, service):
        service.bus = cls._bus
        if isinstance(service, HTTPService):
            cls._http_service = service
            cls._bus._http_host = service
        elif isinstance(service, TCPService):
            cls._tcp_service = service
            cls._bus._tcp_host = service
        else:
            _logger.error('Invalid argument attached as service')

    @classmethod
    def run(cls):
        if cls._tcp_service or cls._http_service:
            cls._set_host_id()
            cls._set_process_name()
            cls._set_signal_handlers()
            cls._start_server()
        else:
            _logger.error('No services to host')

    @classmethod
    def _set_signal_handlers(cls):
        asyncio.get_event_loop().add_signal_handler(getattr(signal, 'SIGINT'), partial(cls._stop, 'SIGINT'))
        asyncio.get_event_loop().add_signal_handler(getattr(signal, 'SIGTERM'), partial(cls._stop, 'SIGTERM'))

    @classmethod
    def _create_tcp_server(cls):
        if cls._tcp_service:
            host_ip, host_port = cls._tcp_service.socket_address
            task = asyncio.get_event_loop().create_server(get_vyked_protocol(cls._bus), host_ip, host_port)
            return asyncio.get_event_loop().run_until_complete(task)

    @classmethod
    def _create_http_server(cls):
        if cls._http_service:
            host_ip, host_port = cls._http_service.socket_address
            ssl_context = cls._http_service.ssl_context
            app = Application(loop=asyncio.get_event_loop())
            for each in cls._http_service.__ordered__:
                fn = getattr(cls._http_service, each)
                if callable(fn) and getattr(fn, 'is_http_method', False):
                    for path in fn.paths:
                        app.router.add_route(fn.method, path, fn)
                        if cls._http_service.cross_domain_allowed:
                            app.router.add_route('options', path, cls._http_service.preflight_response)
            fn = getattr(cls._http_service, 'pong')
            app.router.add_route('GET', '/ping', fn)
            handler = app.make_handler()
            task = asyncio.get_event_loop().create_server(handler, host_ip, host_port, ssl=ssl_context)
            return asyncio.get_event_loop().run_until_complete(task)
        
    @classmethod
    def _set_host_id(cls):
        from uuid import uuid4
        cls._host_id = uuid4()

    @classmethod
    def _start_server(cls):
        tcp_server = cls._create_tcp_server()
        http_server = cls._create_http_server()
        cls._create_pubsub_handler()
        if tcp_server:
            _logger.info('Serving TCP on {}'.format(tcp_server.sockets[0].getsockname()))
        if http_server:
            _logger.info('Serving HTTP on {}'.format(http_server.sockets[0].getsockname()))
        _logger.info("Event loop running forever, press CTRL+c to interrupt.")
        _logger.info("pid %s: send SIGINT or SIGTERM to exit." % os.getpid())

        try:
            asyncio.get_event_loop().run_forever()
        except Exception as e:
            print(e)
        finally:
            if tcp_server:
                tcp_server.close()
                asyncio.get_event_loop().run_until_complete(tcp_server.wait_closed())

            if http_server:
                http_server.close()
                asyncio.get_event_loop().run_until_complete(http_server.wait_closed())

            asyncio.get_event_loop().close()

    @classmethod
    def _create_pubsub_handler(cls):
        asyncio.get_event_loop().run_until_complete(cls.pubsub.connect())
