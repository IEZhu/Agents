"""HTTP proxy management through the running daemon; secrets only on stdin."""
import sys


def run(controller, args):
    def post(body):
        response = controller.request("/admin/mcp", method="POST", body=body, timeout=90)
        if "error" in response:
            raise RuntimeError(response["error"])
        return response["result"]

    if args.mcp_command == "list":
        return controller.request("/admin/mcp")
    if args.mcp_command == "connect":
        return post({"action": "connect", "alias": args.alias})
    if args.mcp_command == "disable":
        records = controller.request("/admin/mcp")["servers"]
        record = next((item for item in records if item["alias"] == args.alias), None)
        if record is None:
            raise ValueError("not_found")
        if not record.get("revision"):
            raise ValueError("invalid_definition")
        return post({"action": "enable", "alias": args.alias, "apps": [], "revision": record["revision"]})
    token = sys.stdin.readline(8193).strip() if args.bearer else None
    if args.bearer and (not token or len(token) > 8192):
        raise ValueError("A bearer token is required on stdin")
    definition = {"format": 1, "alias": args.alias, "title": args.alias, "description": "",
                  "source": {"kind": "manual"}, "transport": {"type": "http", "url": args.url, "headers": {}},
                  "auth": {"type": "bearer" if token else "none"},
                  "secrets": [{"name": "Authorization", "target": "header", "required": True}] if token else [],
                  "tools": {"expose": "all", "deny": [], "always_load": []},
                  "limits": {"call_timeout_seconds": 60, "idle_seconds": 600}, "instructions": ""}
    saved = post({"action": "save", "definition": definition})
    if token:
        post({"action": "secret", "alias": args.alias, "name": "Authorization", "value": "Bearer " + token})
    post({"action": "enable", "alias": args.alias, "apps": args.apps.split(","), "revision": saved["revision"]})
    return post({"action": "connect", "alias": args.alias})
