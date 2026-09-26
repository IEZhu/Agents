Your current prompt leaves most of the format up to the model. It says "sequence diagram" but doesn't give the exact first lines, it doesn't say how to write aliases, and it doesn't say what to do with fences or explanations. The fix is to spell out the output contract and show one complete example. After that, add a small sanitizer as a safety net, because a prompt reduces format drift but doesn't guarantee against it.

## 1. System prompt

```text
You convert plain-text descriptions of request flows between services into Mermaid sequence diagrams. Your output is passed directly to a Mermaid 10 renderer with no human review, so it must be valid Mermaid and nothing else.

OUTPUT CONTRACT (all rules are mandatory):
1. The first line is exactly: sequenceDiagram
2. The second line is exactly: autonumber (indented 4 spaces)
3. Next, declare every participant, one per line, in the order it first appears in the description, using exactly this form:
       participant <ALIAS> as <Display Name>
4. After the declarations, write the messages in the order they happen.
5. Output raw Mermaid only. No ``` fences, no "mermaid" label, no comments, no blank lines, no text before or after the diagram.
6. Never use graph, flowchart or any diagram type other than sequenceDiagram.

ALIAS RULES:
- UPPERCASE letters and digits only, 2 to 5 characters, no spaces, underscores or hyphens.
- Use the well-known abbreviation if one exists (API Gateway -> GW, Postgres -> PG, Redis -> RDS); otherwise use the initials or the first letters of the main word (Auth Service -> AUTH, Orders Service -> ORD, Mobile App -> APP).
- Never use END, OPT, ALT, LOOP, PAR, NOTE or other Mermaid keywords as aliases.
- Display Name is the name from the description in Title Case.

MESSAGE RULES:
- Requests use ->>  and responses use -->>
- Labels are short (2 to 5 words) and verb-first: "Validate JWT", "Read order", "200 OK".
- Labels must not contain ; # : or line breaks.
- Draw a response arrow only where the description states or clearly implies a return (for example, "validates X with Y" implies Y replies to the caller).
- Don't invent steps, endpoints, HTTP methods or status codes that the description doesn't mention.

EXAMPLE
Description:
Mobile app calls API Gateway, gateway validates the JWT with Auth Service, then forwards to Orders Service which reads from Postgres and returns the order; gateway returns 200 to the app.

Output:
sequenceDiagram
    autonumber
    participant APP as Mobile App
    participant GW as API Gateway
    participant AUTH as Auth Service
    participant ORD as Orders Service
    participant PG as Postgres
    APP->>GW: Request order
    GW->>AUTH: Validate JWT
    AUTH-->>GW: JWT valid
    GW->>ORD: Forward request
    ORD->>PG: Read order
    PG-->>ORD: Order row
    ORD-->>GW: Return order
    GW-->>APP: 200 OK
```

## 2. User message

Put only the description in the user message, wrapped in tags so it's clearly marked as input:

```text
<description>
{engineer's text}
</description>
```

## 3. Call settings

- **Temperature 0.** Much of the alias drift you're seeing (`API` / `api_gw` / `ApiGateway`) is probably sampling variance. The alias rules plus temperature 0 should remove most of it.
- **Prefill the answer if your API allows it.** Start the assistant turn with `sequenceDiagram\n    autonumber\n` and let the model continue from there. The output can't then start with a fence or with `graph TD`. You'll need to prepend those two lines yourself before rendering. Some providers and newer models don't support prefill, so check this for the model you use.
- **Optional:** if you have a fixed set of services, add a small glossary to the system prompt (`Orders Service -> ORD`, `Billing Service -> BILL`, ...). Then aliases stay the same across diagrams and not just across runs.

## 4. Sanitizer before rendering

Still validate before rendering. A small check prevents the parse errors from reaching the page:

```js
function extractMermaid(raw) {
  let text = raw.replace(/```(?:mermaid)?/gi, "");     // strip any fences
  const start = text.indexOf("sequenceDiagram");
  if (start === -1) throw new Error("not a sequence diagram");
  text = text.slice(start);
  // keep only lines that look like Mermaid; drops trailing prose
  const ok = /^(sequenceDiagram|\s+(autonumber|participant\s|actor\s|note\s|[A-Z0-9]+\s*-?->>|activate|deactivate|alt|else|opt|loop|par|and|end\b|rect))/i;
  return text.split("\n").filter(l => ok.test(l)).join("\n").trim();
}

const code = extractMermaid(llmOutput);
await mermaid.parse(code);   // throws on invalid syntax in 10.x
```

If `mermaid.parse` fails, retry the LLM call once with the parse error appended ("Your previous output failed to parse: <error>. Return corrected Mermaid only."). If the retry also fails, show a fallback message and don't render the broken diagram.

Two things I haven't checked against mermaid 10.9, so verify them in the docs for your version:
- I'm recalling from memory that `mermaid.parse` is async and throws on invalid input in 10.x. I also recall a `{ suppressErrors: true }` option that returns `false` instead of throwing, but I'm not sure which version added it.
- The line filter is deliberately simple. If you later allow notes or `alt` blocks with complex labels, loosen the regex.
