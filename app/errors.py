from __future__ import annotations


class APIError(Exception):
    """业务错误，统一映射为带 error/code/details 的 JSON 响应。"""

    status_code = 400
    code = "BAD_REQUEST"

    def __init__(self, message: str, *, code: str | None = None, status_code: int | None = None,
                 details: dict | None = None):
        super().__init__(message)
        self.message = message
        if code:
            self.code = code
        if status_code:
            self.status_code = status_code
        self.details = details or {}


class NotFound(APIError):
    status_code = 404
    code = "NOT_FOUND"


class Conflict(APIError):
    status_code = 409
    code = "CONFLICT"


class UnprocessableShift(APIError):
    status_code = 409
    code = "SHIFT_NOT_SETTLED"
