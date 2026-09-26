# Case mutable-default-trace-en

## Conversation so far
(none)

## Latest user message
Interview prep, no interpreter handy. What does this print in Python 3.12, exactly?

```python
def add(item, bucket=[]):
    bucket.append(item)
    return bucket

a = add(1)
b = add(2, [])
c = add(3)
a += [4]
print(a, b, c, a is c)
```

My guess is `[1] [2] [3] False`.

## Rubric (what a strong answer does)
1. Gives the exact output `[1, 3, 4] [2] [1, 3, 4] True`
2. Explains that the default argument list is evaluated once at function definition and shared across calls that omit bucket, while b gets its own explicitly passed list
3. Explains that `a += [4]` extends the list in place (unlike `a = a + [4]`), so the change is visible through c and a is c remains True
4. Says the user's guess is wrong

## Answer A
It prints:

```
[1, 3, 4] [2] [1, 3, 4] True
```

Your guess `[1] [2] [3] False` is wrong in three places. The cause is the mutable default argument.

**What happens, step by step:**

1. `bucket=[]` is evaluated **once**, when `def` runs. Every call that leaves out `bucket` gets that same list object. Call it `D`.
2. `a = add(1)` appends to `D`, so `D == [1]` and `a is D`.
3. `b = add(2, [])` gets a fresh list, so `b == [2]`. The default isn't touched.
4. `c = add(3)` appends to the same `D`, so `D == [1, 3]` and `c is D`. Now `a` and `c` are the same object.
5. `a += [4]` is an in-place extend on a list (`list.__iadd__` mutates the list and returns `self`). It does not rebind `a` to a new object, so `D == [1, 3, 4]`. Both `a` and `c` show the change.
6. `a is c` is `True` because both names still refer to `D`.

**The interview trap in step 5:** `a = a + [4]` would behave differently. It would build a new list, so `a` would be `[1, 3, 4]`, `c` would stay `[1, 3]` and `a is c` would be `False`. `+=` on a list mutates the list in place. `+` creates a new list.

The usual fix:

```python
def add(item, bucket=None):
    if bucket is None:
        bucket = []
    bucket.append(item)
    return bucket
```

Nothing here is specific to 3.12. Default arguments have worked this way in every Python 3 version.

**Confidence: HIGH.** These are well-defined language rules (defaults are evaluated once, and `list.__iadd__` works in place). I worked through them by hand and didn't run the code.


## Answer B
It prints this, so your guess is wrong on three of the four values:

```
[1, 3, 4] [2] [1, 3, 4] True
```

(Your guess also dropped the commas. `print` shows a list using its `repr`, which is `[1, 3, 4]`.)

**Why:**

1. **Python evaluates a default argument once, when it runs the `def` line, not on each call.** So every call that leaves out `bucket` gets the same list object.
2. `a = add(1)` adds 1 to that shared default list. Now `a` points to it, and it holds `[1]`.
3. `b = add(2, [])` gets a new list passed in, so the default isn't touched. `b` is `[2]`.
4. `c = add(3)` uses the shared default list again and adds 3, so it now holds `[1, 3]`. `c` is **the same object** as `a`.
5. `a += [4]` changes the list in place. For a list, `+=` calls `list.__iadd__`, which extends the existing list and doesn't rebind `a` to a new one. Since `a` and `c` point to the same list, both now show `[1, 3, 4]`.
6. `a is c` is `True` because it's the same object the whole time.

**Follow-up interviewers like to ask:** if you write `a = a + [4]` instead, `+` makes a new list. Then `a` is `[1, 3, 4]` but `c` stays `[1, 3]`, and `a is c` becomes `False`.

**The usual fix:**

```python
def add(item, bucket=None):
    if bucket is None:
        bucket = []
    bucket.append(item)
    return bucket
```

Python has behaved this way for a very long time, and 3.12 is no different.

