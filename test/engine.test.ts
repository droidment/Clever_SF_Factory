import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import {
  DecisionError,
  claimTask,
  completeTask,
  recordAgentRun,
  recordDecision,
  sweepExpiredLeases,
} from '../src/engine/engine.ts';
import { startWorker, type WorkerHandle } from '../src/engine/worker.ts';
import { createWorkRequest } from '../src/intake.ts';
import { PermanentError, type TaskHandler } from '../src/types.ts';
import {
  createTestDb,
  requestStatus,
  sampleRequest,
  stubHandlers,
  tasksByKey,
  testOptions,
  waitFor,
  type TestDb,
} from './helpers.ts';

let db: TestDb;
let worker: WorkerHandle | undefined;

beforeEach(async () => {
  db = await createTestDb();
});

afterEach(async () => {
  await worker?.stop();
  worker = undefined;
  await db.drop();
});

async function start(handlers: Record<string, TaskHandler>): Promise<void> {
  worker = await startWorker({
    pool: db.pool,
    options: testOptions,
    handlers,
    pollInterval: 100,
    sweepIntervalMs: 200,
    quiet: true,
  });
}

async function submit(): Promise<string> {
  const { request } = await createWorkRequest(db.pool, sampleRequest);
  return request.id;
}

describe('running a request with a worker', () => {
  it('goes from intake to the approval gate, then completes when approved', async () => {
    await start(stubHandlers());
    const requestId = await submit();

    await waitFor(
      async () => (await requestStatus(db.pool, requestId)) === 'waiting_human',
      'the request to reach the approval gate',
    );
    const waiting = await tasksByKey(db.pool, requestId);
    expect(Object.keys(waiting).sort()).toEqual(
      ['ci', 'implement', 'merge_approval', 'review', 'triage', 'verify'].sort(),
    );
    for (const key of ['triage', 'implement', 'ci', 'review', 'verify']) {
      expect(waiting[key]!.status).toBe('done');
    }
    expect(waiting.merge_approval!.status).toBe('waiting_human');

    await recordDecision(db.pool, testOptions, waiting.merge_approval!.id, {
      decision: 'approved',
      decidedBy: 'owner',
    });
    expect(await requestStatus(db.pool, requestId)).toBe('completed');

    const revisions = await db.pool.query(
      'select template from plan_revision where request_id = $1 order by revision_no',
      [requestId],
    );
    expect(revisions.rows.map((r) => r.template)).toEqual(['intake', 'minor_task']);
  });

  it('retries a task that fails once', async () => {
    let calls = 0;
    await start(
      stubHandlers({
        implement: async () => {
          calls += 1;
          if (calls === 1) throw new Error('model API unavailable');
          return { kind: 'done', output: { ok: true } };
        },
      }),
    );
    const requestId = await submit();

    await waitFor(
      async () => (await requestStatus(db.pool, requestId)) === 'waiting_human',
      'the request to reach the approval gate',
    );
    const tasks = await tasksByKey(db.pool, requestId);
    expect(tasks.implement!.status).toBe('done');
    expect(tasks.implement!.attempt).toBe(2);
  });

  it('fails the request when a task uses up its attempts', async () => {
    await start(
      stubHandlers({
        ci: async () => {
          throw new Error('tests failed');
        },
      }),
    );
    const requestId = await submit();

    await waitFor(
      async () => (await requestStatus(db.pool, requestId)) === 'failed',
      'the request to fail',
    );
    const tasks = await tasksByKey(db.pool, requestId);
    expect(tasks.ci!.status).toBe('failed');
    expect(tasks.ci!.attempt).toBe(3);
    expect(tasks.ci!.error).toBe('tests failed');
    expect(tasks.review!.status).toBe('pending');
  });

  it('does not retry a permanent error', async () => {
    await start(
      stubHandlers({
        implement: async () => {
          throw new PermanentError('repository not found');
        },
      }),
    );
    const requestId = await submit();

    await waitFor(
      async () => (await requestStatus(db.pool, requestId)) === 'failed',
      'the request to fail',
    );
    const tasks = await tasksByKey(db.pool, requestId);
    expect(tasks.implement!.attempt).toBe(1);
  });

  it('fails a task whose type has no handler', async () => {
    await start({});
    const requestId = await submit();

    await waitFor(
      async () => (await requestStatus(db.pool, requestId)) === 'failed',
      'the request to fail',
    );
    const tasks = await tasksByKey(db.pool, requestId);
    expect(tasks.triage!.error).toMatch(/No handler registered/);
  });

  it('asks the requestor, then runs triage again with the answer', async () => {
    const seen: unknown[] = [];
    await start(
      stubHandlers({
        triage: async ({ task }) => {
          seen.push(task.input.clarifications);
          if (!task.input.clarifications) {
            return { kind: 'wait_human', approvalKind: 'clarification', prompt: 'Which page?' };
          }
          return { kind: 'done', output: { classification: 'bug_fix', summary: 'Typo fix' } };
        },
      }),
    );
    const requestId = await submit();

    await waitFor(
      async () => (await requestStatus(db.pool, requestId)) === 'waiting_human',
      'triage to ask its question',
    );
    const before = await tasksByKey(db.pool, requestId);
    expect(Object.keys(before)).toEqual(['triage']);

    await expect(
      recordDecision(db.pool, testOptions, before.triage!.id, {
        decision: 'approved',
        decidedBy: 'tester',
      }),
    ).rejects.toThrow(DecisionError);

    await recordDecision(db.pool, testOptions, before.triage!.id, {
      decision: 'answered',
      decidedBy: 'tester',
      comment: 'The settings page',
    });

    const after = await waitFor(async () => {
      const tasks = await tasksByKey(db.pool, requestId);
      return tasks.merge_approval?.status === 'waiting_human' ? tasks : null;
    }, 'the plan to reach the approval gate');
    expect(after.triage!.status).toBe('done');
    expect(seen[1]).toEqual([{ answer: 'The settings page', answeredBy: 'tester' }]);
  });

  it('fails the request when the approval is rejected', async () => {
    await start(stubHandlers());
    const requestId = await submit();

    const tasks = await waitFor(async () => {
      const all = await tasksByKey(db.pool, requestId);
      return all.merge_approval?.status === 'waiting_human' ? all : null;
    }, 'the approval gate');
    await recordDecision(db.pool, testOptions, tasks.merge_approval!.id, {
      decision: 'rejected',
      decidedBy: 'owner',
      comment: 'Wrong page',
    });

    expect(await requestStatus(db.pool, requestId)).toBe('failed');
    const audit = await db.pool.query(`select actor, action from audit_log where action like 'decision.%'`);
    expect(audit.rows).toEqual([{ actor: 'owner', action: 'decision.rejected' }]);
  });
});

describe('crash safety without a worker', () => {
  it('reclaims a task whose lease expired and refuses the stale attempt', async () => {
    const requestId = await submit();
    const triageId = (await tasksByKey(db.pool, requestId)).triage!.id;

    const first = await claimTask(db.pool, testOptions, triageId);
    expect(first?.task.attempt).toBe(1);
    expect(await claimTask(db.pool, testOptions, triageId)).toBeNull();

    // The worker dies: no more heartbeats, and the lease runs out.
    await db.pool.query(`update task set lease_until = now() - interval '1 second' where id = $1`, [
      triageId,
    ]);
    expect(await sweepExpiredLeases(db.pool, testOptions)).toBe(1);
    expect((await tasksByKey(db.pool, requestId)).triage!.status).toBe('ready');

    const second = await claimTask(db.pool, testOptions, triageId);
    expect(second?.task.attempt).toBe(2);

    const output = { classification: 'minor_task', summary: 'Small change' };
    // The dead worker's attempt comes back late and must not win.
    expect(await completeTask(db.pool, testOptions, triageId, 1, { output })).toBe(false);
    expect(await completeTask(db.pool, testOptions, triageId, 2, { output })).toBe(true);

    const tasks = await tasksByKey(db.pool, requestId);
    expect(tasks.triage!.status).toBe('done');
    expect(tasks.implement!.status).toBe('ready');
    expect(tasks.ci!.status).toBe('pending');
  });

  it('fails a task whose lease expires on its last attempt', async () => {
    const requestId = await submit();
    const triageId = (await tasksByKey(db.pool, requestId)).triage!.id;
    await db.pool.query('update task set max_attempts = 1 where id = $1', [triageId]);

    await claimTask(db.pool, testOptions, triageId);
    await db.pool.query(`update task set lease_until = now() - interval '1 second' where id = $1`, [
      triageId,
    ]);
    await sweepExpiredLeases(db.pool, testOptions);

    expect((await tasksByKey(db.pool, requestId)).triage!.status).toBe('failed');
    expect(await requestStatus(db.pool, requestId)).toBe('failed');
  });

  it('rolls the completion back when the plan cannot be created', async () => {
    const requestId = await submit();
    const triageId = (await tasksByKey(db.pool, requestId)).triage!.id;
    await claimTask(db.pool, testOptions, triageId);

    await expect(
      completeTask(db.pool, testOptions, triageId, 1, { output: { classification: 'epic' } }),
    ).rejects.toThrow(PermanentError);

    const tasks = await tasksByKey(db.pool, requestId);
    expect(Object.keys(tasks)).toEqual(['triage']);
    expect(tasks.triage!.status).toBe('running');
    expect(tasks.triage!.output).toBeNull();
  });

  it('queues a released task in the same transaction that completes its dependency', async () => {
    const requestId = await submit();
    const triageId = (await tasksByKey(db.pool, requestId)).triage!.id;
    await claimTask(db.pool, testOptions, triageId);
    await completeTask(db.pool, testOptions, triageId, 1, {
      output: { classification: 'minor_task', summary: 'Small change' },
    });

    const implementId = (await tasksByKey(db.pool, requestId)).implement!.id;
    const jobs = await db.pool.query(
      `select key from graphile_worker.jobs where key = $1`,
      [implementId],
    );
    expect(jobs.rowCount).toBe(1);
  });

  it('releases a task whose two dependencies finish at the same moment', async () => {
    // Two completions racing in separate transactions cannot see each other's
    // result. The race is timing-dependent, so several requests are tried.
    for (let round = 0; round < 6; round++) {
      const { request } = await createWorkRequest(db.pool, {
        ...sampleRequest,
        title: `${sampleRequest.title} (${round})`,
      });
      const claim = async (key: string) => {
        const id = (await tasksByKey(db.pool, request.id))[key]!.id;
        const claimed = await claimTask(db.pool, testOptions, id);
        return { id, attempt: claimed!.task.attempt };
      };
      const finish = (t: { id: string; attempt: number }, output: unknown = { ok: true }) =>
        completeTask(db.pool, testOptions, t.id, t.attempt, { output });

      // The bug-fix plan has review and verify both waiting on ci, and the gate on both.
      await finish(await claim('triage'), { classification: 'bug_fix', summary: 'Typo fix' });
      await finish(await claim('implement'));
      await finish(await claim('ci'));
      const review = await claim('review');
      const verify = await claim('verify');
      await Promise.all([finish(review), finish(verify)]);

      const tasks = await tasksByKey(db.pool, request.id);
      expect(tasks.review!.status).toBe('done');
      expect(tasks.verify!.status).toBe('done');
      expect(tasks.merge_approval!.status, `round ${round}`).toBe('ready');
    }
  });

  it('pauses an agent task at the cost cap until a person approves more', async () => {
    const requestId = await submit();
    const triageId = (await tasksByKey(db.pool, requestId)).triage!.id;
    await recordAgentRun(db.pool, {
      taskId: triageId,
      profile: 'triage_planner',
      model: 'test-model',
      promptVersion: 'v1',
      tokensIn: 1000,
      tokensOut: 1000,
      costUsd: 10,
      durationMs: 1000,
      status: 'succeeded',
    });

    expect(await claimTask(db.pool, testOptions, triageId)).toBeNull();
    expect((await tasksByKey(db.pool, requestId)).triage!.status).toBe('waiting_human');
    expect(await requestStatus(db.pool, requestId)).toBe('waiting_human');

    await recordDecision(db.pool, testOptions, triageId, { decision: 'approved', decidedBy: 'owner' });
    const cap = await db.pool.query('select cost_cap_usd from work_request where id = $1', [requestId]);
    expect(Number(cap.rows[0].cost_cap_usd)).toBe(20);
    expect(await claimTask(db.pool, testOptions, triageId)).not.toBeNull();
  });
});
