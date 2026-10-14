"""Review threads of a pull request: what is unanswered, and resolving the answered ones.

    python scripts/dev/pr_threads.py 95                  # unresolved threads + reviews on the head
    python scripts/dev/pr_threads.py 95 --resolve-mine   # resolve threads whose last comment is yours
    python scripts/dev/pr_threads.py --closed            # unresolved threads on every merged/closed PR
    python scripts/dev/pr_threads.py 95 --mode autodev   # a self-development PR (flows/pr-review.md#autodev-mode)

"Yours" is the account `gh` is logged in as. A thread is answered once its last
comment is yours; resolve it only after the reply says what was done (the fix commit,
or why the finding was declined). Reviewers also put findings in the review body
("Previously missed", overview notes) rather than a thread: those are printed for
reviews on the current head so they are not overlooked.

In `autodev` mode the owner and self-development share one account (docs/autodev-plan.md
5.1), so the login cannot tell them apart: a thread counts as answered only when its last
comment starts with the agent marker line, the owner's reviews on the head are printed
even when `gh` is logged in as the owner, and `--resolve-mine` is refused. Each thread
and review is also marked TRUSTED or untrusted: only an allowlisted bot, by login and
numeric account ID (`gate.trusted_bot`), is a trusted source of findings (design 5.2).
"""
import argparse
import json
from pathlib import Path
import subprocess
import sys

DEFAULT, AUTODEV = "default", "autodev"
MARKER = "<!-- autodev -->"  # the first line of every self-development comment (design 5.5)
REPO_ROOT = Path(__file__).resolve().parents[2]

# Threads are paged with a cursor; each thread's first comment (the finding) and last
# comment (who spoke last) are fetched directly, so long threads need no paging.
QUERY = """query($owner:String!,$name:String!,$n:Int!,$after:String){repository(owner:$owner,name:$name){
  pullRequest(number:$n){headRefOid reviewThreads(first:100,after:$after){
    pageInfo{hasNextPage endCursor}
    nodes{id isResolved path line
      first:comments(first:1){nodes{databaseId author{login} body}}
      last:comments(last:1){nodes{databaseId author{login}}}}}}}}"""
# Autodev mode also reads the last comment's body (the marker) and each author's node type
# and bot account ID (the allowlist), as the broker's thread query does.
AUTODEV_QUERY = """query($owner:String!,$name:String!,$n:Int!,$after:String){repository(owner:$owner,name:$name){
  pullRequest(number:$n){headRefOid reviewThreads(first:100,after:$after){
    pageInfo{hasNextPage endCursor}
    nodes{id isResolved path line
      first:comments(first:1){nodes{databaseId author{__typename login ... on Bot{databaseId}} body}}
      last:comments(last:1){nodes{databaseId author{__typename login ... on Bot{databaseId}} body}}}}}}}"""


def gh(*args: str) -> str:
    return subprocess.check_output(["gh", *args], text=True)


def threads(owner: str, name: str, number: int, query: str = QUERY) -> tuple[str, list[dict]]:
    nodes, after = [], None
    while True:
        args = ["api", "graphql", "-f", f"query={query}", "-F", f"owner={owner}", "-F", f"name={name}", "-F", f"n={number}"]
        if after:
            args += ["-f", f"after={after}"]
        data = json.loads(gh(*args))["data"]["repository"]["pullRequest"]
        page = data["reviewThreads"]
        nodes += page["nodes"]
        if not page["pageInfo"]["hasNextPage"]:
            return data["headRefOid"], nodes
        after = page["pageInfo"]["endCursor"]


def login(node: dict | None) -> str:
    """GitHub returns a null author for deleted accounts."""
    return ((node or {}).get("author") or {}).get("login") or "ghost"


def marked(body: str | None) -> bool:
    """Whether a comment starts with the agent marker line, as the broker writes it."""
    body = body or ""
    return body == MARKER or body.startswith((MARKER + "\n", MARKER + "\r\n"))


def trusted(user: dict | None) -> bool:
    """An allowlisted bot by login and integer account ID (REST shape): `gate.trusted_bot`,
    imported only in autodev mode, so the default mode needs nothing but `gh`."""
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    from src.schedules.gate import trusted_bot

    return trusted_bot(user)


def graphql_user(comment: dict | None) -> dict | None:
    """A GraphQL comment author in the REST shape `trusted` reads, as the broker builds it: a
    `Bot` node's login without `[bot]` gets it appended; a `User` or a deleted account is no bot."""
    author = (comment or {}).get("author") or {}
    if author.get("__typename") != "Bot" or not isinstance(author.get("login"), str):
        return None
    return {"login": author["login"] + "[bot]", "id": author.get("databaseId")}


def trust(user: dict | None) -> str:
    return "TRUSTED" if trusted(user) else "untrusted"


def answered(last: dict, me: str, mode: str) -> bool:
    """The last comment is yours; in autodev mode also marked, since the owner shares the account."""
    return login(last) == me and (mode != AUTODEV or marked(last.get("body")))


def review_bodies(repo: str, number: int, head: str, me: str, mode: str = DEFAULT) -> list[dict]:
    pages = json.loads(gh("api", "--paginate", "--slurp", f"repos/{repo}/pulls/{number}/reviews"))
    return [r for page in pages for r in page
            if r["commit_id"] == head and (mode == AUTODEV or (r.get("user") or {}).get("login") != me)
            and r["body"].strip()]


def show(repo: str, number: int, me: str, resolve_mine: bool, mode: str = DEFAULT) -> int:
    if resolve_mine and mode == AUTODEV:
        raise ValueError("--resolve-mine is not used in autodev mode")
    owner, name = repo.split("/")
    head, nodes = threads(owner, name, number, AUTODEV_QUERY if mode == AUTODEV else QUERY)
    open_threads = [t for t in nodes if not t["isResolved"]]
    print(f"#{number} head {head[:7]}: {len(open_threads)} unresolved of {len(nodes)} threads")
    for t in open_threads:
        first, last = t["first"]["nodes"][0], t["last"]["nodes"][0]
        if resolve_mine and login(last) == me:
            gh("api", "graphql", "-f", f'query=mutation{{resolveReviewThread(input:{{threadId:"{t["id"]}"}}){{thread{{isResolved}}}}}}')
            print(f"  resolved {t['path']}:{t['line']}")
            continue
        state = "answered" if answered(last, me, mode) else "UNANSWERED"
        source = f" {trust(graphql_user(first))}" if mode == AUTODEV else ""
        print(f"  {state} {t['path']}:{t['line']} {login(first)} c{first['databaseId']}{source}")
        print("    " + first["body"].replace("\n", " ")[:500])
        if mode == AUTODEV and last["databaseId"] != first["databaseId"]:
            # A trusted bot's reply in a thread the agent or the owner started counts (pr-review, autodev item 3).
            print(f"    last: {login(last)} c{last['databaseId']} {trust(graphql_user(last))}: "
                  + (last.get("body") or "").replace("\n", " ")[:500])
    if not resolve_mine:
        for r in review_bodies(repo, number, head, me, mode):
            source = f" {trust(r.get('user'))}" if mode == AUTODEV else ""
            print(f"  REVIEW {(r.get('user') or {}).get('login', 'ghost')} {r['submitted_at']}{source}: "
                  + r["body"].replace("\n", " ")[:800])
    return len(open_threads)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("pr", nargs="?", type=int)
    parser.add_argument("--resolve-mine", action="store_true")
    parser.add_argument("--closed", action="store_true", help="scan every merged or closed PR")
    parser.add_argument("--mode", choices=(DEFAULT, AUTODEV), default=DEFAULT,
                        help="autodev: a self-development PR under the owner's account; no --resolve-mine")
    args = parser.parse_args()
    if args.mode == AUTODEV and args.resolve_mine:
        parser.error("--resolve-mine is not used in autodev mode: resolve only the threads of allowlisted bots "
                     "and the agent's own, each after its answer (flows/pr-review.md#autodev-mode)")
    repo = gh("repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner").strip()
    me = gh("api", "user", "--jq", ".login").strip()
    if args.closed:
        # The REST list pages through every closed PR; `gh pr list --limit` would cap it.
        pages = json.loads(gh("api", "--paginate", "--slurp", f"repos/{repo}/pulls?state=closed&per_page=100"))
        numbers = [pr["number"] for page in pages for pr in page]
        owner, name = repo.split("/")
        for n in sorted(numbers):
            if any(not t["isResolved"] for t in threads(owner, name, n)[1]):
                show(repo, n, me, args.resolve_mine, args.mode)
        return
    if args.pr is None:
        parser.error("give a PR number or --closed")
    show(repo, args.pr, me, args.resolve_mine, args.mode)


if __name__ == "__main__":
    main()
