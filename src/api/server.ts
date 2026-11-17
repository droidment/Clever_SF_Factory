import Fastify, { type FastifyInstance } from 'fastify';
import { z } from 'zod';
import type { Pool } from '../db.ts';
import { DecisionError, recordDecision, type EngineOptions } from '../engine/engine.ts';
import { WorkRequestInput, createWorkRequest } from '../intake.ts';
import type { WorkRequestRow } from '../types.ts';
import { intakeFormHtml } from './form.ts';

const DecisionBody = z.object({
  decision: z.enum(['approved', 'rejected', 'answered']),
  decidedBy: z.string().trim().min(1).max(200),
  comment: z.string().trim().max(5000).optional(),
});

const ListQuery = z.object({
  status: z.enum(['new', 'in_progress', 'waiting_human', 'failed', 'completed']).optional(),
  limit: z.coerce.number().int().min(1).max(200).default(50),
});

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

function summary(request: WorkRequestRow) {
  return {
    id: request.id,
    trackingId: request.tracking_id,
    status: request.status,
    title: request.title,
    requestor: request.requestor,
    source: request.source,
    priority: request.priority,
    createdAt: request.created_at,
    updatedAt: request.updated_at,
  };
}

export function buildServer(deps: { pool: Pool; options: EngineOptions }): FastifyInstance {
  const { pool, options } = deps;
  const app = Fastify({ logger: false });

  app.get('/healthz', async () => {
    await pool.query('select 1');
    return { ok: true };
  });

  app.get('/', async (_req, reply) => {
    reply.type('text/html; charset=utf-8').send(intakeFormHtml);
  });

  // Intake: stores the request and returns a tracking ID at once.
  app.post('/requests', async (req, reply) => {
    const parsed = WorkRequestInput.safeParse(req.body);
    if (!parsed.success) {
      return reply.code(400).send({ error: 'Invalid request', issues: parsed.error.issues });
    }
    const header = req.headers['idempotency-key'];
    const idempotencyKey = typeof header === 'string' && header.trim() ? header.trim() : undefined;
    const { request, duplicate } = await createWorkRequest(pool, parsed.data, idempotencyKey);
    return reply.code(duplicate ? 200 : 201).send({ ...summary(request), duplicate });
  });

  app.get('/requests', async (req, reply) => {
    const parsed = ListQuery.safeParse(req.query);
    if (!parsed.success) {
      return reply.code(400).send({ error: 'Invalid query', issues: parsed.error.issues });
    }
    const { rows } = await pool.query<WorkRequestRow>(
      `select * from work_request
        where ($1::text is null or status = $1)
        order by created_at desc
        limit $2`,
      [parsed.data.status ?? null, parsed.data.limit],
    );
    return { requests: rows.map(summary) };
  });

  // Request detail by ID or tracking ID: the plan, its tasks, the timeline and cost.
  app.get<{ Params: { id: string } }>('/requests/:id', async (req, reply) => {
    const { id } = req.params;
    const found = UUID.test(id)
      ? await pool.query<WorkRequestRow>('select * from work_request where id = $1', [id])
      : await pool.query<WorkRequestRow>('select * from work_request where tracking_id = $1', [id]);
    const request = found.rows[0];
    if (!request) return reply.code(404).send({ error: 'Request not found' });

    const [tasks, dependencies, events, approvals, cost] = await Promise.all([
      pool.query(
        `select t.id, t.key, t.type, t.profile, t.status, t.attempt, t.max_attempts, t.error,
                t.output, t.started_at, t.completed_at, r.revision_no
           from task t
           join plan_revision r on r.id = t.plan_revision_id
          where t.request_id = $1
          order by r.revision_no, t.created_at, t.key`,
        [request.id],
      ),
      pool.query(
        `select d.task_id, d.depends_on_task_id
           from task_dependency d
           join task t on t.id = d.task_id
          where t.request_id = $1`,
        [request.id],
      ),
      pool.query(
        'select id, task_id, type, payload, created_at from event where request_id = $1 order by id',
        [request.id],
      ),
      pool.query(
        `select a.id, a.task_id, a.kind, a.prompt, a.decision, a.decided_by, a.decided_at, a.comment
           from approval a
           join task t on t.id = a.task_id
          where t.request_id = $1
          order by a.created_at`,
        [request.id],
      ),
      pool.query<{ spent: string }>(
        `select coalesce(sum(ar.cost_usd), 0) as spent
           from agent_run ar
           join task t on t.id = ar.task_id
          where t.request_id = $1`,
        [request.id],
      ),
    ]);

    return {
      request: {
        ...summary(request),
        description: request.description,
        repository: request.repository,
        acceptanceCriteria: request.acceptance_criteria,
      },
      cost: { spentUsd: Number(cost.rows[0]!.spent), capUsd: Number(request.cost_cap_usd) },
      tasks: tasks.rows,
      dependencies: dependencies.rows,
      approvals: approvals.rows,
      events: events.rows,
    };
  });

  // A person's decision on a task that is waiting for one.
  app.post<{ Params: { id: string } }>('/tasks/:id/decision', async (req, reply) => {
    if (!UUID.test(req.params.id)) return reply.code(404).send({ error: 'Task not found' });
    const parsed = DecisionBody.safeParse(req.body);
    if (!parsed.success) {
      return reply.code(400).send({ error: 'Invalid decision', issues: parsed.error.issues });
    }
    try {
      const task = await recordDecision(pool, options, req.params.id, parsed.data);
      return { taskId: task.id, status: task.status };
    } catch (err) {
      if (err instanceof DecisionError) {
        const notFound = err.message === 'Task not found';
        return reply.code(notFound ? 404 : 409).send({ error: err.message });
      }
      throw err;
    }
  });

  return app;
}
