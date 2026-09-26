from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.errors import PayloadTooLargeError, problem_response


class BodyLimitMiddleware:
    """Rejects request bodies over `max_bytes` with 413 problem+json.

    A declared Content-Length is refused up front; a chunked body is counted as it streams and
    fails the read (handled by the ApiError handler) once it crosses the limit. With Pydantic's
    max_length checks this is what bounds the cost of a single request.
    """

    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    def _too_large(self) -> PayloadTooLargeError:
        return PayloadTooLargeError(f"Request body must not exceed {self.max_bytes} bytes.")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        declared = dict(scope["headers"]).get(b"content-length", b"")
        if declared.isdigit() and int(declared) > self.max_bytes:
            error = self._too_large()
            response = problem_response(
                scope, error.status, error.code, error.detail, type_slug=error.type_slug
            )
            await response(scope, receive, send)
            return

        received = 0

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    raise self._too_large()
            return message

        await self.app(scope, limited_receive, send)
