# gh-graphql

A tiny GitHub GraphQL client. Standard library only, Python 3.10+.

It handles the boring parts: finding a token, retrying flaky requests, waiting out rate limits, and paging through long lists (with an optional resume file if your script dies halfway).

## Install

    pip install git+https://github.com/iamsorenl/gh-graphql

## Example

List the issues of a repo:

```python
from gh_graphql import query, paginate

print(query("{ viewer { login } }"))

Q = """
query($cursor: String) {
  repository(owner: "cli", name: "cli") {
    issues(first: 50, after: $cursor) {
      pageInfo { hasNextPage endCursor }
      nodes { number title }
    }
  }
}"""

for issue in paginate(Q, {}, "repository.issues", checkpoint="issues.cursor"):
    print(issue["number"], issue["title"])
```

## API

- `token()` returns `$GITHUB_TOKEN`, else `$GH_TOKEN`, else the output of `gh auth token`. Raises a clear error if none.
- `query(q, variables=None, *, tries=5, timeout=60, max_backoff=20, token=None)` returns the `data` dict.
  - Network errors and 5xx: exponential backoff (1s, 2s, 4s..., capped at `max_backoff`).
  - Rate limit (HTTP 403/429 or a `RATE_LIMITED` GraphQL error): sleeps until the reset time GitHub sends, or 60s if it sends none.
  - Other 4xx errors are raised right away.
  - GraphQL errors with no data raise `GraphQLError` (the list is in `.errors`).
  - GraphQL errors *with* partial data: the data is returned and a `warnings.warn` is issued. Nodes that did come back are usually still useful. Turn warnings into errors with `-W error` if you want it strict.
- `paginate(q, variables, path, *, checkpoint=None, cursor_var="cursor", **query_kwargs)` yields each node of the connection at the dotted `path`. Your query needs a `$cursor: String` variable used as `after:` and a `pageInfo { hasNextPage endCursor }` selection. With `checkpoint`, the cursor is saved after each page you finish consuming, and the file is deleted when the last page is done. If you crash mid-page, that page is fetched again on resume, so handle duplicates.

## Tests

    python -m unittest discover -s tests
