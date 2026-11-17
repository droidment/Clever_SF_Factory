import { readdir, readFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { runMigrations as runWorkerMigrations } from 'graphile-worker';
import pg from 'pg';

export type Pool = pg.Pool;
export type Client = pg.PoolClient;

const MIGRATION_LOCK_ID = 727_001;
const MIGRATIONS_DIR = fileURLToPath(new URL('../migrations', import.meta.url));

export function createPool(connectionString: string): Pool {
  const pool = new pg.Pool({ connectionString, max: 10 });
  // An idle connection can drop (server restart, network). Without a listener
  // that error would crash the process; the pool replaces the connection itself.
  pool.on('error', (err) => {
    console.error('Idle database connection failed:', err.message);
  });
  // The same applies to a connection that is checked out when it drops.
  pool.on('connect', (client) => {
    client.on('error', (err) => {
      console.error('Database connection failed:', err.message);
    });
  });
  return pool;
}

export async function withTransaction<T>(
  pool: Pool,
  fn: (client: Client) => Promise<T>,
): Promise<T> {
  const client = await pool.connect();
  try {
    await client.query('begin');
    const result = await fn(client);
    await client.query('commit');
    return result;
  } catch (err) {
    await client.query('rollback').catch(() => {});
    throw err;
  } finally {
    client.release();
  }
}

/**
 * Applies the job queue's own schema, then every file in migrations/ that has
 * not run yet. Safe to call from several processes at once.
 */
export async function migrate(pool: Pool): Promise<void> {
  await runWorkerMigrations({ pgPool: pool });

  const files = (await readdir(MIGRATIONS_DIR)).filter((f) => f.endsWith('.sql')).sort();
  const client = await pool.connect();
  try {
    await client.query('select pg_advisory_lock($1)', [MIGRATION_LOCK_ID]);
    await client.query(
      `create table if not exists schema_migrations (
         name text primary key,
         applied_at timestamptz not null default now()
       )`,
    );
    for (const file of files) {
      const applied = await client.query('select 1 from schema_migrations where name = $1', [file]);
      if (applied.rowCount) continue;
      const sql = await readFile(path.join(MIGRATIONS_DIR, file), 'utf8');
      await client.query('begin');
      try {
        await client.query(sql);
        await client.query('insert into schema_migrations (name) values ($1)', [file]);
        await client.query('commit');
      } catch (err) {
        await client.query('rollback').catch(() => {});
        throw new Error(`Migration ${file} failed`, { cause: err });
      }
    }
  } finally {
    await client.query('select pg_advisory_unlock($1)', [MIGRATION_LOCK_ID]).catch(() => {});
    client.release();
  }
}
