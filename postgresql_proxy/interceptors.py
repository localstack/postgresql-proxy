import logging
from pg8000.converters import PG_PY_ENCODINGS

from postgresql_proxy.constants import ALLOWED_CONNECTION_PARAMETERS


class Interceptor:
    def __init__(self, interceptor_config, plugins, context):
        self.interceptor_config = interceptor_config
        self.plugins = plugins
        self.context = context

    def intercept(self, packet_type, data):
        return data

    def _get_plugin_interceptor_function(self, interceptor):
        if plugin := self.plugins.get(interceptor.plugin):
            if func := getattr(plugin, interceptor.function, None):
                return func

            else:
                raise Exception("Can't find function {} in plugin {}".format(
                    interceptor.function,
                    interceptor.plugin
                ))
        else:
            raise Exception("Plugin {} not loaded".format(interceptor.plugin))

    def get_codec(self):
        if self.context is not None and 'connect_params' in self.context:
            if self.context['connect_params'] is not None and 'client_encoding' in self.context['connect_params']:
                return self.convert_encoding_to_python(self.context['connect_params']['client_encoding'])
        return 'utf-8'

    @staticmethod
    def convert_encoding_to_python(encoding: str) -> str:
        encoding = encoding.lower()
        result = PG_PY_ENCODINGS.get(encoding, encoding)
        if not result:
            raise Exception(f"Encoding {encoding} not supported by postgresql-proxy!")
        return result


class CommandInterceptor(Interceptor):
    def intercept(self, packet_type, data):
        if self.interceptor_config.queries is not None:
            ic_queries = self.interceptor_config.queries
            if packet_type == b'Q':
                # Query, ends with b'\x00'
                data = self._intercept_query(data, ic_queries)
            elif packet_type == b'P':
                # Statement that needs parsing.
                # First byte of the body is some Statement flag. Ignore, don't lose
                # Next is the query, same as above, ends with an b'\x00'
                # Last 2 bytes are the number of parameters. Ignore, don't lose
                statement = data[0:1]
                query = self._intercept_query(data[1:-2], ic_queries)
                params = data[-2:]
                data = statement + query + params

        if packet_type == b'':
            # Connection request / context. Ignore the first 4 bytes, keep it
            packet_start = data[0:4]
            context_data = self._intercept_context_data(data[4:-1])
            data = packet_start + context_data

        return data

    def _intercept_context_data(self, data):
        # Each entry is terminated by b'\x00'
        entries = data.split(b'\x00')[:-1]
        entries = dict(zip(entries[0::2], entries[1::2]))
        self.context['connect_params'] = {}
        # Try to set codec, then transcode the dict
        if b'client_encoding' in entries:
            self.context['connect_params']['client_encoding'] = entries[b'client_encoding'].decode('ascii')
        codec = self.get_codec()
        for k, v in entries.items():
            key: str = k.decode(codec)
            # don't keep parameters not allowed by postgres
            if key.lower() not in ALLOWED_CONNECTION_PARAMETERS:
                continue
            self.context['connect_params'][k.decode(codec)] = v.decode(codec)

        context_data = b'\x00'.join(
            [
                key.encode(codec) + b'\x00' + value.encode(codec)
                for key, value in self.context['connect_params'].items()
            ]
        )
        return context_data + b'\x00\x00'

    def _intercept_query(self, query, interceptors):
        logging.getLogger('intercept').debug("intercepting query\n%s", query)
        # Remove zero byte at the end
        query = query[:-1].decode('utf-8')
        for interceptor in interceptors:
            func = self._get_plugin_interceptor_function(interceptor)
            query = func(query, self.context)
            logging.getLogger('intercept').debug(
                "modifying query using interceptor %s.%s\n%s",
                interceptor.plugin,
                interceptor.function,
                query)

        # Append the zero byte at the end
        return query.encode('utf-8') + b'\x00'


class ResponseInterceptor(Interceptor):
    def intercept(self, packet_type, data):
        if (ic_param_status := self.interceptor_config.parameter_status) is not None:
            if packet_type == b'S':
                # ParameterStatus, see https://www.postgresql.org/docs/current/protocol-message-formats.html#PROTOCOL-MESSAGE-FORMATS-PARAMETERSTATUS
                data = self._intercept_parameter_status(data, ic_param_status)

        return data

    def _intercept_parameter_status(self, data, interceptors):
        pos: int = data.find(b"\x00")
        key, value = data[:pos], data[pos + 1 : -1]
        key, value = key.decode("ascii"), value.decode("ascii")
        for interceptor in interceptors:
            func = self._get_plugin_interceptor_function(interceptor)
            key, value = func(key, value, self.context)
            logging.getLogger('intercept').debug(
                "modifying parameter status using interceptor %s.%s\nkey=%s value=%s",
                interceptor.plugin,
                interceptor.function,
                key,
                value,
            )

        data = key.encode("ascii") + b"\x00" + value.encode("ascii") + b"\x00"
        return data
