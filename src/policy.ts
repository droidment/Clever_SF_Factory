import { z } from 'zod';
import type { TaskDoneHook } from './engine/engine.ts';
import { createPlanRevision, validatePlan, type PlanNode } from './engine/plan.ts';
import { PermanentError } from './types.ts';

/** Agent capability profiles. See README.md, "Agents". */
export const PROFILES = {
  triagePlanner: 'triage_planner',
  implementer: 'implementer',
  reviewer: 'reviewer',
  verifier: 'verifier',
} as const;

/** What the triage task must return for a plan to be created. */
export const TriageOutput = z.object({
  classification: z.enum(['minor_task', 'bug_fix']),
  summary: z.string().min(1),
});
export type TriageOutput = z.infer<typeof TriageOutput>;

/** The plan every request starts with: triage alone. */
export const intakePlan: PlanNode[] = [
  { key: 'triage', type: 'triage', profile: PROFILES.triagePlanner, dependsOn: [] },
];

/**
 * Vetted plan templates. The planner chooses one; it does not write the graph.
 * Neither template contains the merge approval: policy adds it below.
 */
export const templates: Record<TriageOutput['classification'], () => PlanNode[]> = {
  minor_task: () => [
    { key: 'implement', type: 'implement', profile: PROFILES.implementer, dependsOn: [] },
    { key: 'ci', type: 'ci', dependsOn: ['implement'] },
    { key: 'review', type: 'review', profile: PROFILES.reviewer, dependsOn: ['ci'] },
    { key: 'verify', type: 'verify', profile: PROFILES.verifier, dependsOn: ['review'] },
  ],
  bug_fix: () => [
    { key: 'implement', type: 'implement', profile: PROFILES.implementer, dependsOn: [] },
    { key: 'ci', type: 'ci', dependsOn: ['implement'] },
    { key: 'review', type: 'review', profile: PROFILES.reviewer, dependsOn: ['ci'] },
    { key: 'verify', type: 'verify', profile: PROFILES.verifier, dependsOn: ['ci'] },
  ],
};

/**
 * Adds the approval gates policy requires. In the MVP every plan gets one
 * merge approval that waits for all other tasks. Agents cannot remove it.
 */
export function withRequiredGates(nodes: PlanNode[]): PlanNode[] {
  return [
    ...nodes,
    {
      key: 'merge_approval',
      type: 'human_approval',
      dependsOn: nodes.map((n) => n.key),
      input: { prompt: 'Approve merging this change.' },
      maxAttempts: 1,
    },
  ];
}

/** When triage finishes, turn its classification into a plan in the same transaction. */
export const onTaskDone: TaskDoneHook = async (client, task, output) => {
  if (task.type !== 'triage') return;

  const parsed = TriageOutput.safeParse(output);
  if (!parsed.success) {
    throw new PermanentError(`Triage output is not valid: ${parsed.error.message}`);
  }
  const nodes = withRequiredGates(templates[parsed.data.classification]());
  validatePlan(nodes, { requireApproval: true });
  await createPlanRevision(client, {
    requestId: task.request_id,
    template: parsed.data.classification,
    nodes,
    createdBy: PROFILES.triagePlanner,
    reason: parsed.data.summary,
  });
};
