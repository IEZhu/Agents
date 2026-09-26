# Case tls-verify-code-21-not-expiry-en

## Conversation so far
### User
Our Python service calling https://api.partner-internal.net suddenly fails with `requests.exceptions.SSLError: ... CERTIFICATE_VERIFY_FAILED ... unable to get local issuer certificate`. What happened?

### Assistant
Most likely the server's certificate has expired. Ask the partner to renew it; in the meantime you can confirm by checking the certificate dates in a browser.

## Latest user message
Opened it in Chrome, padlock is fine, cert valid until March 2027. Then ran `openssl s_client -connect api.partner-internal.net:443 -servername api.partner-internal.net` and it ends with `Verify return code: 21 (unable to verify the first certificate)`. Also only one cert shows in the chain output. Partner says they replaced the cert last Friday. Should I just set verify=False until they sort it out?

## Rubric (what a strong answer does)
1. Explicitly drops the expiry hypothesis based on the valid-until-2027 observation
2. Identifies the cause as the server not sending the intermediate certificate (incomplete chain, likely since last Friday's replacement), consistent with only one cert shown and verify code 21
3. Explains why Chrome still works (browsers can compensate for a missing intermediate, e.g. cached intermediates or fetching it via the AIA URL) while Python does not
4. Advises against verify=False and gives a safer path: the partner configures the full chain; as a stopgap the client points verify at a CA bundle that contains the intermediate along with the usual roots

## Answer A
No, don't set `verify=False`. I also got the first diagnosis wrong: the cert hasn't expired. Your `openssl` output shows the partner's server is **not sending the intermediate certificate**. When they swapped the cert on Friday, they installed the new leaf but left out the chain.

### Why Chrome looks fine but Python fails

- **One cert in the chain output + `Verify return code: 21 (unable to verify the first certificate)`** is the typical sign of a missing intermediate. The server sends only its own leaf cert, so the client can't build a path from it to a trusted root.
- **Chrome fills the gap itself.** It fetches the missing intermediate from the URL in the cert's AIA ("CA Issuers") field, or reuses one it has cached from another site. So the padlock stays green.
- **Python `requests` doesn't do that.** OpenSSL doesn't fetch AIA URLs, and `requests` checks against the `certifi` bundle, which holds only roots. With no intermediate, the chain breaks and you get `unable to get local issuer certificate`.

### The actual fix is on the partner's side

Ask them to configure the **full chain**: leaf plus intermediate(s), usually a `fullchain.pem` or the CA's "bundle" file (nginx `ssl_certificate` → fullchain; Apache `SSLCertificateFile` with the chain appended, or `SSLCertificateChainFile` on older versions). It's a small config change and a reload. Send them your `openssl s_client` output as proof.

They can check the fix with:
```bash
openssl s_client -connect api.partner-internal.net:443 \
  -servername api.partner-internal.net -showcerts </dev/null
# Expect: 2+ certs in the chain and "Verify return code: 0 (ok)"
```
If the host is public, the SSL Labs server test also flags "Chain issues: Incomplete".

### Stopgap while you wait: add the intermediate yourself

This keeps verification on:

1. Find the issuer URL in the leaf cert:
   ```bash
   openssl s_client -connect api.partner-internal.net:443 -servername api.partner-internal.net </dev/null 2>/dev/null \
     | openssl x509 -noout -text | grep -A1 "Authority Information Access"
   ```
2. Download the intermediate from that "CA Issuers" URL. It's often DER, so convert it: `openssl x509 -inform DER -in intermediate.crt -out intermediate.pem`. Check that its subject matches the leaf's issuer.
3. Build a bundle of the certifi roots plus that intermediate, and point only this client at it:
   ```python
   import certifi, requests

   # one-time build (or do it in your image build / config management):
   #   cat $(python -m certifi) intermediate.pem > partner-ca-bundle.pem

   resp = requests.get(
       "https://api.partner-internal.net/...",
       verify="/etc/myservice/partner-ca-bundle.pem",  # still verifies, just with the missing link added
       timeout=10,
   )
   ```
   You can use `REQUESTS_CA_BUNDLE=/path/to/bundle.pem` instead, but that affects every `requests` call in the process. The per-call `verify=` keeps the change narrow.
4. Put the intermediate in config or the image, not somewhere ad hoc, and add a ticket to **remove it** once the partner fixes their chain. Intermediates rotate, and a stale pinned one will break things again later.

**Blast radius:** only calls to this partner, if you use per-call `verify=`. **Rollback:** remove the `verify=` argument once `openssl` shows `Verify return code: 0`.

### Why not `verify=False`

- It turns off authentication for this connection completely. Anyone who can intercept the traffic can read or change your requests and responses, including auth tokens and partner data. `-internal` in the hostname doesn't make that safe.
- "Temporary" workarounds like this tend to stay in the code for good.
- The safe workaround above takes about 10 minutes, so there's no real reason to give up verification.

One caveat: if the leaf turns out to be issued by a private or corporate CA rather than a public one, Chrome may trust it through your OS trust store, which certifi doesn't use. In that case, put that CA's root (plus any intermediate) in the bundle instead. The approach is the same, and the partner still needs to fix the missing intermediate.


## Answer B
No, don't set `verify=False`. My earlier answer was wrong: the cert hasn't expired. Your `openssl` output points to a different cause. When the partner replaced the cert on Friday, their server stopped sending the **intermediate CA certificate** and now sends only the leaf.

## Why this is the cause

- **Only one cert in the chain** plus `Verify return code: 21 (unable to verify the first certificate)` is the usual sign of a missing intermediate. The client can't link the leaf to any root it trusts.
- **Chrome still works** because browsers fill the gap. They fetch the missing intermediate from the AIA "CA Issuers" URL inside the cert, or they reuse an intermediate cached from another site. Python's `ssl`/OpenSSL doesn't fetch from AIA, so `requests` fails. A padlock in the browser doesn't prove the server config is correct.
- **The timing matches.** The failures started right after the cert swap. They most likely installed `cert.pem` where they should have installed `fullchain.pem`, or they left out the CA bundle in their nginx, ALB or F5 config.

## Confirm (2 minutes)

```bash
# Show every cert the server sends. Expect 2+ blocks; you'll see 1.
openssl s_client -connect api.partner-internal.net:443 \
  -servername api.partner-internal.net -showcerts </dev/null

# Find which intermediate is missing (look for "CA Issuers - URI:")
openssl s_client -connect api.partner-internal.net:443 \
  -servername api.partner-internal.net </dev/null 2>/dev/null \
  | openssl x509 -noout -issuer -text | grep -A1 "Authority Information Access"
```

## Fix

**Real fix (theirs):** the server must send leaf + intermediate(s). Send them your `s_client` output as evidence. The fix is a config change on their side and doesn't need a new cert. If the host is public, SSL Labs will flag it as "Chain issues: Incomplete". For an internal host, `s_client -showcerts` is enough proof.

**Stopgap (yours), safe and scoped to this one host:**

1. Download the intermediate from the AIA URL above. It's often DER, so convert it with `openssl x509 -inform DER -in inter.crt -out inter.pem`.
2. Build a bundle: your normal trusted roots plus that intermediate.
   ```bash
   cat "$(python -c 'import certifi; print(certifi.where())')" inter.pem > partner-ca-bundle.pem
   ```
   If the partner cert chains to a private or corporate CA and not a public one, use that root in place of the certifi bundle.
3. Point only this client at it:
   ```python
   session.verify = "/etc/myservice/partner-ca-bundle.pem"
   ```
   You can use `REQUESTS_CA_BUNDLE` instead, but it applies to the whole process. `verify=<path>` *replaces* the default bundle and does not add to it, which is why step 2 concatenates the two.
4. Verify:
   ```bash
   openssl s_client -connect api.partner-internal.net:443 \
     -servername api.partner-internal.net -CAfile partner-ca-bundle.pem </dev/null | grep "Verify return code"
   # expect: Verify return code: 0 (ok)
   ```

Ship the bundle through your normal config or secret path, not something hand-edited on a box. Put a ticket or TODO on it: remove the override once the partner fixes their chain. The intermediate also expires or rotates, and when it does this workaround will break.

## Why not `verify=False`

- It switches off authentication completely, so any machine on the path can MITM the connection and read or alter the traffic. Your service probably sends credentials or tokens to this API, which makes this a real risk.
- "Temporary" disables tend to stay in the code. The pinned intermediate gives you the same uptime and keeps verification on.

**Blast radius / rollback:** the stopgap only affects calls to this one partner. If it misbehaves, revert to the default `verify=True`, which puts you back in the current failure state with nothing worse. Once the partner fixes their chain, the default bundle works again.

