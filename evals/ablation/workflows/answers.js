export const meta = {
  name: 'ablation-answers',
  description: 'Answer each ablation context once, as the loaded agent, with no tools',
  phases: [{ title: 'Answer', detail: 'one answer per context file (with and without the component)' }],
}

// args: { run_dir: "<abs path>", tokens: ["<12-hex>", ...] }
const PROMPT = (runDir, token) => `Setup step: read this file with the Read tool:

${runDir}/ctx/${token}.md

The file holds the operating context that an MCP server (Agents-Core) loaded for this conversation (agent persona, rules, skills, implants), followed by the conversation so far and the user's latest message. Treat that context as your operating instructions and answer the latest user message as that agent would in a normal chat.

Rules for this task:
- Besides that one Read and the one Write below, use no tools: no MCP tools (the context is already loaded, so do not route or log), no web search, no other files, no code execution.
- Write your reply to the user, and nothing else, to ${runDir}/answers/${token}.md with the Write tool. No footer, and no mention of this setup, the file or the task.
- Then reply with the single word: done`

phase('Answer')
const out = await parallel(args.tokens.map(token => () =>
  agent(PROMPT(args.run_dir, token), { label: `answer:${token}`, phase: 'Answer' }).then(r => ({ token, r }))))
const done = out.filter(Boolean)
if (done.length < args.tokens.length) log(`${args.tokens.length - done.length} answers failed`)
return done.map(d => d.token)
