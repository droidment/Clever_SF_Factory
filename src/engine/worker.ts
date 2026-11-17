import { Logger, run, type Runner } from 'graphile-worker';
import type { Pool } from '../db.ts';
import { PermanentError, type TaskHandler } from '../types.ts';
import {
  RUN_TASK_JOB,
  claimTask,
  completeTask,
  failAttempt,
  heartbeat,
  sweepExpiredLeases,
  waitForHuman,
  type EngineOptions,
} from './engine.ts';

export interface WorkerDeps {
  pool: Pool;
  options: EngineOptions;
  /** Handlers by task type. A task whose type has no handler fails at once. */
  handlers: Record<string, TaskHandler>;
}

/** Claims one task, runs its handler, and records the outcome. */
export async function runTask(deps: WorkerDeps, taskId: string): Promise<void> {
  const { pool, options, handlers } = deps;
  const claimed = await claimTask(pool, options, taskId);
  if (!claimed) return;
  const { task } = claimed;

  const handler = handlers[task.type];
  if (!handler) {
    await failAttempt(
      pool,
      options,
      task.id,
      task.attempt,
      `No handler registered for task type "${task.type}"`,
      false,
    );
    return;
  }

  const controller = new AbortController();
  const beat = setInterval(() => {
    heartbeat(pool, options, task.id, task.attempt)
      .then((owned) => {
        if (!owned) controller.abort();
      })
      .catch(() => {
        // A missed heartbeat is not fatal; the next one may succeed.
      });
  }, options.heartbeatMs);

  try {
    const result = await handler({ ...claimed, pool, signal: controller.signal });
    if (result.kind === 'wait_human') {
      await waitForHuman(pool, task.id, task.attempt, {
        kind: result.approvalKind,
        prompt: result.prompt,
        requestedFrom: result.requestedFrom,
      });
    } else {
      await completeTask(pool, options, task.id, task.attempt, result);
    }
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    await failAttempt(pool, options, task.id, task.attempt, message, !(err instanceof PermanentError));
  } finally {
    clearInterval(beat);
  }
}

export interface WorkerHandle {
  stop(): Promise<void>;
}

/** Starts the job-queue runner and the periodic lease sweep. */
export async function startWorker(
  deps: WorkerDeps & {
    concurrency?: number;
    sweepIntervalMs?: number;
    pollInterval?: number;
    quiet?: boolean;
  },
): Promise<WorkerHandle> {
  const runner: Runner = await run({
    pgPool: deps.pool,
    concurrency: deps.concurrency ?? 4,
    pollInterval: deps.pollInterval ?? 2000,
    noHandleSignals: true,
    ...(deps.quiet ? { logger: new Logger(() => () => {}) } : {}),
    taskList: {
      [RUN_TASK_JOB]: async (payload) => {
        const { taskId } = payload as { taskId: string };
        await runTask(deps, taskId);
      },
    },
  });

  const sweep = setInterval(() => {
    sweepExpiredLeases(deps.pool, deps.options).catch((err) => {
      if (!deps.quiet) console.error('Lease sweep failed', err);
    });
  }, deps.sweepIntervalMs ?? 15_000);

  return {
    async stop() {
      clearInterval(sweep);
      await runner.stop();
    },
  };
}
