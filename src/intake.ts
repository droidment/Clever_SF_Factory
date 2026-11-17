import { createHash } from 'node:crypto';
import { z } from 'zod';
import { withTransaction, type Pool } from './db.ts';
import { emitEvent } from './engine/engine.ts';
import { createPlanRevision } from './engine/plan.ts';
import { intakePlan } from './policy.ts';
import type { WorkRequestRow } from './types.ts';

/** The common shape every intake channel normalizes into. */
export const WorkRequestInput = z.object({
  source: z.string().trim().min(1).max(50).default('api'),
  requestor: z.string().trim().min(1).max(200),
  title: z.string().trim().min(1).max(200),
  description: z.string().trim().min(1).max(20_000),
  repository: z.string().trim().min(1).max(200).optional(),
  priority: z.enum(['low', 'normal', 'high', 'urgent']).default('normal'),
  acceptanceCriteria: z.array(z.string().trim().min(1).max(1000)).max(50).default([]),
  /** The channel's own ID for this request (message ID, ticket key), used to spot duplicates. */
  externalId: z.string().trim().min(1).max(200).optional(),
});
export type WorkRequestInput = z.infer<typeof WorkRequestInput>;

function dedupeKey(input: WorkRequestInput, idempotencyKey?: string): string {
  if (idempotencyKey) return `key:${idempotencyKey}`;
  if (input.externalId) return `ext:${input.source}:${input.externalId}`;
  const hash = createHash('sha256')
    .update([input.source, input.requestor, input.title, input.description].join('\u0000'))
    .digest('hex');
  return `hash:${hash}`;
}

/**
 * Stores a request and queues its triage task in one transaction. A repeat of
 * a request already received returns the original with `duplicate: true`.
 */
export async function createWorkRequest(
  pool: Pool,
  input: WorkRequestInput,
  idempotencyKey?: string,
): Promise<{ request: WorkRequestRow; duplicate: boolean }> {
  const key = dedupeKey(input, idempotencyKey);
  return withTransaction(pool, async (client) => {
    const inserted = await client.query<WorkRequestRow>(
      `insert into work_request
         (source, requestor, title, description, repository, priority, acceptance_criteria, dedupe_key)
       values ($1, $2, $3, $4, $5, $6, $7, $8)
       on conflict (tenant_id, dedupe_key) do nothing
       returning *`,
      [
        input.source,
        input.requestor,
        input.title,
        input.description,
        input.repository ?? null,
        input.priority,
        JSON.stringify(input.acceptanceCriteria),
        key,
      ],
    );
    const request = inserted.rows[0];
    if (!request) {
      const existing = await client.query<WorkRequestRow>(
        `select * from work_request where tenant_id = 'default' and dedupe_key = $1`,
        [key],
      );
      return { request: existing.rows[0]!, duplicate: true };
    }

    await client.query(
      'insert into audit_log (actor, action, target, detail) values ($1, $2, $3, $4)',
      [
        input.requestor,
        'request.created',
        `request:${request.id}`,
        JSON.stringify({ source: input.source, trackingId: request.tracking_id }),
      ],
    );
    await emitEvent(client, request.id, null, 'request.received', { source: input.source });
    await createPlanRevision(client, {
      requestId: request.id,
      template: 'intake',
      nodes: intakePlan,
      createdBy: 'system',
      reason: 'Initial triage',
    });
    return { request, duplicate: false };
  });
}
