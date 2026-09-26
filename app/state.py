from dataclasses import dataclass


@dataclass
class AppState:
    # True only after the DB and cache are connected and the seed has run; back to False at the
    # start of shutdown. Read by GET /health/readiness, which must not touch any dependency.
    ready: bool = False


state = AppState()
