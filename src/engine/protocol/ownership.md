<!-- variant: full -->
## Work that another session owns

A branch, worktree or pull request that another session or agent created belongs
to it, and so does a host or service that another session is working on. Before
pushing to it, merging into it, rebasing it or changing it, message that session
through the client's session messaging (for example Claude Code `SendMessage`) or
Agents-Core `cooperation_send`, and wait for its answer. If it does not answer,
ask the user: silence is not consent. Follow the host's message delivery and
processing status: a reply may arrive between tool calls during an active turn
or start a later turn. Sending a message or receiving a delivery acknowledgement
is not the required answer. While waiting, stop the conflicting action, release
its claims and continue independent work where possible. The Agents-Core mailbox
can wait within the turn with bounded `cooperation_wait`; waiting does not wake
an idle model. Explicit user authorization remains authoritative.

<!-- variant: compact -->
Before changing another session's branch, worktree, pull request, host or service, message the owning session and wait for its answer. If it does not answer, ask the user; silence and delivery acknowledgement are not consent. Explicit user authorization remains authoritative.
