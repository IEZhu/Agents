#!/usr/bin/env python3
"""Local token-ID transport probe. Does not run a model or send network requests."""
import argparse
import copy
import hashlib
import json
from pathlib import Path
import struct
import sys
import zlib

import tokenizers
from tokenizers import Tokenizer

MAX_INPUT_BYTES = 1_000_000
MAX_TOKENIZER_BYTES = 50_000_000
MAX_IDS = 1_000_000
RENDER_PROFILE = "already-rendered-worker-context-utf8-v1-no-chat-template"
DEFAULT_INPUT = Path(__file__).parent / "fixtures/worker-context.txt"
DEFAULT_OUTPUT = Path(__file__).parent / "results/token-transport.json"
DEFAULT_PROFILES = [
    ("harrier", "/Users/alexey.zhuchkov/.cache/fastembed/local/microsoft--harrier-oss-v1-270m/d59c919d0159aea2c19ed7d04288fcdd048d0f9c/tokenizer.json"),
    ("embeddinggemma", "/Users/alexey.zhuchkov/.cache/fastembed/local/google--embeddinggemma-300m/5090578d9565bb06545b4552f76e6bc2c93e4a66/tokenizer.json"),
]


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def json_bytes(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def bounded_read(path, limit):
    with Path(path).open("rb") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError(f"file exceeds {limit} bytes: {path}")
    return data


def load_profile(name, path):
    raw = bounded_read(path, MAX_TOKENIZER_BYTES)
    tokenizer = Tokenizer.from_str(raw.decode("utf-8"))
    tokenizer.no_padding()
    tokenizer.no_truncation()
    profile = {
        "name": name,
        "tokenizer_sha256": sha256(raw),
        "tokenizers_version": tokenizers.__version__,
        "render_profile": RENDER_PROFILE,
        "add_special_tokens": False,
        "padding": False,
        "truncation": False,
        "decode_skip_special_tokens": False,
    }
    return tokenizer, profile, raw


def pack_ids(ids):
    if not isinstance(ids, list) or len(ids) > MAX_IDS:
        raise ValueError("invalid ID list or ID count limit exceeded")
    if any(type(item) is not int or not 0 <= item <= 0xFFFFFFFF for item in ids):
        raise ValueError("IDs must be unsigned 32-bit integers")
    return struct.pack(f"<{len(ids)}I", *ids)


def make_packet(profile, input_bytes, ids):
    payload = {
        "schema": "accord-token-transport-probe/1",
        "profile": profile,
        "profile_sha256": sha256(json_bytes(profile)),
        "input_sha256": sha256(input_bytes),
        "input_utf8_bytes": len(input_bytes),
        "token_count": len(ids),
        "ids_sha256": sha256(pack_ids(ids)),
        "ids": ids,
    }
    return {"payload": payload, "payload_sha256": sha256(json_bytes(payload))}


def verify_and_decode(packet, expected_profile, expected_input_sha256,
                      expected_input_bytes, decode):
    """Compare against separately supplied expectations before decoding or use.

    Hashes detect accidental corruption and bind bytes; they are not signatures.
    The caller is responsible for obtaining trustworthy expected values.
    """
    if set(packet) != {"payload", "payload_sha256"}:
        raise ValueError("invalid packet envelope")
    payload = packet["payload"]
    if set(payload) != {"schema", "profile", "profile_sha256", "input_sha256",
                        "input_utf8_bytes", "token_count", "ids_sha256", "ids"}:
        raise ValueError("invalid payload fields")
    if payload["schema"] != "accord-token-transport-probe/1":
        raise ValueError("unsupported schema")
    # Binding is stricter than behavioral equivalence of two tokenizer files.
    if payload["profile"] != expected_profile:
        raise ValueError("tokenizer/render profile mismatch")
    if payload["profile_sha256"] != sha256(json_bytes(expected_profile)):
        raise ValueError("profile integrity mismatch")
    if payload["input_sha256"] != expected_input_sha256:
        raise ValueError("input hash mismatch")
    if payload["input_utf8_bytes"] != expected_input_bytes:
        raise ValueError("input length mismatch")
    packed_ids = pack_ids(payload["ids"])
    if payload["token_count"] != len(payload["ids"]):
        raise ValueError("token count mismatch")
    if payload["ids_sha256"] != sha256(packed_ids):
        raise ValueError("IDs integrity mismatch")
    if packet["payload_sha256"] != sha256(json_bytes(payload)):
        raise ValueError("payload integrity mismatch")
    decoded = decode(payload["ids"])
    decoded_bytes = decoded.encode("utf-8")
    if (sha256(decoded_bytes) != expected_input_sha256
            or len(decoded_bytes) != expected_input_bytes):
        raise ValueError("decoded input differs from expected source")
    return decoded


def test_rejection(name, packet, expected_profile, raw, decode, before_decode=True):
    calls = []

    def observed_decode(ids):
        calls.append(True)
        return decode(ids)

    try:
        verify_and_decode(packet, expected_profile, sha256(raw), len(raw), observed_decode)
    except ValueError as error:
        passed = not calls if before_decode else True
        return {"name": name, "status": "passed" if passed else "failed",
                "error": str(error), "decode_called": bool(calls)}
    return {"name": name, "status": "failed", "error": "packet was accepted",
            "decode_called": bool(calls)}


def run_checks(profiles, packets, raw):
    tokenizer, profile, _ = profiles[0]
    decode = lambda ids: tokenizer.decode(ids, skip_special_tokens=False)
    packet = packets[0]
    checked = verify_and_decode(packet, profile, sha256(raw), len(raw), decode)
    checks = [{"name": "valid packet roundtrip", "status": "passed"
               if checked.encode("utf-8") == raw else "failed"}]
    checks.append(test_rejection("wrong tokenizer profile rejected before decode",
                                 packet, profiles[1][1], raw, decode))
    wrong_render = copy.deepcopy(profile)
    wrong_render["render_profile"] = "different-chat-template-v1"
    checks.append(test_rejection("wrong render profile rejected before decode",
                                 packet, wrong_render, raw, decode))
    tampered = copy.deepcopy(packet)
    tampered["payload"]["ids"][0] ^= 1
    checks.append(test_rejection("tampered IDs rejected before decode",
                                 tampered, profile, raw, decode))
    invalid = copy.deepcopy(packet)
    invalid["payload"]["ids"][0] = -1
    checks.append(test_rejection("negative ID rejected before decode",
                                 invalid, profile, raw, decode))
    changed_source = copy.deepcopy(packet)
    changed_source["payload"]["input_sha256"] = "0" * 64
    checks.append(test_rejection("unexpected input rejected before decode",
                                 changed_source, profile, raw, decode))
    # Recomputing unkeyed hashes is possible; trusted source binding is still checked.
    rehashed = make_packet(profile, raw, [])
    checks.append(test_rejection("rehashed wrong text rejected before return",
                                 rehashed, profile, raw, decode, before_decode=False))
    return checks


def run(input_path, output_path, profile_paths):
    raw = bounded_read(input_path, MAX_INPUT_BYTES)
    text = raw.decode("utf-8")  # Preserve original newlines and exact UTF-8 bytes.
    if not raw:
        raise ValueError("probe input must be nonempty")
    profiles = [load_profile(name, path) for name, path in profile_paths]
    if len(profiles) < 2:
        raise ValueError("at least two tokenizer profiles are required")
    if len({profile[1]["tokenizer_sha256"] for profile in profiles}) < 2:
        raise ValueError("at least two distinct tokenizer files are required")
    rows = []
    packets = []
    artifacts = []
    for (tokenizer, profile, tokenizer_raw), (_, path) in zip(profiles, profile_paths):
        ids = tokenizer.encode(text, add_special_tokens=False).ids
        packed = pack_ids(ids)
        packet = make_packet(profile, raw, ids)
        decoded = tokenizer.decode(ids, skip_special_tokens=False)
        exact = decoded.encode("utf-8") == raw
        checked = verify_and_decode(packet, profile, sha256(raw), len(raw),
                                    lambda value: tokenizer.decode(value, skip_special_tokens=False))
        packet_path = output_path.with_name(output_path.stem + f"-{profile['name']}-packet.json")
        packet_path.write_bytes(json_bytes(packet) + b"\n")
        artifacts.append(str(packet_path))
        persisted_packet = json.loads(packet_path.read_bytes())
        persisted_roundtrip = verify_and_decode(
            persisted_packet, profile, sha256(raw), len(raw),
            lambda value: tokenizer.decode(value, skip_special_tokens=False))
        packets.append(persisted_packet)
        rows.append({
            "name": profile["name"], "tokenizer_path": path, "profile": profile,
            "tokenizer_file_bytes": len(tokenizer_raw),
            "tokenizer_sha256": profile["tokenizer_sha256"],
            "input_sha256": sha256(raw), "decoded_sha256": sha256(decoded.encode("utf-8")),
            "encode_decode_exact_roundtrip": exact,
            "receiver_verified_roundtrip": checked.encode("utf-8") == raw,
            "serialized_packet_verified_roundtrip": persisted_roundtrip.encode("utf-8") == raw,
            "effective_padding": tokenizer.padding, "effective_truncation": tokenizer.truncation,
            "token_count": len(ids), "utf8_text_bytes": len(raw),
            "json_ids_bytes": len(json_bytes(ids)), "uint32_le_ids_bytes": len(packed),
            "zlib_text_bytes_level_9": len(zlib.compress(raw, level=9)),
            "zlib_uint32_ids_bytes_level_9": len(zlib.compress(packed, level=9)),
            "packet_json_bytes": len(json_bytes(packet)),
            "packet_artifact_bytes": packet_path.stat().st_size,
            "packet_path": str(packet_path), "ids_sha256": sha256(packed),
        })
    checks = run_checks(profiles, packets, raw)
    first_json, second_json = (json.loads(profile[2]) for profile in profiles[:2])
    different_fields = sorted(key for key in set(first_json) | set(second_json)
                              if first_json.get(key) != second_json.get(key))
    report = {
        "experiment": "local encoder and integrity-bound transport only",
        "input_path": str(input_path), "input_sha256": sha256(raw),
        "input_utf8_bytes": len(raw), "tokenizers_version": tokenizers.__version__,
        "profiles": rows, "checks": checks,
        "comparison": {
            "first_two_id_sequences_equal": packets[0]["payload"]["ids"] == packets[1]["payload"]["ids"],
            "first_two_tokenizer_differing_top_level_fields": different_fields,
        },
        "limits": {"input_bytes": MAX_INPUT_BYTES, "tokenizer_bytes": MAX_TOKENIZER_BYTES,
                   "token_ids": MAX_IDS},
        "limitations": [
            "Cached model directories are embedding models; this probe loads only tokenizer JSON, with no model inference.",
            "Two tokenizer files may produce identical IDs on this input; this is not evidence for portability across unrelated tokenizers.",
            "Input is already-rendered worker-context text, not a chat template or engine request; no system/role/tool framing was measured.",
            "No native token-ID messaging was attempted. The agent tool accepts text, so these packets are only local artifacts.",
            "Hashes bind integrity to separately supplied expected profile and source values; they do not authenticate a sender.",
            "Token IDs are an encoding of text, not compiled prompt semantics, inference state, or a KV cache.",
            "Byte lengths are local serialization measurements. No billing, inference latency, quality, or KV-cache-saving claim is made.",
        ],
        "packet_artifacts": artifacts,
    }
    output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", default=DEFAULT_INPUT, type=Path)
    parser.add_argument("--output", default=DEFAULT_OUTPUT, type=Path)
    parser.add_argument("--profile", action="append", metavar="NAME=TOKENIZER_JSON",
                        help="Repeat to override both default local profiles")
    args = parser.parse_args()
    profile_paths = DEFAULT_PROFILES
    if args.profile:
        profile_paths = []
        for spec in args.profile:
            name, separator, path = spec.partition("=")
            if not separator or not name.isidentifier() or not path:
                parser.error("profile must be NAME=TOKENIZER_JSON with an identifier name")
            profile_paths.append((name, path))
        if len({name for name, _ in profile_paths}) != len(profile_paths):
            parser.error("profile names must be unique")
    try:
        report = run(args.input, args.output, profile_paths)
    except (OSError, ValueError) as error:
        parser.exit(2, f"probe failed: {error}\n")
    print(json.dumps({"output": str(args.output), "input_sha256": report["input_sha256"],
                      "profiles": [{key: row[key] for key in
                                    ("name", "token_count", "encode_decode_exact_roundtrip",
                                     "utf8_text_bytes", "json_ids_bytes", "uint32_le_ids_bytes",
                                     "zlib_text_bytes_level_9", "packet_json_bytes")}
                                   for row in report["profiles"]],
                      "checks": report["checks"]}, indent=2))
    return 0 if all(check["status"] == "passed" for check in report["checks"]) else 1


if __name__ == "__main__":
    sys.exit(main())
