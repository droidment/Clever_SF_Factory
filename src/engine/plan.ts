import type { Client } from '../db.ts';
import { PermanentError } from '../types.ts';
import { emitEvent, releaseUnblocked } from './engine.ts';

export interface PlanNode {
  /** Unique within the plan revision. */
  key: string;
  /** Selects the handler that runs the task. */
  type: string;
  /** Agent capability profile; absent for deterministic and human tasks. */
  profile?: string;
  dependsOn: string[];
  input?: Record<string, unknown>;
  maxAttempts?: number;
}

/** Rejects plans that are malformed, cyclic, or missing a human approval gate. */
export function validatePlan(nodes: PlanNode[], options: { requireApproval: boolean }): void {
  if (nodes.length === 0) throw new PermanentError('Plan has no tasks');

  const byKey = new Map<string, PlanNode>();
  for (const node of nodes) {
    if (byKey.has(node.key)) throw new PermanentError(`Plan has duplicate task key "${node.key}"`);
    byKey.set(node.key, node);
  }
  for (const node of nodes) {
    for (const dep of node.dependsOn) {
      if (!byKey.has(dep)) {
        throw new PermanentError(`Task "${node.key}" depends on unknown task "${dep}"`);
      }
    }
  }

  // Depth-first search: a node seen again while still on the stack is a cycle.
  const state = new Map<string, 'visiting' | 'done'>();
  const visit = (key: string): void => {
    const seen = state.get(key);
    if (seen === 'done') return;
    if (seen === 'visiting') throw new PermanentError(`Plan has a dependency cycle at "${key}"`);
    state.set(key, 'visiting');
    for (const dep of byKey.get(key)!.dependsOn) visit(dep);
    state.set(key, 'done');
  };
  for (const node of nodes) visit(node.key);

  if (options.requireApproval) {
    const gates = nodes.filter((n) => n.type === 'human_approval');
    if (gates.length === 0) throw new PermanentError('Plan has no human approval gate');

    // The last gate must come after every other task, so nothing merges unreviewed.
    const ancestors = (key: string, acc: Set<string>): Set<string> => {
      for (const dep of byKey.get(key)!.dependsOn) {
        if (!acc.has(dep)) {
          acc.add(dep);
          ancestors(dep, acc);
        }
      }
      return acc;
    };
    const covered = gates.some((gate) => ancestors(gate.key, new Set()).size === nodes.length - 1);
    if (!covered) {
      throw new PermanentError('No human approval gate comes after every other task');
    }
  }
}

/**
 * Adds a plan revision and its tasks to a request. Existing tasks are never
 * rewritten: a new revision only appends. Tasks with no dependencies are
 * queued in the caller's transaction.
 */
export async function createPlanRevision(
  client: Client,
  plan: {
    requestId: string;
    template: string;
    nodes: PlanNode[];
    createdBy: string;
    reason: string;
  },
): Promise<{ revisionId: string; taskIds: Record<string, string> }> {
  const revision = await client.query<{ id: string; revision_no: number }>(
    `insert into plan_revision (request_id, revision_no, template, created_by, reason)
     select $1::uuid, coalesce(max(revision_no), 0) + 1, $2::text, $3::text, $4::text
       from plan_revision
      where request_id = $1::uuid
     returning id, revision_no`,
    [plan.requestId, plan.template, plan.createdBy, plan.reason],
  );
  const { id: revisionId, revision_no: revisionNo } = revision.rows[0]!;

  const taskIds: Record<string, string> = {};
  for (const node of plan.nodes) {
    const inserted = await client.query<{ id: string }>(
      `insert into task (request_id, plan_revision_id, key, type, profile, max_attempts, input)
       values ($1, $2, $3, $4, $5, $6, $7)
       returning id`,
      [
        plan.requestId,
        revisionId,
        node.key,
        node.type,
        node.profile ?? null,
        node.maxAttempts ?? 3,
        JSON.stringify(node.input ?? {}),
      ],
    );
    taskIds[node.key] = inserted.rows[0]!.id;
  }
  for (const node of plan.nodes) {
    for (const dep of node.dependsOn) {
      await client.query(
        'insert into task_dependency (task_id, depends_on_task_id) values ($1, $2)',
        [taskIds[node.key], taskIds[dep]],
      );
    }
  }

  await emitEvent(client, plan.requestId, null, 'plan.revision', {
    revisionNo,
    template: plan.template,
    reason: plan.reason,
    tasks: plan.nodes.map((n) => n.key),
  });
  await releaseUnblocked(client, plan.requestId);
  return { revisionId, taskIds };
}
