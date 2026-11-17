import { mkdtemp, rm } from 'node:fs/promises';
import net from 'node:net';
import { tmpdir } from 'node:os';
import path from 'node:path';
import type { TestProject } from 'vitest/node';

declare module 'vitest' {
  export interface ProvidedContext {
    adminUrl: string;
  }
}

function freePort(): Promise<number> {
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    server.once('error', reject);
    server.listen(0, '127.0.0.1', () => {
      const address = server.address();
      const port = typeof address === 'object' && address ? address.port : 0;
      server.close(() => resolve(port));
    });
  });
}

/**
 * Tests need a real Postgres. With DATABASE_URL set (CI), they use that server.
 * Otherwise a throwaway Postgres is started from the embedded-postgres package.
 */
export default async function setup(project: TestProject): Promise<(() => Promise<void>) | void> {
  if (process.env.DATABASE_URL) {
    project.provide('adminUrl', process.env.DATABASE_URL);
    return;
  }

  const { default: EmbeddedPostgres } = await import('embedded-postgres');
  const databaseDir = await mkdtemp(path.join(tmpdir(), 'factory-pg-'));
  const port = await freePort();
  const server = new EmbeddedPostgres({
    databaseDir,
    user: 'postgres',
    password: 'postgres',
    port,
    persistent: false,
    onLog: () => {},
    onError: () => {},
  });
  await server.initialise();
  await server.start();
  project.provide('adminUrl', `postgres://postgres:postgres@127.0.0.1:${port}/postgres`);

  return async () => {
    await server.stop();
    await rm(databaseDir, { recursive: true, force: true }).catch(() => {});
  };
}
