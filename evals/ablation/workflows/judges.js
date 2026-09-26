export const meta = {
  name: 'ablation-judges',
  description: 'Blind pairwise judging of ablation answer pairs (each pair is judged in both orders)',
  phases: [{ title: 'Judge', detail: 'one verdict per judge file' }],
}

// args: { run_dir: "<abs path>", files: ["<judge file stem>", ...] }
const PROMPT = (runDir, stem) => `You are a strict, impartial evaluator. Read the file ${runDir}/judge/${stem}.md with the Read tool. Do not open any other file and do not call MCP tools.

Your own knowledge may end before recent laws, releases or prices. If a specific fact would decide the verdict (a law, a number, a version, an API) and you are not sure whether it is right, check it with WebSearch before counting it as an error. Never mark a fact wrong only because you do not recognise it.

The file has a conversation, the latest user message, a rubric describing a strong answer, and two candidate answers, A and B, written by an assistant with no tool access. Decide which answer serves the user better, using the rubric as the main standard and general answer quality (correctness, fit to the request, clarity, honest handling of uncertainty) as the secondary one. Answer order and length are not merits in themselves. Judge the answers only as written.

Write only this JSON object to ${runDir}/judge/${stem}.verdict.json with the Write tool:
{"winner": "A" | "B" | "tie", "margin": "small" | "clear" | "large", "rubric": [{"item": <number>, "A": "met" | "partial" | "missed", "B": "met" | "partial" | "missed"}], "reasons": "<3 sentences max: the differences that decided it>", "factual_errors": {"A": ["..."], "B": ["..."]}}

Then reply with the single word: done`

phase('Judge')
const out = await parallel(args.files.map(stem => () =>
  agent(PROMPT(args.run_dir, stem), { label: `judge:${stem}`, phase: 'Judge' }).then(r => ({ stem, r }))))
const done = out.filter(Boolean)
if (done.length < args.files.length) log(`${args.files.length - done.length} verdicts failed`)
return done.map(d => d.stem)
