```python
def chunk(items, size):
    """Split items into consecutive lists of at most `size` elements."""
    if size < 1:
        raise ValueError(f"size must be at least 1, got {size}")
    return [items[i:i + size] for i in range(0, len(items), size)]
```

Notes:

- Validation happens at the boundary, before any work, so a bad `size` fails fast even for an empty list.
- The last chunk holds the remainder, so it may be shorter than `size`; `chunk([], 3)` returns `[]`.
- O(n) time and O(n) space — each element is copied once via slicing.
- Slicing requires a sequence. If you want to accept any iterable (including generators), say so and I'll switch to an `itertools.islice` loop instead.
