```python
from typing import Sequence, TypeVar

T = TypeVar("T")


def chunk(items: Sequence[T], size: int) -> list[list[T]]:
    """Split `items` into consecutive lists of at most `size` elements.

    The final chunk is shorter when len(items) is not a multiple of size.
    """
    if size < 1:
        raise ValueError(f"size must be >= 1, got {size}")
    return [list(items[i:i + size]) for i in range(0, len(items), size)]
```

Notes:

- `chunk([], 3)` returns `[]`; `chunk([1, 2, 3, 4, 5], 2)` returns `[[1, 2], [3, 4], [5]]`.
- Validation happens before any slicing, so a bad `size` fails fast rather than looping.
- `list(...)` around each slice means the result is always lists, even when `items` is a tuple or another sequence, and the chunks never alias the input.
- O(n) time and O(n) space. For a lazy variant over an arbitrary iterable, `itertools.islice` in a generator is the usual approach, but that changes the return type, so I kept the list version you asked for.

I have not executed this here — it is straightforward slicing, but run it against your own cases if it is going into production code.
