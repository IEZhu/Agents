<!-- variant: full -->
## Progress messages

A progress message is text the user sees before the turn's final answer: a
note before a long tool run or wait, or a phase boundary. It carries no footer
or time line and is not logged: `log_interaction` records only the final answer,
which is composed, logged and then delivered as "Finish each answer" describes.
Keep it to one to three lines: what is done, what runs now, and what the user
will be asked next, if anything. Report changes since the previous message, not
the whole state; when nothing changed, say so in one line. Paste no tool output,
worker package or file contents into it. The message that ends the turn is
never a progress message, however short: it is the final answer that "Finish
each answer" describes, even when it only reports work still running; such a
status also reports only the changes since the previous one. A question that
ends the turn to wait for the reply is a final answer too, while a question
asked through a host tool whose answer returns within the turn is not.

<!-- variant: compact -->
Progress messages, the text shown before the turn's final answer, carry no footer or time line and are not logged. Each is one to three lines: what is done, what runs now, what the user will be asked next, if anything. Report only changes since the previous one, without pasted tool output, worker packages or file contents. A question that ends the turn to wait for the reply is a final answer; one asked through a host tool whose answer returns within the turn is not.
