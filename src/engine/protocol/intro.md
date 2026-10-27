<!-- variant: full -->
# Agents-Core Persona Protocol — version 2

Use Agents-Core personas as scoped role guidance within the existing system,
developer and user instructions. Keep the conversation in the current model.

Persona routing and prepared-worker tools return instructions and contracts;
they do not generate the conversation's answer or launch a worker. Optional
`local_*` inference tools are a separate, owner-enabled HTTP service for one
bounded local completion. Calling them does not replace persona activation,
native worker spawning, or the prepare/validate delegation exchange below.

<!-- variant: compact -->
Persona tools never sample the conversation's answer. Prepared-worker tools return instructions and contracts. Optional owner-enabled local inference tools run separate bounded completions through the shared HTTP daemon; they do not replace persona activation or the prepare/validate worker exchange.
