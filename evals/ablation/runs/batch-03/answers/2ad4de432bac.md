Yes, you can rewrite them mechanically. Two things will bite you, though. A plain sed that turns `pricing._apply_tier_discount` into `pricing.discounts.apply_tier` won't work, because of how `patch.object` takes its arguments. And a green suite after the rewrite tells you less than you might expect. Here's a script that does the rewrite properly, then what to watch for.

## Why a naive sed breaks

`patch.object(target, "name")` looks up **one attribute on one object**. This does not work:

```python
patch.object(pricing, "discounts.apply_tier")   # AttributeError: no attribute 'discounts.apply_tier'
```

The object you pass has to change along with the name:

```python
patch.object(pricing.discounts, "apply_tier", return_value=Decimal("10.00"))
```

So the rewrite changes two things: the first argument (`pricing` → `pricing.discounts`) and the attribute string (`"_apply_tier_discount"` → `"apply_tier"`). A regex can handle that, as long as it only touches the `patch.object(...)` call.

## Rewrite script

Put your real mapping in `MOVES`. The component attribute names and new method names below are placeholders I guessed from your message, so check them against the new classes.

```python
# tools/rewrite_patch_targets.py
import re
import sys
from pathlib import Path

# old private method -> (component attribute on OrderPricing, new method name)
MOVES: dict[str, tuple[str, str]] = {
    "_apply_tier_discount": ("discounts", "apply_tier"),
    "_compute_vat":         ("tax",       "compute_vat"),
    "_lookup_zone":         ("shipping",  "lookup_zone"),
    # ...add the rest
}

PATCH_OBJECT = re.compile(
    r'patch\.object\(\s*(?P<obj>[A-Za-z_]\w*)\s*,\s*(?P<q>["\'])(?P<name>_\w+)(?P=q)'
)


def rewrite(source: str, unmapped: set[str]) -> str:
    def repl(m: re.Match[str]) -> str:
        name = m["name"]
        if name not in MOVES:
            unmapped.add(name)
            return m[0]
        component, new_name = MOVES[name]
        return f'patch.object({m["obj"]}.{component}, "{new_name}"'
    return PATCH_OBJECT.sub(repl, source)


def main(paths: list[str]) -> None:
    unmapped: set[str] = set()
    for p in map(Path, paths):
        old = p.read_text()
        new = rewrite(old, unmapped)
        if new != old:
            p.write_text(new)
            print(f"rewrote {p}")
    if unmapped:
        print("NOT mapped (left untouched):", ", ".join(sorted(unmapped)), file=sys.stderr)


if __name__ == "__main__":
    main(sys.argv[1:])
```

```bash
git switch -c fix/pricing-test-targets
python tools/rewrite_patch_targets.py tests/test_pricing*.py
git diff --stat
pytest tests/ -q -x
```

Run it on a branch so `git diff` shows you exactly what changed. I haven't run this against your code, so read the diff before you trust it.

Afterwards, grep for anything the regex missed. Two likely cases are string-form patches like `patch("app.pricing.OrderPricing._compute_vat")` and `@patch.object(OrderPricing, "_x")` used as a class decorator:

```bash
grep -rnE 'OrderPricing\._|patch\.object\([^,]+,\s*["'\'']_' tests/
```

For the class-level form, the target becomes the new class: `patch.object(DiscountEngine, "apply_tier")`. For the string form, use the new class's path, e.g. `"app.pricing.discounts.DiscountEngine.apply_tier"`.

## Things the rewrite won't fix

1. **Instance patching only works if the components exist before `total()` runs.** `patch.object(pricing.discounts, ...)` needs `OrderPricing.__init__` to create `self.discounts` and `total()` to call it through that same attribute. If `total()` builds a new `DiscountEngine(...)` inside itself, the patch applies to a different object. The test then fails, or worse, silently runs the real code. In that case, patch the class instead: `patch.object(DiscountEngine, "apply_tier")`.

2. **Signatures probably changed.** `m.assert_called_once_with(order, "VIP")` assumes `apply_tier` still takes `(order, tier)`. If the engine now takes a subtotal, or holds the order itself, those assertions will fail after the rewrite. You'd have to fix them by hand, since a regex can't know the new call shape. Adding `autospec=True` to the rewritten patches is worth doing: a mismatched call then raises `TypeError` straight away, instead of passing against a mock that accepts anything.

3. **You said "almost all" of the 46 look like this.** Check the others separately rather than assuming the script covers them.

## Pushback on "green before Friday"

These 46 tests broke when behaviour **didn't** change, and your 2,000-order comparison shows it didn't. That tells you they test wiring, not pricing. After the rewrite they'll be coupled to the new internals instead of the old ones. They'll break again the next time you move a method, and they still won't catch a wrong total. A patched `_compute_vat` with `vat.assert_called_once()` passes whatever VAT comes out.

For the release, your staging comparison is stronger evidence than a re-greened mock suite. A good use of an hour before Friday is to save that comparison as a regression test, which will survive the next refactor:

```python
# tests/test_pricing_golden.py
import json
from decimal import Decimal
from pathlib import Path

import pytest

CASES = json.loads(Path(__file__).with_name("pricing_golden.json").read_text())

@pytest.mark.parametrize("case", CASES, ids=lambda c: c["order_id"])
def test_total_matches_pre_refactor(case):
    order = build_order(case["order"])          # your existing fixture/factory
    assert OrderPricing(order).total() == Decimal(case["expected_total"])
```

Take a few hundred of those 2,000 orders, anonymised. Pick them to cover VIP/non-VIP, EU/non-EU and the zero-tax and free-shipping edge cases, and store the totals from before the refactor. After the release, I'd move the discount, tax and shipping unit tests onto the three new classes directly (`DiscountEngine(...).apply_tier(...) == Decimal("10.00")`, no mocks). Then drop the `OrderPricing` tests that only check delegation, keeping a few that check composition.

**Confidence: MEDIUM.** The `patch.object` behaviour and the regex approach are standard. I haven't seen your new class layout, so the mapping, the attribute names and whether the components are created in `__init__` all need checking against your code.
