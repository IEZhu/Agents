"""Closed, versioned contract data and deliberately restricted canonical encoding."""

from dataclasses import dataclass
import hashlib
import json
import re


class AccordError(ValueError):
    """A contract or runtime precondition was not satisfied."""


def require(condition, message):
    if not condition:
        raise AccordError(message)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def digest(kind, value):
    return hashlib.sha256(kind.encode("ascii") + b"\0" + canonical(value)).hexdigest()


def text_digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def identifier(value, *, revision=False):
    pattern = r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}" if revision else r"[A-Za-z][A-Za-z0-9_.-]{0,63}"
    require(type(value) is str and re.fullmatch(pattern, value) is not None,
            "invalid identifier")
    return value


def bounded_int(value, low, high):
    require(type(value) is int and low <= value <= high, "integer outside allowed bounds")
    return value


def revision_token(value):
    """Adapter-owned CAS token: bounded integer or opaque printable ASCII text."""
    if type(value) is int:
        return bounded_int(value, 0, 2**63 - 1)
    require(type(value) is str and 1 <= len(value) <= 128 and
            all(33 <= ord(char) <= 126 for char in value), "invalid revision token")
    return value


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, "duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(value):
    raise AccordError(f"nonfinite JSON: {value}")


@dataclass(frozen=True)
class Contract:
    name: str
    inputs: tuple[str, ...]
    output_max_bytes: int
    checks: tuple[tuple[str, str], ...]
    publish_to: tuple[str, ...]
    request_budget: int
    policy_revision: str

    def __post_init__(self):
        identifier(self.name)
        identifier(self.policy_revision, revision=True)
        bounded_int(self.output_max_bytes, 1, 65536)
        bounded_int(self.request_budget, 0, 10000)
        for items in (self.inputs, self.publish_to):
            require(type(items) is tuple, "resource lists must be immutable tuples")
            for item in items:
                identifier(item)
            require(len(set(items)) == len(items), "duplicate resource")
        require(type(self.checks) is tuple and len(self.checks) > 0, "checks must be nonempty")
        for item in self.checks:
            require(type(item) is tuple and len(item) == 2, "invalid check")
            identifier(item[0])
            identifier(item[1], revision=True)
        require(len(dict(self.checks)) == len(self.checks), "duplicate check")
        object.__setattr__(self, "inputs", tuple(sorted(self.inputs)))
        object.__setattr__(self, "publish_to", tuple(sorted(self.publish_to)))
        object.__setattr__(self, "checks", tuple(sorted(self.checks)))
        require(len(canonical(self.to_dict())) <= 65536, "contract too large")

    def to_dict(self):
        return {"ir": "accord.contract/0.1", "name": self.name,
                "inputs": list(self.inputs), "output_max_bytes": self.output_max_bytes,
                "checks": dict(self.checks), "publish_to": list(self.publish_to),
                "request_budget": self.request_budget, "policy_revision": self.policy_revision}

    @property
    def revision(self):
        return digest("accord.contract/0.1", self.to_dict())

    @classmethod
    def parse(cls, data):
        if isinstance(data, (str, bytes)):
            try:
                encoded = data.encode("utf-8") if isinstance(data, str) else data
            except UnicodeEncodeError:
                raise AccordError("invalid contract JSON")
            require(len(encoded) <= 65536, "contract too large")
            try:
                data = json.loads(data, object_pairs_hook=_unique_object,
                                  parse_constant=_reject_constant)
            except (ValueError, UnicodeError) as exc:
                raise AccordError("invalid contract JSON") from exc
        require(type(data) is dict, "contract must be an object")
        fields = {"ir", "name", "inputs", "output_max_bytes", "checks", "publish_to",
                  "request_budget", "policy_revision"}
        require(set(data) == fields, "unknown or missing contract fields")
        require(data["ir"] == "accord.contract/0.1", "unsupported IR version")
        require(type(data["checks"]) is dict, "checks must be an object")
        require(type(data["inputs"]) is list and type(data["publish_to"]) is list,
                "resources must be arrays")
        return cls(data["name"], tuple(data["inputs"]), data["output_max_bytes"],
                   tuple(data["checks"].items()), tuple(data["publish_to"]),
                   data["request_budget"], data["policy_revision"])
