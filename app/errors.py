"""RFC 9457 application/problem+json errors.

Every error carries the RFC members `type`, `title`, `status`, `detail` and `instance`, plus a
stable machine-readable extension `code` (BAD_USER_INPUT, NOT_FOUND, CONFLICT,
INTERNAL_SERVER_ERROR) that clients switch on. `type` is a relative URI naming the problem class
(`/problems/not-found`, served by app/routers/problems.py); an error with no semantics beyond its
HTTP status (a 405, say) uses `about:blank`, as the RFC prescribes. `instance` is the request path.
The metrics middleware reads the code from the ASGI scope (`scope["error_code"]`) to increment
http_errors_total.
"""

from collections.abc import MutableMapping
from dataclasses import dataclass
from http import HTTPStatus
from typing import Any
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

PROBLEM_JSON = "application/problem+json"
PROBLEM_BASE = "/problems"
VALIDATION_DETAIL = "The request content was invalid or failed validation constraints."

InvalidParam = dict[str, str]


@dataclass(frozen=True)
class ProblemType:
    slug: str
    code: str
    status: int
    description: str

    @property
    def uri(self) -> str:
        return f"{PROBLEM_BASE}/{self.slug}"

    @property
    def title(self) -> str:
        return HTTPStatus(self.status).phrase


PROBLEM_TYPES: dict[str, ProblemType] = {
    t.slug: t
    for t in (
        ProblemType(
            "bad-user-input",
            "BAD_USER_INPUT",
            400,
            "The request was malformed or failed validation: bad JSON, a missing or blank "
            "required field, a value out of range, or a malformed id. The `invalidParams` member "
            "lists every offending field at once.",
        ),
        ProblemType(
            "not-found",
            "NOT_FOUND",
            404,
            "The resource, or a resource the request refers to (such as the author of a new "
            "message), does not exist. Also returned for an unknown route.",
        ),
        ProblemType(
            "conflict",
            "CONFLICT",
            409,
            "The request conflicts with the current state of the resource: a stale `version` on "
            "update (re-read and retry), a duplicate author email, or deleting an author who "
            "still has messages.",
        ),
        ProblemType(
            "payload-too-large",
            "BAD_USER_INPUT",
            413,
            "The request body exceeds the maximum size the service accepts.",
        ),
        ProblemType(
            "internal-server-error",
            "INTERNAL_SERVER_ERROR",
            500,
            "An unexpected error occurred. The response carries no internals; the failure is "
            "logged server-side with the trace id.",
        ),
    )
}

# The type of an error that does not name one itself. payload-too-large shares BAD_USER_INPUT with
# the 400 case, so it is chosen explicitly (PayloadTooLargeError.type_slug), not by code.
_DEFAULT_TYPE_BY_CODE = {
    "BAD_USER_INPUT": "bad-user-input",
    "NOT_FOUND": "not-found",
    "CONFLICT": "conflict",
    "INTERNAL_SERVER_ERROR": "internal-server-error",
}


class ProblemParam(BaseModel):
    name: str = Field(examples=["title"])
    reason: str = Field(examples=["title is required and cannot be blank"])


class Problem(BaseModel):
    """RFC 9457 problem details as documented in OpenAPI; the wire body is built by
    problem_response."""

    type: str = Field(
        description="URI reference naming the problem class; `about:blank` when the status "
        "code alone says it all.",
        examples=["/problems/not-found"],
    )
    title: str = Field(description="The HTTP status phrase.", examples=["Not Found"])
    status: int = Field(examples=[404])
    detail: str = Field(
        description="Human-readable explanation of this occurrence. It may change: do not parse.",
        examples=["Message with ID '4b0c7e2a' was not found."],
    )
    instance: str = Field(
        description="URI reference identifying this occurrence: the request path.",
        examples=["/messages/4b0c7e2a-6a3f-4c1e-9d59-1d2f5c5b8a10"],
    )
    code: str = Field(
        description="Stable machine-readable error code. Switch on this.", examples=["NOT_FOUND"]
    )
    invalidParams: list[ProblemParam] | None = Field(
        default=None, description="Every invalid field, for validation failures."
    )


class ApiError(Exception):
    status = 500
    code = "INTERNAL_SERVER_ERROR"
    type_slug: str | None = None  # None: the default type for `code`

    def __init__(self, detail: str, invalid_params: list[InvalidParam] | None = None) -> None:
        super().__init__(detail)
        self.detail = detail
        self.invalid_params = invalid_params


class BadUserInputError(ApiError):
    status = 400
    code = "BAD_USER_INPUT"


class NotFoundError(ApiError):
    status = 404
    code = "NOT_FOUND"


class ConflictError(ApiError):
    status = 409
    code = "CONFLICT"


class PayloadTooLargeError(ApiError):
    status = 413
    code = "BAD_USER_INPUT"
    type_slug = "payload-too-large"


def problem_response(
    request_scope: MutableMapping[str, Any],
    status: int,
    code: str,
    detail: str,
    invalid_params: list[InvalidParam] | None = None,
    type_slug: str | None = None,
) -> JSONResponse:
    request_scope["error_code"] = code
    slug = type_slug or _DEFAULT_TYPE_BY_CODE.get(code)
    body: dict[str, Any] = {
        "type": PROBLEM_TYPES[slug].uri if slug else "about:blank",
        "title": HTTPStatus(status).phrase,
        "status": status,
        "detail": detail,
        # scope["path"] is decoded; re-encode it so instance stays a valid URI reference.
        "instance": quote(request_scope["path"], safe="/"),
        "code": code,
    }
    if invalid_params:
        body["invalidParams"] = invalid_params
    return JSONResponse(body, status_code=status, media_type=PROBLEM_JSON)


def _param_name(loc: tuple[Any, ...]) -> str:
    # loc is ("body", "title"), ("query", "limit") or ("path", "id"): the field is the tail. A
    # malformed body reports ("body", <char offset>): an int, not a field name.
    parts = [p for p in loc if isinstance(p, str) and p not in ("body", "query", "path")]
    return ".".join(parts) or "body"


def _reason(name: str, error: dict[str, Any]) -> str:
    kind = error["type"]
    ctx = error.get("ctx") or {}
    if kind == "missing" or (kind == "string_type" and error.get("input") is None):
        return f"{name} is required and cannot be blank"
    if kind == "greater_than_equal":
        return (
            f"{name} must not be negative"
            if ctx.get("ge") == 0
            else f"{name} must be at least {ctx.get('ge')}"
        )
    if kind == "less_than_equal":
        return f"{name} must not exceed {ctx.get('le')}"
    if kind == "int_parsing":
        return f"{name} must be an integer"
    if kind == "uuid_parsing":
        return f"{name} must be a valid UUID"
    if kind == "string_type":
        return f"{name} must be a string"
    if kind == "literal_error":
        return f"{name} must be one of: {ctx.get('expected')}"
    if kind in ("json_invalid", "model_attributes_type", "dict_type"):
        return "request body must be a valid JSON object"
    if kind == "value_error":
        # Our own validators raise ValueError("<field> ..."); drop pydantic's prefix.
        return str(error["msg"]).removeprefix("Value error, ")
    return str(error["msg"])


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api_error(request: Request, exc: ApiError) -> JSONResponse:
        return problem_response(
            request.scope, exc.status, exc.code, exc.detail, exc.invalid_params, exc.type_slug
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        # FastAPI defaults to 422; this API answers 400 and lists every failing field at once.
        params: list[InvalidParam] = []
        for error in exc.errors():
            name = _param_name(tuple(error["loc"]))
            params.append({"name": name, "reason": _reason(name, dict(error))})
        return problem_response(request.scope, 400, "BAD_USER_INPUT", VALIDATION_DETAIL, params)

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = {404: "NOT_FOUND"}.get(exc.status_code, f"HTTP_{exc.status_code}")
        response = problem_response(request.scope, exc.status_code, code, str(exc.detail))
        if exc.headers:
            response.headers.update(exc.headers)
        return response


_RESPONSE_DESCRIPTIONS = {
    400: "Validation failed or the request was malformed (BAD_USER_INPUT).",
    404: "The resource does not exist (NOT_FOUND).",
    409: "The request conflicts with the current state (CONFLICT).",
    413: "The request body is too large (BAD_USER_INPUT).",
    500: "Unexpected error (INTERNAL_SERVER_ERROR).",
}


def problem_responses(*statuses: int) -> dict[int | str, dict[str, Any]]:
    """`responses=` entries that document problem+json errors in OpenAPI."""
    return {s: {"model": Problem, "description": _RESPONSE_DESCRIPTIONS[s]} for s in statuses}


def install_openapi(app: FastAPI) -> None:
    """Make the generated OpenAPI document describe the errors this API really returns.

    FastAPI documents a 422 HTTPValidationError on every route with input and files error models
    under application/json; here validation answers 400 and errors are application/problem+json.
    """
    generate = app.openapi

    def openapi() -> dict[str, Any]:
        if app.openapi_schema is None:
            _problemize(generate())  # generate() caches the dict it returns: edit it in place
        return app.openapi_schema or {}

    app.openapi = openapi


def _problemize(schema: dict[str, Any]) -> None:
    for path_item in schema.get("paths", {}).values():
        for operation in path_item.values():
            responses: dict[str, Any] = operation.get("responses", {})
            responses.pop("422", None)
            for status, response in responses.items():
                content: dict[str, Any] = response.get("content", {})
                if int(status) >= 400 and "application/json" in content:
                    content[PROBLEM_JSON] = content.pop("application/json")
    schemas: dict[str, Any] = schema.get("components", {}).get("schemas", {})
    for name in ("HTTPValidationError", "ValidationError"):
        schemas.pop(name, None)
