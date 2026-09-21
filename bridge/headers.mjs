import { readFileSync, statSync } from 'node:fs';
const path = process.argv[2];
if (!path || (statSync(path).mode & 0o077)) throw new Error('Private config (0600) required');
process.stdout.write(JSON.stringify(JSON.parse(readFileSync(path, 'utf8')).headers));
