from fastapi import APIRouter
from pydantic import BaseModel

from app.errors import PROBLEM_TYPES, NotFoundError, ProblemType, problem_responses

router = APIRouter(prefix="/problems", tags=["problems"])


class ProblemTypeOut(BaseModel):
    """What a problem `type` URI resolves to: documentation, not an error body."""

    type: str
    title: str
    status: int
    code: str
    description: str


def _out(problem: ProblemType) -> ProblemTypeOut:
    return ProblemTypeOut(
        type=problem.uri,
        title=problem.title,
        status=problem.status,
        code=problem.code,
        description=problem.description,
    )


@router.get("", response_model=list[ProblemTypeOut])
async def list_problem_types() -> list[ProblemTypeOut]:
    return [_out(p) for p in PROBLEM_TYPES.values()]


@router.get("/{slug}", response_model=ProblemTypeOut, responses=problem_responses(404))
async def get_problem_type(slug: str) -> ProblemTypeOut:
    problem = PROBLEM_TYPES.get(slug)
    if problem is None:
        raise NotFoundError(f"Problem type '{slug}' was not found.")
    return _out(problem)
