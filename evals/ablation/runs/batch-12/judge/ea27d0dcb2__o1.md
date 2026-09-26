# Case shcot-outage-email-two-versions-en

## Conversation so far
(none)

## Latest user message
We had an outage yesterday and two people on my team wrote the customer email. I need to send one today. Facts: our invoicing API was down from 14:05 to 16:45 UTC, caused by an expired TLS certificate on our internal load balancer; about 1,900 customers couldn't generate invoices; no data was lost; we're giving every affected account a 10% credit on next month's bill; we've added automated cert-expiry alerts 30 days out.

Version 1 (Priya):
"Subject: Incident report INC-2291
Between 14:05 and 16:45 UTC on Sept 24, the /v2/invoices endpoint returned HTTP 503 due to TLS handshake failures between the edge LB and the upstream pool, caused by certificate expiry. Remediation: cert rotated at 16:41 UTC; expiry monitoring with a 30-day threshold has been deployed. No data loss occurred. Affected tenants: ~1,900."

Version 2 (Marcus):
"Subject: We're sorry about yesterday
Hi there, yesterday for about an hour some of you couldn't create invoices, and we know how frustrating that is when you're trying to get paid. This was a technical issue on our side and it's completely fixed. It will never happen again. As a thank-you for your patience, we'll take 10% off your next bill automatically - no need to do anything. Thanks for sticking with us!"

Which one should I send?

## Rubric (what a strong answer does)
1. Does not recommend sending either version unchanged, and identifies at least one concrete weakness of each (e.g. Version 1: internal jargon, no apology, no mention of the credit; Version 2: 'about an hour' contradicts the 14:05-16:45 window, vague cause, 'it will never happen again' is an overpromise).
2. Provides a final email that combines the warm, plain-language tone and automatic 10% credit from Version 2 with the accurate facts from Version 1.
3. The final email states the correct window (14:05 to 16:45 UTC, about 2 hours 40 minutes), that no data was lost, a plain-language cause (an expired security certificate), and the preventive step (alerts 30 days before expiry), and does not claim the problem can never recur.
4. The final email is concise and readable for non-technical customers, without internal terms like 'upstream pool', 'edge LB' or 'tenants'.

## Answer A
Don't send either one as written. Use Marcus's tone and Priya's accuracy. Each draft has a problem that could hurt you, and Marcus's has two factual problems.

## What's wrong with each

**Marcus's version: the right tone, but it's inaccurate and promises too much**
- **"About an hour" is wrong.** The outage ran 2 hours 40 minutes (14:05–16:45 UTC). Customers who were affected will know it lasted longer, and understating it looks evasive.
- **"It will never happen again" is a promise you can't keep.** Cert-expiry alerts prevent *this* failure. They don't prevent every outage. If anything else breaks next quarter, customers will throw this line back at you. Commit to the specific fix instead.
- **It leaves out that no data was lost.** For an invoicing product, that's probably the first thing customers want to know.
- **Calling the credit a "thank-you for your patience" is slightly off.** It's compensation for your failure, so say so plainly.
- "Technical issue on our side" is fine for this audience, but one plain sentence about the cause builds more trust.

**Priya's version: accurate, but written for engineers**
- `/v2/invoices`, HTTP 503, TLS handshakes, "upstream pool" and "tenants" mean nothing to most customers.
- It has no apology and **doesn't mention the 10% credit**, which is the one thing customers can act on.
- **It may contradict your own facts.** You told me the expired cert was on the *internal* load balancer, but Priya writes "edge LB." Check which is right before any technical detail goes out.
- The subject line "Incident report INC-2291" reads like an internal ticket and will probably go unopened.

Priya's text is still useful. It works well as a status-page post-mortem or as a link for customers who want the technical detail.

## Suggested email

> **Subject: Invoicing outage on September 24 – what happened and your credit**
>
> Hi,
>
> On September 24, from 14:05 to 16:45 UTC (about 2 hours 40 minutes), our invoicing service was unavailable and you couldn't create invoices. We know that means delays in getting paid, and we're sorry.
>
> **What happened:** A security certificate on one of our internal servers expired, which blocked connections to the invoicing service. This was our mistake.
>
> **Your data is safe:** No data was lost. Everything you created before the outage is intact.
>
> **Your credit:** We've applied a 10% credit to your next month's bill. You don't need to do anything.
>
> **What we've changed:** We now have automated alerts that warn us 30 days before any certificate expires, so this kind of failure is caught well in advance.
>
> If you have any questions or were affected in ways we should know about, just reply to this email.
>
> [Name]
> [Title, Company]

## Check these before sending
1. **Recipients.** Send this only to the ~1,900 affected accounts, since the credit applies only to them. If it goes to everyone, change the credit wording.
2. **Credit mechanics.** Make sure billing has actually set up the automatic credit, so "no need to do anything" is true.
3. **Internal vs. edge load balancer.** Resolve the mismatch in Priya's draft if you publish a technical post-mortem.
4. **"Invoices created before the outage."** Confirm nothing that was in progress at 14:05 was dropped. If it was, customers need to know which actions to retry.


## Answer B
I wouldn't send either one as written. Use Marcus's tone and Priya's accuracy. Each draft has a problem that should keep it out of customers' inboxes.

## What's wrong with each

**Marcus's version has factual errors and a promise you can't keep.**
- **"About an hour" is wrong.** The outage ran from 14:05 to 16:45 UTC, which is 2 hours 40 minutes. Customers who were affected know how long it lasted. Understating it looks like spin, and that will cost you more trust than the outage did.
- **"It will never happen again"** is a promise nobody can back up. The cert alerts reduce the chance of this specific failure. They don't guarantee zero incidents.
- **It leaves out things customers want to know:** the date, that no data was lost, and what you changed to prevent a repeat.
- **"Thanks for sticking with us" and "thank-you for your patience"** turn compensation into a favour. The credit is there because you let them down, so say that.

**Priya's version is accurate, but it's written for engineers.**
- "Edge LB", "upstream pool", "TLS handshake failures" and "tenants" mean nothing to someone who just wanted to send an invoice.
- It doesn't apologise and doesn't mention the 10% credit, which is the part most customers will care about.
- "Incident report INC-2291" as a subject line is likely to get skimmed or ignored.

## Suggested merged version

> **Subject: Invoicing outage on September 24: what happened and your account credit**
>
> Hi [Name],
>
> On September 24, from 14:05 to 16:45 UTC, you couldn't generate invoices through our platform. We know that means delays in getting paid, and we're sorry.
>
> **What happened:** A security certificate on part of our internal infrastructure expired. Our systems stopped accepting connections to the invoicing service until we replaced it.
>
> **Your data is safe:** No data was lost, and all your existing invoices and records are intact.
>
> **Your credit:** We're applying a 10% credit to your next month's bill. You don't need to do anything.
>
> **What we've changed:** We now have automated alerts that warn us 30 days before any certificate expires, so this kind of failure is caught well ahead of time.
>
> If you have questions or noticed anything unusual in your account, reply to this email or contact [support channel].
>
> [Name], [Title]

## Before you send

- **Send it only to the ~1,900 affected accounts.** Marcus's draft reads like it's going to everyone. If unaffected customers get "10% off your next bill", you'll either have to honour credits you didn't plan for or walk it back.
- **Check what "next month's bill" means for customers on annual or usage-based billing** so the credit line is accurate for them.
- **Sign it from a real person** (head of engineering, CEO or head of support) rather than a no-reply address. That makes the apology read as sincere.
- **Keep Priya's version for your status page or post-incident review,** where the technical detail belongs.

