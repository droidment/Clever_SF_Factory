import { randomBytes } from 'node:crypto';
import pg from 'pg';
import { inject } from 'vitest';
import { createPool, migrate, type Pool } from '../src/db.ts';
import type { EngineOptions } from '../src/engine/engine.ts';
import { humanApproval } from '../src/handlers/human-approval.ts';
import { onTaskDone } from '../src/policy.ts';
import type { TaskHandler, TaskRow } from '../src/types.ts';

export interface TestDb {
  pool: Pool;
  drop(): Promise<void>;
}

/** Creates an empty, migrated database so each test starts from nothing. */
export async function createTestDb(): Promise<TestDb> {
  const adminUrl = inject('adminUrl');
  const name = `factory_test_${randomBytes(6).toString('hex')}`;

  const admin = new pg.Client({ connectionString: adminUrl });
  await admin.connect();
  await admin.query(`create database ${name}`);
  await admin.end();

  const url = new URL(adminUrl);
  url.pathname = `/${name}`;
  const pool = createPool(url.toString());
  await migrate(pool);

  return {
    pool,
    async drop() {
      await pool.end();
      const cleanup = new pg.Client({ connectionString: adminUrl });
      await cleanup.connect();
      await cleanup.query(`drop database if exists ${name} with (force)`);
      await cleanup.end();
    },
  };
}

/** Engine settings for tests: retries happen at once, heartbeats are frequent. */
export const testOptions: EngineOptions = {
  leaseSeconds: 30,
  heartbeatMs: 500,
  backoffMs: () => 0,
  onTaskDone,
};

const done: TaskHandler = async () => ({ kind: 'done', output: { ok: true } });

/** Handlers where every agent and CI step succeeds immediately. */
export function stubHandlers(overrides: Record<string, TaskHandler> = {}): Record<string, TaskHandler> {
  return {
    triage: async () => ({
      kind: 'done',
      output: { classification: 'minor_task', summary: 'Small change' },
    }),
    implement: done,
    ci: done,
    review: done,
    verify: done,
    human_approval: humanApproval,
    ...overrides,
  };
}

export async function waitFor<T>(
  check: () => Promise<T | null | undefined | false>,
  description: string,
  timeoutMs = 20_000,
): Promise<T> {
  const deadline = Date.now() + timeoutMs;
  for (;;) {
    const value = await check();
    if (value) return value;
    if (Date.now() > deadline) throw new Error(`Timed out waiting for ${description}`);
    await new Promise((resolve) => setTimeout(resolve, 50));
  }
}

export async function requestStatus(pool: Pool, requestId: string): Promise<string> {
  const { rows } = await pool.query<{ status: string }>(
    'select status from work_request where id = $1',
    [requestId],
  );
  return rows[0]!.status;
}

export async function tasksByKey(pool: Pool, requestId: string): Promise<Record<string, TaskRow>> {
  const { rows } = await pool.query<TaskRow>('select * from task where request_id = $1', [requestId]);
  return Object.fromEntries(rows.map((row) => [row.key, row]));
}

export const sampleRequest = {
  source: 'api',
  requestor: 'tester',
  title: 'Fix the typo on the settings page',
  description: 'The word "recieve" should be "receive".',
  priority: 'normal' as const,
  acceptanceCriteria: ['The settings page shows "receive"'],
};
