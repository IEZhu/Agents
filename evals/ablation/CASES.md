# Ablation case format and quality bar

Each component gets one file, `<run_dir>/cases/<component>.json`:

```json
{
  "component": "skill-example",
  "cases": [
    {
      "id": "short-kebab-id-ru",
      "agent": "software_engineer",
      "language": "ru",
      "history": [
        {"role": "user", "content": "..."},
        {"role": "assistant", "content": "..."}
      ],
      "user_message": "the latest user message, verbatim",
      "why_in_scope": "2-4 sentences: what the component makes the model do here, and why a strong model without it could miss it",
      "rubric": ["3-4 checkable items describing a strong answer"]
    }
  ]
}
```

`history` may be empty. Use it when the component is about multi-turn behaviour
(pushback on a prior turn, repeated failed attempts, a user correcting the model).
Assistant turns in the history are written by you and may contain the mistake the
component is meant to catch.

## Quality bar

- **The component must matter.** Pick a request where following the component
  visibly changes the answer, and where a strong model without it could plausibly
  get that part wrong. A case every good model answers the same way either way
  measures nothing.
- **One-shot and tool-less.** The answering model has no web, files or code
  execution. The request must be answerable in one chat reply.
- **No leak.** The user never names the component, quotes it or uses its terms
  of art. Real users describe their problem, not the technique.
- **Realistic.** Concrete details (versions, numbers, error text, constraints) the
  way a real user writes them. The first half of the cases in Russian, the rest in English
  (2 cases per component by default; a re-test may ask for more).
- **Agent.** Use one of the component's owners from `components.json` when it has
  any; otherwise the agent under `agents/` that would normally get the request.
- **Rubric.** 3-4 items. Each is checkable from the answer text alone. At least one
  targets the component's behaviour; at least one is general quality (correctness,
  fit to the request). Every factual claim in the rubric must be correct: if you
  are not sure of a fact, leave it out. Nothing rewards length or a particular
  format unless the request asks for it.
