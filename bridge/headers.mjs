import { readFileSync, statSync } from 'node:fs';
const path = process.argv[2];
// On Windows the owner-only ACL of the service directory protects the config (see stdio.mjs).
if (!path || (process.platform !== 'win32' && (statSync(path).mode & 0o077))) throw new Error('Private config (0600) required');
process.stdout.write(JSON.stringify(JSON.parse(readFileSync(path, 'utf8')).headers));
