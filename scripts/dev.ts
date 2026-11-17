// Runs the factory against a local Postgres that this script starts itself,
// so no Docker or Postgres install is needed. Data is kept in .data/pg.
import { existsSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import EmbeddedPostgres from 'embedded-postgres';

const databaseDir = fileURLToPath(new URL('../.data/pg', import.meta.url));
const port = Number(process.env.PGPORT ?? 54329);

const postgres = new EmbeddedPostgres({
  databaseDir,
  user: 'postgres',
  password: 'postgres',
  port,
  persistent: true,
  onLog: () => {},
  onError: () => {},
});

if (!existsSync(path.join(databaseDir, 'PG_VERSION'))) {
  await postgres.initialise();
}
await postgres.start();
try {
  await postgres.createDatabase('factory');
} catch {
  // The database exists from an earlier run.
}
console.log(`Local Postgres running on port ${port} (data in .data/pg)`);

process.env.DATABASE_URL ??= `postgres://postgres:postgres@127.0.0.1:${port}/factory`;

let stopping = false;
for (const signal of ['SIGINT', 'SIGTERM'] as const) {
  process.on(signal, () => {
    if (stopping) return;
    stopping = true;
    // main.ts closes the server and worker on the same signal; give it a moment first.
    setTimeout(() => {
      postgres.stop().finally(() => process.exit(0));
    }, 1500);
  });
}

await import('../src/main.ts');
