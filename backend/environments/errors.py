"""Failures owned by the configured executor and its actual transport."""

class ExecutorError(Exception):
    pass


class ExecutorProtocolError(ExecutorError):
    pass


class ExecutorDisconnected(ExecutorError):
    pass


class ExecutorClosed(ExecutorError):
    pass


class ExecutorEventsLagged(ExecutorError):
    def __init__(self, lost: int):
        super().__init__(f"executor process receiver lagged by {lost} events; recover through process/read")
        self.lost = lost


class ExecutorRpcError(ExecutorError):
    def __init__(self, code: int, message: str, data: object = None):
        super().__init__(message)
        self.code = code
        self.data = data

