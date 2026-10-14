# Real-model prompt and KV-cache pilot

This standard-library Python experiment compares complete rendered text and exact integer token input, each with an empty cache and a restored shared-prefix KV cache. It calls a real, locally cached Qwen3 8B model through llama.cpp. The [protocol](protocol.md) and [workload](workload.json) were frozen before scoring. See the [evaluation report](../../../docs/accord-inference-evaluation.md) for measurements and limitations.

The baseline is ordinary Python sending text to the native inference endpoint. Both representations use the same client, endpoint, model and sampling settings. This isolates representation and cache effects; it is not a comparison between unrelated hosted APIs or SDK frameworks. The inference optimization does not depend on an Accord compiler or even on the existing task-contract interpreter.

## Files

- `workload.json`: twelve frozen repository questions, strict expected answers, source excerpts and fingerprints. Expected answers and evidence metadata are never sent to the model.
- `workload.py`: workload loader and strict JSON scorer, including duplicate-key and Boolean/integer controls.
- `client.py`: loopback native client, streaming measurements and profile-bound prepared inputs. Hashes detect accidental changes against trusted metadata; they do not authenticate a sender.
- `run.py`: balanced 144-trial plan, one-time preparation, snapshot creation, cold-cache clearing and warm-cache restoration. Model failures are retained and abort the run without retries; wrong answers remain ordinary scored results.
- `analyze.py`: descriptive paired statistics and setup-inclusive local work accounting.
- `format_control.py`: separately specified warm comparison with both format orders, guarding against carryover in the primary rotation. Its [protocol](format-control-protocol.md) discloses its post-hoc motivation.
- `verify.py`: offline replay of scores, summaries, identity and token-accounting checks against retained raw evidence.
- `results/qwen3-8b-m4max-20261014/`: preparation, actual responses, timings, output tokens, effective generation settings and summary.

## Reproduce

Use the exact llama.cpp [b11429 macOS arm64 release](https://github.com/ggml-org/llama.cpp/releases/tag/b11429), archive SHA256 `740288ec6887be94280a5dfa25b5e23a78285cab104519e6c7e218904ee82459`, and the Qwen3 8B Q4_K Medium GGUF SHA256 `d98cdcbd03e17ce47681435b5150e34c1417f50b5c0019dd560e4882c5745785`. The archive reports build `b11429-d81235049`. The model already existed on the measured machine; the experiment downloaded no model weights and called no paid provider API.

From the repository worktree, create a private runtime directory and run a dedicated server. Substitute the paths to the pinned binary and model:

```sh
mkdir -p .accord-local/slots
/path/to/llama-server \
  --model /path/to/model.gguf --alias accord-qwen3-8b \
  --host 127.0.0.1 --port 18187 --parallel 1 --ctx-size 8192 \
  --gpu-layers 99 --flash-attn on --batch-size 512 --ubatch-size 512 \
  --threads 8 --threads-batch 8 --cache-ram 0 --no-cache-idle-slots \
  --no-context-shift --no-ui --slots --metrics --reasoning off \
  --slot-save-path .accord-local/slots
```

In a second terminal, use that server's PID:

```sh
python3 -m unittest experiments.accord.tests.test_inference -v
python3 -m experiments.accord.inference.run \
  --pid SERVER_PID --output .accord-local/new-measured-run
```

Do not point the runner at a shared server: it deliberately clears slot zero before each trial. The output directory must not exist. No trial retry or workload tuning occurs. The runner verifies the model digest, build, slot count, context size and source hashes; the recorded server command supplies the remaining configuration evidence. CPU/RSS sampling uses macOS `ps`. Hardware labels and the profile in this bounded pilot are fixed to the measured configuration, so adapting the code/configuration requires a separately identified experiment, not relabeling the original results.

After the primary run, the optional disclosed format-order control uses the same running server and snapshot:

```sh
python3 -m experiments.accord.inference.format_control \
  --primary .accord-local/new-measured-run \
  --output .accord-local/new-format-control --pid SERVER_PID
python3 -m experiments.accord.inference.verify .accord-local/new-measured-run
```

Stop the dedicated server after measurement. Keep the binary, logs and large KV snapshot in `.accord-local/`, which the repository ignores. Model weights can remain in their existing local cache. The raw result artifacts are deliberately committed; snapshot bytes and executable binaries are not.

## Boundaries

The native parent/worker API of the surrounding coding host still carries messages as text. This pilot demonstrates integer-input dispatch and repeated restoration of an actual model KV state through a dedicated engine, not a change to that host API. Restoring a local snapshot into the same engine profile models a controlled handoff. It does not demonstrate cross-process, cross-machine, cross-model or cross-tenant cache portability.

The prefix and snapshot are bound to model, engine, tokenizer-in-GGUF, chat template, source context, settings and cache configuration. The file digest is recorded once after creation; this trusted same-process pilot checks metadata before restoration, not the snapshot bytes on every request. A production cache store would need authenticated handles, tenant isolation, expiry, resource limits and actual compatibility checks. A cache miss must use the complete original input; every cold cell tests that execution path.

Input ID packets preserve the logical token count. They are neither semantic compilation nor a method to bypass context limits or billing. Text input can use the same cache optimization. API charges for this local run are zero; electricity and hardware cost are unmeasured.
