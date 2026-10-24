<!-- variant: full -->
## Run a named flow

When the user names a flow, workflow or ritual, or asks for work "by the flow"
("flow", "workflow", "флоу", "по флоу X", "ритуал", or a flow ID such as
`pr-review`), call `list_flows` and then `run_flow` for the matching flow before
taking any of its steps. Pass the user's scope and constraints in `request`, plus
`current_persona` and `workspace`; apply a returned `persona_activation` as a
`switch`, then execute the returned instructions. Do not improvise a procedure
that an installed flow covers. If no flow matches, say so and continue without
one.

<!-- variant: server -->
When the user names a flow, workflow or ritual, or asks for work by one, in any language, call list_flows and then run_flow for a matching flow before taking its steps. If no flow matches, say so and continue without one. Do not improvise a procedure an installed flow covers. Flows are built-in, personal (user:<id>, every repository) or per repository (repo:<id>); run_flow binds them to the caller's workspace and returns needs_execution. When the user asks to save, change, restore or delete a flow, use get_flow, save_flow and delete_flow; say which scope you used. To choose a flow's agent, skills, implants or rules, use set_flow_persona. Pass current_persona to run_flow and apply its persona_activation as a switch before executing. Execute the returned instructions in the current model session against repo_path, preserving user constraints. It does not perform the workflow or authorize additional actions. HTTP requires X-Agents-Workspace; never substitute the installation for a missing target.
