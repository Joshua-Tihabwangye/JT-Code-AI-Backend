from contextvars import ContextVar

request_id_var: ContextVar[str] = ContextVar('request_id', default='-')
trace_id_var: ContextVar[str] = ContextVar('trace_id', default='-')
