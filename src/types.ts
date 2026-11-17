import type { Pool } from './db.ts';

export type TaskStatus = 'pending' | 'ready' | 'running' | 'waiting_human' | 'done' | 'failed';

export type RequestStatus = 'new' | 'in_progress' | 'waiting_human' | 'failed' | 'completed';

export type ApprovalKind = 'approval' | 'clarification' | 'budget';

export type Decision = 'approved' | 'rejected' | 'answered';

export interface TaskRow {
  id: string;
  request_id: string;
  plan_revision_id: string;
  key: string;
  type: string;
  profile: string | null;
  status: TaskStatus;
  attempt: number;
  max_attempts: number;
  input: Record<string, unknown>;
  output: unknown;
  error: string | null;
}

export interface WorkRequestRow {
  id: string;
  tracking_id: string;
  tenant_id: string;
  source: string;
  requestor: string;
  title: string;
  description: string;
  repository: string | null;
  priority: string;
  acceptance_criteria: string[];
  status: RequestStatus;
  cost_cap_usd: string;
  created_at: Date;
  updated_at: Date;
}

export interface ArtifactInput {
  type: string;
  schemaVersion?: number;
  content?: unknown;
  location?: string;
}

export type HandlerResult =
  | { kind: 'done'; output?: unknown; artifacts?: ArtifactInput[] }
  | {
      kind: 'wait_human';
      approvalKind: Exclude<ApprovalKind, 'budget'>;
      prompt: string;
      requestedFrom?: string;
    };

export interface HandlerContext {
  task: TaskRow;
  request: WorkRequestRow;
  /** Outputs of the tasks this one depends on, by their plan key. */
  dependencyOutputs: Record<string, unknown>;
  pool: Pool;
  /** Aborted when the task loses its lease; the handler should stop work. */
  signal: AbortSignal;
}

export type TaskHandler = (ctx: HandlerContext) => Promise<HandlerResult>;

/** An error that retrying cannot fix. The task fails without further attempts. */
export class PermanentError extends Error {
  override name = 'PermanentError';
}
