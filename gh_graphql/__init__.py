"""Small stdlib-only client for the GitHub GraphQL API."""
import json
import os
import subprocess
import time
import urllib.error
import urllib.request
import warnings
from typing import Iterator

__all__ = ["token", "query", "paginate", "GraphQLError"]

API_URL = "https://api.github.com/graphql"
_sleep = time.sleep  # patch in tests
_now = time.time


class GraphQLError(RuntimeError):
    """GraphQL returned errors and no data. The raw list is in `.errors`."""

    def __init__(self, errors):
        self.errors = errors
        super().__init__(str(errors)[:300])


def token() -> str:
    """$GITHUB_TOKEN, then $GH_TOKEN, then `gh auth token`."""
    for var in ("GITHUB_TOKEN", "GH_TOKEN"):
        if os.environ.get(var, "").strip():
            return os.environ[var].strip()
    try:
        out = subprocess.run(["gh", "auth", "token"], capture_output=True,
                             text=True).stdout.strip()
    except OSError:
        out = ""
    if out:
        return out
    raise RuntimeError("No GitHub token: set GITHUB_TOKEN or GH_TOKEN, "
                       "or run `gh auth login`.")


def _rate_wait(headers) -> float:
    """Seconds to wait: retry-after, else until x-ratelimit-reset, else 60."""
    if headers.get("retry-after"):
        return float(headers["retry-after"])
    if headers.get("x-ratelimit-reset"):
        return max(float(headers["x-ratelimit-reset"]) - _now(), 0) + 1
    return 60.0


def _is_rate_limit(status, headers) -> bool:
    return status in (403, 429) and (
        headers.get("x-ratelimit-remaining") == "0" or headers.get("retry-after")
        or status == 429)


def query(q, variables=None, *, tries=5, timeout=60, max_backoff=20,
          token=None) -> dict:
    """Run a query and return `data`.

    Retries 5xx and network errors with exponential backoff (1, 2, 4... capped at
    max_backoff). Rate limits (HTTP 403/429 or a RATE_LIMITED GraphQL error) sleep
    until the reset time, else 60s. Other 4xx raise immediately.

    Partial results: if GraphQL returns errors AND data, the data is returned and
    a warning is issued (the sources treated partial nodes as usable). If it
    returns errors and no data, GraphQLError is raised.
    """
    tok = token or globals()["token"]()
    body = json.dumps({"query": q, "variables": variables or {}}).encode()
    req = urllib.request.Request(API_URL, data=body, headers={
        "Authorization": f"bearer {tok}", "Content-Type": "application/json"})
    for attempt in range(tries):
        last = attempt == tries - 1
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                headers = {k.lower(): v for k, v in r.headers.items()}
                d = json.loads(r.read())
        except urllib.error.HTTPError as e:
            headers = {k.lower(): v for k, v in e.headers.items()}
            if _is_rate_limit(e.code, headers):
                if last:
                    raise
                _sleep(_rate_wait(headers))
            elif e.code >= 500:
                if last:
                    raise
                _sleep(min(2 ** attempt, max_backoff))
            else:
                raise
            continue
        except (urllib.error.URLError, OSError, ValueError):
            if last:
                raise
            _sleep(min(2 ** attempt, max_backoff))
            continue
        errors = d.get("errors")
        if errors:
            if any(isinstance(e, dict) and e.get("type") == "RATE_LIMITED"
                   for e in errors) and not last:
                _sleep(_rate_wait(headers))
                continue
            if not d.get("data"):
                raise GraphQLError(errors)
            warnings.warn(f"GraphQL returned partial data with errors: "
                          f"{str(errors)[:200]}", stacklevel=2)
        return d["data"]
    raise RuntimeError("unreachable")


def _dig(data, path):
    for key in path.split("."):
        data = data.get(key) if isinstance(data, dict) else None
    if data is None:
        raise GraphQLError([{"message": f"nothing at path {path!r}"}])
    return data


def paginate(q, variables, path, *, checkpoint=None, cursor_var="cursor",
             **query_kwargs) -> Iterator[dict]:
    """Yield every node of the connection at dotted `path`, following pageInfo.

    `q` must declare a `$cursor: String` variable (name via cursor_var), pass it
    as `after:`, and select `pageInfo { hasNextPage endCursor }` and `nodes`.
    With `checkpoint` (a file path) the cursor is saved after each page is fully
    consumed and resumed on the next run; the file is removed on completion.
    A crash mid-page re-fetches that page (at-least-once).
    """
    variables = dict(variables or {})
    cursor = None
    if checkpoint and os.path.exists(checkpoint):
        with open(checkpoint) as f:
            cursor = f.read().strip() or None
    while True:
        variables[cursor_var] = cursor
        conn = _dig(query(q, variables, **query_kwargs), path)
        for node in conn.get("nodes") or []:
            if node is not None:  # search connections can contain nulls
                yield node
        info = conn["pageInfo"]
        if not info["hasNextPage"]:
            break
        cursor = info["endCursor"]
        if checkpoint:
            with open(checkpoint, "w") as f:
                f.write(cursor)
    if checkpoint and os.path.exists(checkpoint):
        os.remove(checkpoint)
