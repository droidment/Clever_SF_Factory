import type { FastifyInstance } from 'fastify';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { buildServer } from '../src/api/server.ts';
import { runTask } from '../src/engine/worker.ts';
import {
  createTestDb,
  sampleRequest,
  stubHandlers,
  tasksByKey,
  testOptions,
  type TestDb,
} from './helpers.ts';

let db: TestDb;
let app: FastifyInstance;

beforeEach(async () => {
  db = await createTestDb();
  app = buildServer({ pool: db.pool, options: testOptions });
});

afterEach(async () => {
  await app.close();
  await db.drop();
});

const post = (url: string, payload: unknown, headers: Record<string, string> = {}) =>
  app.inject({ method: 'POST', url, payload: payload as object, headers });

describe('intake', () => {
  it('acknowledges a request with a tracking ID and queues triage', async () => {
    const response = await post('/requests', sampleRequest);
    expect(response.statusCode).toBe(201);
    const body = response.json();
    expect(body.trackingId).toBe('REQ-000001');
    expect(body.status).toBe('new');
    expect(body.duplicate).toBe(false);

    const tasks = await tasksByKey(db.pool, body.id);
    expect(tasks.triage!.status).toBe('ready');
  });

  it('returns the original request for a repeated idempotency key', async () => {
    const headers = { 'idempotency-key': 'teams-message-42' };
    const first = (await post('/requests', sampleRequest, headers)).json();
    const second = await post('/requests', { ...sampleRequest, title: 'Edited title' }, headers);

    expect(second.statusCode).toBe(200);
    expect(second.json().id).toBe(first.id);
    expect(second.json().duplicate).toBe(true);
    const count = await db.pool.query('select count(*) from work_request');
    expect(Number(count.rows[0].count)).toBe(1);
  });

  it('treats identical content with no key as the same request', async () => {
    const first = (await post('/requests', sampleRequest)).json();
    const second = (await post('/requests', sampleRequest)).json();
    expect(second.id).toBe(first.id);

    const different = await post('/requests', { ...sampleRequest, title: 'Another change' });
    expect(different.statusCode).toBe(201);
    // Tracking IDs come from a sequence, so a rejected duplicate can leave a gap.
    expect(different.json().trackingId).toMatch(/^REQ-\d{6}$/);
    expect(different.json().trackingId).not.toBe(first.trackingId);
  });

  it('rejects a request with missing fields', async () => {
    const response = await post('/requests', { title: 'No description' });
    expect(response.statusCode).toBe(400);
  });

  it('serves the intake form', async () => {
    const response = await app.inject({ method: 'GET', url: '/' });
    expect(response.statusCode).toBe(200);
    expect(response.headers['content-type']).toMatch(/text\/html/);
    expect(response.body).toContain('New software request');
  });
});

describe('reading requests and recording decisions', () => {
  it('lists requests and shows detail by tracking ID', async () => {
    const created = (await post('/requests', sampleRequest)).json();

    const list = (await app.inject({ method: 'GET', url: '/requests?status=new' })).json();
    expect(list.requests.map((r: { id: string }) => r.id)).toEqual([created.id]);

    const detail = await app.inject({ method: 'GET', url: `/requests/${created.trackingId}` });
    expect(detail.statusCode).toBe(200);
    const body = detail.json();
    expect(body.request.acceptanceCriteria).toEqual(sampleRequest.acceptanceCriteria);
    expect(body.tasks.map((t: { key: string }) => t.key)).toEqual(['triage']);
    expect(body.cost).toEqual({ spentUsd: 0, capUsd: 10 });
    expect(body.events.map((e: { type: string }) => e.type)).toEqual([
      'request.received',
      'plan.revision',
      'task.ready',
    ]);

    const missing = await app.inject({ method: 'GET', url: '/requests/REQ-999999' });
    expect(missing.statusCode).toBe(404);
  });

  it('completes a request when the approval is recorded through the API', async () => {
    const created = (await post('/requests', sampleRequest)).json();
    const deps = { pool: db.pool, options: testOptions, handlers: stubHandlers() };

    // Drive the plan by hand, in dependency order, instead of starting a worker.
    await runTask(deps, (await tasksByKey(db.pool, created.id)).triage!.id);
    for (const key of ['implement', 'ci', 'review', 'verify', 'merge_approval']) {
      await runTask(deps, (await tasksByKey(db.pool, created.id))[key]!.id);
    }
    const gate = (await tasksByKey(db.pool, created.id)).merge_approval!;
    expect(gate.status).toBe('waiting_human');

    const tooEarly = await post(`/tasks/${(await tasksByKey(db.pool, created.id)).ci!.id}/decision`, {
      decision: 'approved',
      decidedBy: 'owner',
    });
    expect(tooEarly.statusCode).toBe(409);

    const approved = await post(`/tasks/${gate.id}/decision`, {
      decision: 'approved',
      decidedBy: 'owner',
    });
    expect(approved.statusCode).toBe(200);
    expect(approved.json().status).toBe('done');

    const detail = (await app.inject({ method: 'GET', url: `/requests/${created.id}` })).json();
    expect(detail.request.status).toBe('completed');
    expect(detail.approvals).toHaveLength(1);
    expect(detail.approvals[0].decision).toBe('approved');
  });

  it('returns 404 for a decision on an unknown task', async () => {
    const response = await post('/tasks/00000000-0000-4000-8000-000000000000/decision', {
      decision: 'approved',
      decidedBy: 'owner',
    });
    expect(response.statusCode).toBe(404);
  });
});
