export const meta = {
  name: 'ablation-cases',
  description: 'Write and adversarially check in-scope test cases per prompt component (2 by default)',
  phases: [
    { title: 'Generate', detail: 'one writer per component: n_cases (default 2, ru then en) with rubrics' },
    { title: 'Check', detail: 'one skeptic per component: fix or drop weak cases' },
  ],
}

// args: { run_dir: "<abs path>", ids: ["skill-x", "implant-y", ...], n_cases?: 2 }
const N = args.n_cases || 2
const HALF = Math.ceil(N / 2)
const ROOT_NOTE = `Work from the repository root (the directory that contains evals/ablation/). Do not call MCP tools and do not route through Agents-Core; this is an offline eval task.`

const GENERATE = (id, runDir) => `You are writing test cases for an ablation eval of one prompt component of the Agents-Core framework: ${id}.

${ROOT_NOTE}

Read these files first:
- the component itself (its path is in evals/ablation/components.json under id "${id}"; read the whole file)
- evals/ablation/components.json entry "${id}" (kind, owners)
- evals/ablation/CASES.md (the case format and the quality bar), and follow it exactly

Then write exactly ${N} cases for this component: the first ${HALF} in Russian, the rest in English. Make them differ from each other in situation and in how the component's behaviour shows up, not just in wording. Each case must be a realistic chat request where following this component should visibly change a strong model's answer, and where a strong model without it could plausibly get that part wrong. Pick the answering agent from the component's owners when it has any; otherwise pick the agent in agents/ that would normally receive such a request.

If the component cannot change a one-shot, tool-less chat answer at all (for example it only governs tool calls or file edits), write {"component": "${id}", "cases": [], "untestable": "<one-sentence reason>"} instead.

Write the result as JSON to ${runDir}/cases/${id}.json with the Write tool, then reply with one line: the case ids you wrote, or "untestable: <reason>".`

const CHECK = (id, runDir) => `You are the adversarial reviewer for test cases of the prompt component ${id}. Your job is to find reasons the cases are weak and fix them.

${ROOT_NOTE}

Read: the component file (path in evals/ablation/components.json under "${id}"), evals/ablation/CASES.md, and ${runDir}/cases/${id}.json.

For each case check, and fix in place when it fails:
1. In scope: following the component should change the answer in a way the rubric rewards. If a strong model would do this anyway without the component, make the case harder or subtler; if you cannot, drop it.
2. No leak: the user message and history must not name the component, quote it, or use its terms of art.
3. Answerable in one chat reply with no tools (no web, files or code execution).
4. The agent exists under agents/ and fits the request; prefer the component's owners.
5. Rubric: 3-4 items, each checkable from the answer text alone; every factual claim in it is correct; at least one item targets the component's behaviour and at least one is general quality; nothing rewards length.
6. Count and language: exactly ${N} cases, the first ${HALF} in Russian and the rest in English; realistic user voice; no two cases test the same situation.
If you had doubts about a factual rubric item you could not settle, remove that item rather than keep a guess.

If the file says "untestable", confirm or overturn that judgement: if a tool-less chat case is possible after all, write the ${N} cases yourself.

Write the fixed JSON back to the same path, adding "checked": true and "checker_notes": "<what you changed and why, 1-3 sentences>". Reply with one line: the final case ids, or "untestable: <reason>".`

const results = await pipeline(
  args.ids,
  (id) => agent(GENERATE(id, args.run_dir), { label: `write:${id}`, phase: 'Generate' }),
  (_written, id) => agent(CHECK(id, args.run_dir), { label: `check:${id}`, phase: 'Check' })
    .then(summary => ({ id, summary })),
)
const done = results.filter(Boolean)
if (done.length < args.ids.length) log(`${args.ids.length - done.length} components failed in case writing`)
return done
