# URL-subscribed user rules

Subscribe to a self-contained MDC rule at a direct HTTPS URL to use it across this
installation's workspaces. The rule stays in a local cache with a `user:` ID;
it does not become a built-in rule or enter the personal flow library's Git sync.
This is useful for private team guidance without adding its text to the public
Agents-Core repository.

Subscription and refresh are explicit operations. Persona activation, compact
worker preparation, catalog listing and flow selection read only accepted local
snapshots. They never fetch the URL or poll for updates.

## Source document

Serve a UTF-8 raw text file, not a website page or a sign-in form:

```yaml
---
name: team-conventions
description: Conventions for work on the team's repositories.
category: engineering
priority: 50
---
# Team conventions
Apply these conventions when working on a repository owned by this team.
Keep changes focused and record the relevant validation with each change.
```

The resulting ID is `user:team-conventions`. `name` is required, starts with a
letter or digit, and contains at most 80 letters, digits, underscores or hyphens.
The other fields are optional: `description` defaults to an empty string,
`category` to `user`, and `priority` to `50`. Priority must be an integer from
0 to 1000; lower values load first. Only these four metadata fields are allowed.
The body must be nonempty. Put any repository or task applicability in the body;
there are no per-agent routing fields. MDC imports and YAML anchors or aliases
are rejected. The entire document is limited to 64 KiB.

Subscribed text remains user-supplied guidance within the host's instruction
hierarchy. A source cannot grant itself system authority. Review the source and
its intended scope before subscribing or explicitly accepting a refresh.

## MCP operations

| Tool | Behavior |
|---|---|
| `list_user_rules()` | List subscriptions, including disabled rules, their current revisions and refresh errors; no fetch. |
| `get_user_rule(rule_id)` | Read the accepted MDC and retained content hashes; no fetch. |
| `subscribe_user_rule(url, auth_env=None)` | Fetch, validate, cache and enable one new subscription. The source's `name` determines its `user:` ID. |
| `refresh_user_rule(rule_id, expected_revision)` | Fetch the same source and accept valid content with the same name. Failure retains the previous content and enabled state and records an error. |
| `set_user_rule_enabled(rule_id, enabled, expected_revision)` | Enable or disable the cached rule; no fetch. |
| `rollback_user_rule(rule_id, content_sha256, expected_revision)` | Restore a retained content hash while preserving the URL and enabled state; no fetch. |
| `delete_user_rule(rule_id, expected_revision)` | Delete the local subscription and its history. The source document is unchanged. |

For a mutation after subscription, first read `get_user_rule` and pass its
`revision` as `expected_revision`. A stale revision returns `rule_conflict`;
read again before retrying. Refresh attempts also update the revision, including
failed attempts, so read the retained result or fetch the current metadata before
another mutation. The library retains up to 20 distinct accepted content
snapshots per subscription and supports at most 32 subscriptions. A rollback can
only select a retained hash. Deletion removes that local history; subscribing
again starts a new history.

After changing cached content or enabled state, use `refresh_persona_context`
with the current descriptor to issue an updated persona bundle. Its revision
covers the delivered rule names and text. A conversation that keeps its current
activation retains the rules it already received. A newly prepared compact worker
receives the enabled cached rules when its package is built.

## Selection and the web UI

By default, protocol 2 persona bundles and compact worker packages include enabled
user rules alongside built-in rules. The persona descriptor and footer use the
full `user:` IDs. The legacy per-query evaluation path excludes subscriptions
and continues using built-in rules independently of local switches.

A [flow persona](../flows/README.md#choose-a-flows-agent-and-components) can choose
an exact rule list:

```yaml
persona:
  agent: software_engineer
  rules: [no-fabrication, "user:team-conventions"]
```

An omitted `rules` field uses the default enabled set. `rules: []` remains empty;
no user rule is added to it. A named user rule must exist and be enabled, otherwise
selection fails. A named built-in rule keeps the existing behavior of overriding
its web UI switch. `user:` IDs are accepted only for rules, never for skills or
implants. `RULES_ENABLED=0` disables both built-in and user rules in bundles;
exact selections still validate their names. Mandatory compact-worker contract
rules remain present.

The local web UI's Rules tab displays cached user rules, including disabled ones,
and can switch them on or off. Their switches update the rule library, separately
from the built-in rule switches in `components.json`. Subscription, explicit
refresh, rollback and deletion use the MCP operations above. A switch checks the
revision of the displayed rule; a concurrent refresh or switch returns a conflict
and reloads the current content for inspection before another attempt.

## Storage, authentication and private hosts

The default store is `data/user-rules` under the installation's data directory.
`AGENTS_USER_RULES_DIR` can override the location. This cache is machine-local
and separate from `AGENTS_USER_FLOWS_DIR` and its Git remote; configuring flow
sync does not distribute subscribed rule bodies. Keep private rule storage out
of repositories that you publish or sync.

A source must be a direct HTTPS URL without a fragment, embedded credentials or
redirects. The only permitted query parameter is a single `ref=<branch>` for a
raw Git file endpoint, such as the GitLab repository-files API; query credentials
and all other parameters are rejected. TLS certificate verification remains
enabled. All DNS answers are validated before connecting. If a connection fails,
the transport tries the remaining validated numeric addresses in resolver order
within the same total deadline, retaining the source hostname for TLS checks.

For a protected endpoint, the owner must set a Bearer token in the server
process's environment and bind that variable to the exact source hostname in
`AGENTS_RULE_AUTH_HOSTS`, a JSON map. For example,
`{"TEAM_RULE_TOKEN":"git.example.com"}` permits `auth_env="TEAM_RULE_TOKEN"` only
for `git.example.com`. Pass only the variable's name through `auth_env`; never
pass the token in chat or put it in the URL. The library stores the environment
variable name, not the token value. Both the token and host binding must be
available to the process serving the MCP request, including a shared daemon.

Public network addresses are permitted by default. To reach an internal host,
set `AGENTS_RULE_PRIVATE_HOSTS` to its exact hostname (multiple names use commas
or whitespace). This exception permits RFC1918 IPv4 addresses for those hosts.
It never permits loopback, link-local or metadata addresses. Redirects remain
blocked, so use the endpoint that returns the raw document directly. Only
subscription and explicit refresh contact it; failed fetches do not replace an
accepted snapshot.
