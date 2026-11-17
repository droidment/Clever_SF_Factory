import type { TaskHandler } from '../types.ts';

/**
 * A plan's approval gate. It parks the task until a person records a decision;
 * approving completes the task, so the handler never runs a second time.
 */
export const humanApproval: TaskHandler = async ({ task }) => {
  const prompt = typeof task.input.prompt === 'string' ? task.input.prompt : 'Approval required.';
  return { kind: 'wait_human', approvalKind: 'approval', prompt };
};
