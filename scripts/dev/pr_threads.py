"""Review threads of a pull request: what is unanswered, and resolving the answered ones.

    python scripts/dev/pr_threads.py 95                  # unresolved threads + reviews on the head
    python scripts/dev/pr_threads.py 95 --resolve-mine   # resolve threads whose last comment is yours
    python scripts/dev/pr_threads.py --closed            # unresolved threads on every merged/closed PR

"Yours" is the account `gh` is logged in as. A thread is answered once its last
comment is yours; resolve it only after the reply says what was done (the fix commit,
or why the finding was declined). Reviewers also put findings in the review body
("Previously missed", overview notes) rather than a thread: those are printed for
reviews on the current head so they are not overlooked.
"""
import argparse
import json
import subprocess

QUERY = """query($owner:String!,$name:String!,$n:Int!){repository(owner:$owner,name:$name){pullRequest(number:$n){
  headRefOid reviewThreads(first:100){nodes{id isResolved path line
    comments(first:50){nodes{databaseId author{login} body}}}}}}}"""


def gh(*args: str) -> str:
    return subprocess.check_output(["gh", *args], text=True)


def threads(owner: str, name: str, number: int) -> tuple[str, list[dict]]:
    data = json.loads(gh("api", "graphql", "-f", f"query={QUERY}", "-F", f"owner={owner}",
                         "-F", f"name={name}", "-F", f"n={number}"))["data"]["repository"]["pullRequest"]
    return data["headRefOid"], data["reviewThreads"]["nodes"]


def review_bodies(repo: str, number: int, head: str, me: str) -> list[dict]:
    pages = json.loads(gh("api", "--paginate", "--slurp", f"repos/{repo}/pulls/{number}/reviews"))
    return [r for page in pages for r in page
            if r["commit_id"] == head and r["user"]["login"] != me and r["body"].strip()]


def show(repo: str, number: int, me: str, resolve_mine: bool) -> int:
    owner, name = repo.split("/")
    head, nodes = threads(owner, name, number)
    open_threads = [t for t in nodes if not t["isResolved"]]
    print(f"#{number} head {head[:7]}: {len(open_threads)} unresolved of {len(nodes)} threads")
    for t in open_threads:
        comments = t["comments"]["nodes"]
        last, first = comments[-1], comments[0]
        if resolve_mine and last["author"]["login"] == me:
            gh("api", "graphql", "-f", f'query=mutation{{resolveReviewThread(input:{{threadId:"{t["id"]}"}}){{thread{{isResolved}}}}}}')
            print(f"  resolved {t['path']}:{t['line']}")
            continue
        state = "answered" if last["author"]["login"] == me else "UNANSWERED"
        print(f"  {state} {t['path']}:{t['line']} {first['author']['login']} c{first['databaseId']}")
        print("    " + first["body"].replace("\n", " ")[:500])
    if not resolve_mine:
        for r in review_bodies(repo, number, head, me):
            print(f"  REVIEW {r['user']['login']} {r['submitted_at']}: " + r["body"].replace("\n", " ")[:800])
    return len(open_threads)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("pr", nargs="?", type=int)
    parser.add_argument("--resolve-mine", action="store_true")
    parser.add_argument("--closed", action="store_true", help="scan every merged or closed PR")
    args = parser.parse_args()
    repo = gh("repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner").strip()
    me = gh("api", "user", "--jq", ".login").strip()
    if args.closed:
        numbers = json.loads(gh("pr", "list", "--repo", repo, "--state", "closed", "--limit", "500",
                                "--json", "number", "--jq", "[.[].number]"))
        owner, name = repo.split("/")
        for n in sorted(numbers):
            if any(not t["isResolved"] for t in threads(owner, name, n)[1]):
                show(repo, n, me, args.resolve_mine)
        return
    if args.pr is None:
        parser.error("give a PR number or --closed")
    show(repo, args.pr, me, args.resolve_mine)


if __name__ == "__main__":
    main()
