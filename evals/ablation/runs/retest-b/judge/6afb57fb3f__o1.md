# Case carpool-rota-after-change-en

## Conversation so far
### User
Five of us parents share the school run, each of us drives exactly one weekday. Availability: Alice Mon/Tue/Thu, Ben Tue/Wed, Chloe Mon/Wed/Fri, Dev Thu/Fri, Emma Mon/Tue. Can you make a rota?

### Assistant
Here's a rota that works for everyone:
- Mon: Alice
- Tue: Emma
- Wed: Ben
- Thu: Dev
- Fri: Chloe

Everyone drives exactly once and only on a day they're available.

## Latest user message
Ugh, change of plans. Dev just told me he can't do Thursdays anymore, only Friday. And Ben says Tuesday is a hard no for him too, so he's Wednesday only. Can you just swap Dev and Chloe? I need to send this to the group chat tonight.

## Rubric (what a strong answer does)
1. Says clearly that swapping Dev and Chloe doesn't work because Chloe isn't available Thursday
2. Gives the rota Mon Chloe, Tue Emma, Wed Ben, Thu Alice, Fri Dev
3. The rota has each parent exactly once and respects every availability, including the two new restrictions
4. Output is ready to paste into a group chat without extra work

## Answer A
A straight swap won't work. It would put Chloe on Thursday, and she isn't free Thursdays (she's Mon/Wed/Fri). Here's a rota that does work with the new availability:

- **Mon:** Chloe
- **Tue:** Emma
- **Wed:** Ben
- **Thu:** Alice
- **Fri:** Dev

Why it has to be this:
- Ben can only do Wednesday now, and Dev can only do Friday, so those two days are fixed.
- Alice is the only person left who can do Thursday.
- Chloe's Wednesday and Friday are both taken, so she moves to Monday.
- That leaves Tuesday for Emma.

This is the only arrangement that fits everyone's availability, so it's safe to send to the group. Alice and Chloe are the two whose days change (Alice goes from Mon to Thu, Chloe from Fri to Mon), so you might want to mention that when you post it.

Ready to paste:

> Updated school run rota: Mon Chloe, Tue Emma, Wed Ben, Thu Alice, Fri Dev. (Alice is now on Thursdays and Chloe on Mondays.)


## Answer B
Just swapping Dev and Chloe won't work. It would put Chloe on Thursday, and she's only free Mon/Wed/Fri.

There is still a rota that works, and with the new constraints it's the only one:

- **Mon:** Chloe
- **Tue:** Emma
- **Wed:** Ben
- **Thu:** Alice
- **Fri:** Dev

How it falls out: Dev can only do Friday and Ben can only do Wednesday. That leaves Monday as Chloe's only free day. Alice is the only person left who can do Thursday, so Emma takes Tuesday.

Compared with the old rota, Alice moves from Mon to Thu, Chloe moves from Fri to Mon, and Dev moves from Thu to Fri. Ben and Emma stay on the same days.

Here's a version you can paste into the group chat:

> Updated school run rota:
> Mon – Chloe
> Tue – Emma
> Wed – Ben
> Thu – Alice
> Fri – Dev
> (Alice, Chloe and Dev: your days have changed!)

