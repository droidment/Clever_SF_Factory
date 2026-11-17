import { withTransaction, type Client, type Pool } from '../db.ts';
import type {
  ApprovalKind,
  ArtifactInput,
  Decision,
  RequestStatus,
  TaskRow,
  WorkRequestRow,
} from '../types.ts';

/** Name of the job-queue task that runs one factory task. */
export const RUN_TASK_JOB = 'run_task';

/**
 * Called inside the transaction that marks a task done, before dependants are
 * released. Policy code uses it to add work (for example, a plan after triage).
 * Throwing rolls the completion back.
 */
export type TaskDoneHook = (client: Client, task: TaskRow, output: unknown) => Promise<void>;

export interface EngineOptions {
  /** How long a running task may go without a heartbeat before it is reclaimed. */
  leaseSeconds: number;
  /** How often a running task renews its lease. */
  heartbeatMs: number;
  /** Delay before retry number `attempt + 1`, in milliseconds. */
  backoffMs: (attempt: number) => number;
  onTaskDone?: TaskDoneHook;
}

export const defaultEngineOptions: EngineOptions = {
  leaseSeconds: 120,
  heartbeatMs: 30_000,
  backoffMs: (attempt) => Math.min(5 * 60_000, 5_000 * 2 ** (attempt - 1)),
};

export interface ClaimedTask {
  task: TaskRow;
  request: WorkRequestRow;
  dependencyOutputs: Record<string, unknown>;
}

export interface DecisionInput {
  decision: Decision;
  decidedBy: string;
  comment?: string;
}

export class DecisionError extends Error {
  override name = 'DecisionError';
}

const TASK_COLUMNS =
  'id, request_id, plan_revision_id, key, type, profile, status, attempt, max_attempts, input, output, error';

export async function emitEvent(
  client: Client,
  requestId: string,
  taskId: string | null,
  type: string,
  payload: Record<string, unknown> = {},
): Promise<void> {
  await client.query(
    'insert into event (request_id, task_id, type, payload) values ($1, $2, $3, $4)',
    [requestId, taskId, type, JSON.stringify(payload)],
  );
  await client.query(`select pg_notify('factory_events', $1::text)`, [requestId]);
}

/** Queues a task for a worker. Commits or rolls back with the caller's transaction. */
export async function enqueue(client: Client, taskId: string, runAt?: Date): Promise<void> {
  await client.query(
    `select graphile_worker.add_job(
       $1,
       json_build_object('taskId', $2::text),
       run_at := coalesce($3::timestamptz, now()),
       max_attempts := 5,
       job_key := $2::text
     )`,
    [RUN_TASK_JOB, taskId, runAt ?? null],
  );
}

/**
 * Serializes state changes within one request. Two tasks finishing at the same
 * moment each run in their own transaction and cannot see the other's result;
 * without this lock neither would release a task that depends on both, and
 * neither would see the request as complete. The second transaction waits
 * here, and its next statement then sees what the first one committed.
 *
 * Lock order is always task row first, then request row.
 *
 * Row locks in this file are NO KEY UPDATE, not UPDATE. Inserting an event or
 * approval takes a key-share lock on the rows it references, and a plain
 * FOR UPDATE conflicts with that: two transactions that had each logged an
 * event would deadlock here. NO KEY UPDATE still excludes other writers.
 */
async function lockRequest(client: Client, requestId: string): Promise<void> {
  await client.query('select 1 from work_request where id = $1 for no key update', [requestId]);
}

/** Marks every pending task whose dependencies are all done as ready, and queues it. */
export async function releaseUnblocked(client: Client, requestId: string): Promise<string[]> {
  await lockRequest(client, requestId);
  const { rows } = await client.query<{ id: string }>(
    `update task t
        set status = 'ready', updated_at = now()
      where t.request_id = $1
        and t.status = 'pending'
        and not exists (
          select 1
            from task_dependency d
            join task dep on dep.id = d.depends_on_task_id
           where d.task_id = t.id and dep.status <> 'done'
        )
      returning t.id`,
    [requestId],
  );
  for (const { id } of rows) {
    await enqueue(client, id);
    await emitEvent(client, requestId, id, 'task.ready');
  }
  return rows.map((r) => r.id);
}

/** Derives the request's status from its tasks. Call in the transaction that changed them. */
export async function recomputeRequestStatus(
  client: Client,
  requestId: string,
): Promise<RequestStatus> {
  await lockRequest(client, requestId);
  const { rows } = await client.query<{
    failed: string;
    waiting: string;
    open: string;
    total: string;
  }>(
    `select count(*) filter (where status = 'failed') as failed,
            count(*) filter (where status = 'waiting_human') as waiting,
            count(*) filter (where status <> 'done') as open,
            count(*) as total
       from task
      where request_id = $1`,
    [requestId],
  );
  const counts = rows[0]!;
  let status: RequestStatus = 'in_progress';
  if (Number(counts.failed) > 0) status = 'failed';
  else if (Number(counts.waiting) > 0) status = 'waiting_human';
  else if (Number(counts.total) > 0 && Number(counts.open) === 0) status = 'completed';

  const updated = await client.query(
    `update work_request set status = $2, updated_at = now()
      where id = $1 and status <> $2`,
    [requestId, status],
  );
  if (updated.rowCount) {
    await emitEvent(client, requestId, null, 'request.status', { status });
  }
  return status;
}

async function lockTask(client: Client, taskId: string): Promise<TaskRow | null> {
  const { rows } = await client.query<TaskRow>(
    `select ${TASK_COLUMNS} from task where id = $1 for no key update`,
    [taskId],
  );
  return rows[0] ?? null;
}

async function openApproval(
  client: Client,
  task: TaskRow,
  kind: ApprovalKind,
  prompt: string,
  requestedFrom?: string,
): Promise<void> {
  await client.query(
    `update task
        set status = 'waiting_human', heartbeat_at = null, lease_until = null, updated_at = now()
      where id = $1`,
    [task.id],
  );
  await client.query(
    'insert into approval (task_id, kind, prompt, requested_from) values ($1, $2, $3, $4)',
    [task.id, kind, prompt, requestedFrom ?? null],
  );
  await emitEvent(client, task.request_id, task.id, 'task.waiting_human', { kind, prompt });
}

/**
 * Moves a ready task to running for one worker. Returns null when the task is
 * not ready (already claimed, finished, or paused on the cost cap).
 */
export async function claimTask(
  pool: Pool,
  options: EngineOptions,
  taskId: string,
): Promise<ClaimedTask | null> {
  return withTransaction(pool, async (client) => {
    const task = await lockTask(client, taskId);
    if (!task || task.status !== 'ready') return null;

    const requestResult = await client.query<WorkRequestRow>(
      'select * from work_request where id = $1',
      [task.request_id],
    );
    const request = requestResult.rows[0]!;

    if (task.profile) {
      const spentResult = await client.query<{ spent: string }>(
        `select coalesce(sum(ar.cost_usd), 0) as spent
           from agent_run ar
           join task t on t.id = ar.task_id
          where t.request_id = $1`,
        [task.request_id],
      );
      const spent = Number(spentResult.rows[0]!.spent);
      const cap = Number(request.cost_cap_usd);
      if (spent >= cap) {
        await openApproval(
          client,
          task,
          'budget',
          `Cost cap reached: $${spent.toFixed(2)} spent of $${cap.toFixed(2)}. Approving doubles the cap.`,
        );
        await recomputeRequestStatus(client, task.request_id);
        return null;
      }
    }

    const claimed = await client.query<TaskRow>(
      `update task
          set status = 'running',
              attempt = attempt + 1,
              started_at = coalesce(started_at, now()),
              heartbeat_at = now(),
              lease_until = now() + make_interval(secs => $2),
              error = null,
              updated_at = now()
        where id = $1
        returning ${TASK_COLUMNS}`,
      [taskId, options.leaseSeconds],
    );
    const running = claimed.rows[0]!;

    const deps = await client.query<{ key: string; output: unknown }>(
      `select dep.key, dep.output
         from task_dependency d
         join task dep on dep.id = d.depends_on_task_id
        where d.task_id = $1`,
      [taskId],
    );
    const dependencyOutputs: Record<string, unknown> = {};
    for (const dep of deps.rows) dependencyOutputs[dep.key] = dep.output;

    await emitEvent(client, task.request_id, taskId, 'task.started', { attempt: running.attempt });
    await recomputeRequestStatus(client, task.request_id);
    return { task: running, request, dependencyOutputs };
  });
}

/** Renews the lease. Returns false when this attempt no longer owns the task. */
export async function heartbeat(
  pool: Pool,
  options: EngineOptions,
  taskId: string,
  attempt: number,
): Promise<boolean> {
  const result = await pool.query(
    `update task
        set heartbeat_at = now(), lease_until = now() + make_interval(secs => $3)
      where id = $1 and attempt = $2 and status = 'running'`,
    [taskId, attempt, options.leaseSeconds],
  );
  return (result.rowCount ?? 0) > 0;
}

/** Locks the task and returns it only if this attempt still owns it. */
async function lockOwnedTask(
  client: Client,
  taskId: string,
  attempt: number,
): Promise<TaskRow | null> {
  const task = await lockTask(client, taskId);
  if (!task || task.status !== 'running' || task.attempt !== attempt) return null;
  return task;
}

async function finishTask(
  client: Client,
  options: EngineOptions,
  task: TaskRow,
  output: unknown,
  artifacts: ArtifactInput[],
): Promise<void> {
  await client.query(
    `update task
        set status = 'done', output = $2, error = null, completed_at = now(),
            heartbeat_at = null, lease_until = null, updated_at = now()
      where id = $1`,
    [task.id, JSON.stringify(output ?? null)],
  );
  for (const artifact of artifacts) {
    await client.query(
      `insert into artifact (task_id, type, schema_version, content, location)
       values ($1, $2, $3, $4, $5)`,
      [
        task.id,
        artifact.type,
        artifact.schemaVersion ?? 1,
        artifact.content === undefined ? null : JSON.stringify(artifact.content),
        artifact.location ?? null,
      ],
    );
  }
  await emitEvent(client, task.request_id, task.id, 'task.done');
  if (options.onTaskDone) {
    await options.onTaskDone(client, { ...task, status: 'done', output }, output);
  }
  await releaseUnblocked(client, task.request_id);
  await recomputeRequestStatus(client, task.request_id);
}

/**
 * Records a task's result. The status change, output, artifacts, event and the
 * release of dependent tasks commit together. Returns false when this attempt
 * no longer owns the task (its lease expired and the task was reclaimed).
 */
export async function completeTask(
  pool: Pool,
  options: EngineOptions,
  taskId: string,
  attempt: number,
  result: { output?: unknown; artifacts?: ArtifactInput[] } = {},
): Promise<boolean> {
  return withTransaction(pool, async (client) => {
    const task = await lockOwnedTask(client, taskId, attempt);
    if (!task) return false;
    await finishTask(client, options, task, result.output, result.artifacts ?? []);
    return true;
  });
}

async function failOrRetry(
  client: Client,
  options: EngineOptions,
  task: TaskRow,
  error: string,
  retryable: boolean,
  eventType: string,
): Promise<void> {
  if (retryable && task.attempt < task.max_attempts) {
    const runAt = new Date(Date.now() + options.backoffMs(task.attempt));
    await client.query(
      `update task
          set status = 'ready', error = $2, heartbeat_at = null, lease_until = null,
              updated_at = now()
        where id = $1`,
      [task.id, error],
    );
    await enqueue(client, task.id, runAt);
    await emitEvent(client, task.request_id, task.id, eventType, {
      error,
      attempt: task.attempt,
      retryAt: runAt.toISOString(),
    });
  } else {
    await client.query(
      `update task
          set status = 'failed', error = $2, completed_at = now(),
              heartbeat_at = null, lease_until = null, updated_at = now()
        where id = $1`,
      [task.id, error],
    );
    await emitEvent(client, task.request_id, task.id, 'task.failed', {
      error,
      attempt: task.attempt,
    });
  }
  await recomputeRequestStatus(client, task.request_id);
}

/** Records a failed attempt: schedules a retry with backoff, or fails the task. */
export async function failAttempt(
  pool: Pool,
  options: EngineOptions,
  taskId: string,
  attempt: number,
  error: string,
  retryable = true,
): Promise<boolean> {
  return withTransaction(pool, async (client) => {
    const task = await lockOwnedTask(client, taskId, attempt);
    if (!task) return false;
    await failOrRetry(client, options, task, error, retryable, 'task.retry');
    return true;
  });
}

/** Parks a running task until a person records a decision. */
export async function waitForHuman(
  pool: Pool,
  taskId: string,
  attempt: number,
  ask: { kind: ApprovalKind; prompt: string; requestedFrom?: string },
): Promise<boolean> {
  return withTransaction(pool, async (client) => {
    const task = await lockOwnedTask(client, taskId, attempt);
    if (!task) return false;
    await openApproval(client, task, ask.kind, ask.prompt, ask.requestedFrom);
    await recomputeRequestStatus(client, task.request_id);
    return true;
  });
}

/**
 * Applies a person's decision to a waiting task.
 * - approval: approved completes the task; rejected fails it.
 * - clarification: the answer is added to the task's input and the task runs again.
 * - budget: approved doubles the request's cost cap and the task runs again.
 */
export async function recordDecision(
  pool: Pool,
  options: EngineOptions,
  taskId: string,
  input: DecisionInput,
): Promise<TaskRow> {
  return withTransaction(pool, async (client) => {
    const task = await lockTask(client, taskId);
    if (!task) throw new DecisionError('Task not found');
    if (task.status !== 'waiting_human') {
      throw new DecisionError(`Task is ${task.status}, not waiting for a decision`);
    }
    const open = await client.query<{ id: string; kind: ApprovalKind }>(
      `select id, kind from approval
        where task_id = $1 and decision is null
        order by created_at desc
        limit 1
        for no key update`,
      [taskId],
    );
    const approval = open.rows[0];
    if (!approval) throw new DecisionError('No open approval for this task');

    const isClarification = approval.kind === 'clarification';
    if (isClarification && input.decision !== 'answered') {
      throw new DecisionError('A clarification takes the decision "answered"');
    }
    if (!isClarification && input.decision === 'answered') {
      throw new DecisionError('An approval takes the decision "approved" or "rejected"');
    }
    if (isClarification && !input.comment?.trim()) {
      throw new DecisionError('A clarification needs the answer in "comment"');
    }

    await client.query(
      `update approval
          set decision = $2, decided_by = $3, decided_at = now(), comment = $4
        where id = $1`,
      [approval.id, input.decision, input.decidedBy, input.comment ?? null],
    );
    await client.query(
      'insert into audit_log (actor, action, target, detail) values ($1, $2, $3, $4)',
      [
        input.decidedBy,
        `decision.${input.decision}`,
        `task:${taskId}`,
        JSON.stringify({ kind: approval.kind, comment: input.comment ?? null }),
      ],
    );
    await emitEvent(client, task.request_id, taskId, 'task.decision', {
      kind: approval.kind,
      decision: input.decision,
      decidedBy: input.decidedBy,
    });

    if (input.decision === 'rejected') {
      await failOrRetry(
        client,
        options,
        task,
        `Rejected by ${input.decidedBy}${input.comment ? `: ${input.comment}` : ''}`,
        false,
        'task.retry',
      );
    } else if (approval.kind === 'approval') {
      await finishTask(
        client,
        options,
        task,
        { decision: 'approved', decidedBy: input.decidedBy, comment: input.comment ?? null },
        [],
      );
    } else {
      if (approval.kind === 'budget') {
        await client.query(
          'update work_request set cost_cap_usd = cost_cap_usd * 2, updated_at = now() where id = $1',
          [task.request_id],
        );
      }
      // Run the task again. A person's answer does not use up an attempt.
      await client.query(
        `update task
            set status = 'ready',
                attempt = 0,
                input = case when $2::jsonb is null then input
                             else jsonb_set(
                               input,
                               '{clarifications}',
                               coalesce(input -> 'clarifications', '[]'::jsonb) || $2::jsonb
                             )
                        end,
                updated_at = now()
          where id = $1`,
        [
          taskId,
          isClarification
            ? JSON.stringify([{ answer: input.comment, answeredBy: input.decidedBy }])
            : null,
        ],
      );
      await enqueue(client, taskId);
      await emitEvent(client, task.request_id, taskId, 'task.ready');
      await recomputeRequestStatus(client, task.request_id);
    }

    const refreshed = await client.query<TaskRow>(
      `select ${TASK_COLUMNS} from task where id = $1`,
      [taskId],
    );
    return refreshed.rows[0]!;
  });
}

/**
 * Reclaims running tasks whose lease has expired (the worker died or hung).
 * Each goes back to ready, or to failed when its attempts are used up.
 */
export async function sweepExpiredLeases(pool: Pool, options: EngineOptions): Promise<number> {
  return withTransaction(pool, async (client) => {
    const { rows } = await client.query<TaskRow>(
      `select ${TASK_COLUMNS}
         from task
        where status = 'running' and lease_until < now()
        for no key update skip locked`,
    );
    for (const task of rows) {
      await failOrRetry(client, options, task, 'Lease expired: no heartbeat', true, 'task.lease_expired');
    }
    return rows.length;
  });
}

/** Records one agent attempt and its cost. The cost cap is checked against these rows. */
export async function recordAgentRun(
  pool: Pool,
  run: {
    taskId: string;
    profile: string;
    model: string;
    promptVersion: string;
    tokensIn: number;
    tokensOut: number;
    costUsd: number;
    durationMs: number;
    status: 'succeeded' | 'failed';
    error?: string;
  },
): Promise<void> {
  await pool.query(
    `insert into agent_run
       (task_id, profile, model, prompt_version, tokens_in, tokens_out, cost_usd, duration_ms, status, error)
     values ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)`,
    [
      run.taskId,
      run.profile,
      run.model,
      run.promptVersion,
      run.tokensIn,
      run.tokensOut,
      run.costUsd,
      run.durationMs,
      run.status,
      run.error ?? null,
    ],
  );
}
