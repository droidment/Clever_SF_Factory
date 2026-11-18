export interface WorkInput {
  title: string;
  description: string;
  requestor: string;
  source: string;
  business_context: string;
  repository: string;
  priority: string;
  due_date: string | null;
  acceptance_criteria: string[];
  security_requirements: string[];
  deploy: boolean;
  budget_usd: number;
}
export interface TaskSpec {
  id: string;
  title: string;
  agent: string;
  dependencies: string[];
  kind: string;
}
export interface Plan {
  revision: number;
  rationale: string;
  tasks: TaskSpec[];
}
export interface Work {
  id: string;
  tenant_id: string;
  status: string;
  stage: string;
  data: WorkInput;
  created_at: string;
  updated_at: string;
  completed_at: string | null;
  cost: number;
  tokens: number;
  gate: string | null;
  triage: { summary: string; category: string; complexity: string; agents: string[]; risks: string[] } | null;
  plan: Plan | null;
}
export interface Task {
  node_id: string;
  status: string;
  spec: TaskSpec;
  started_at: string | null;
  completed_at: string | null;
}
export interface Artifact {
  id: string;
  node_id: string;
  name: string;
  data: { content: string; type: string; location: string | null; sha256: string };
}
export interface Event {
  id: number;
  type: string;
  message: string;
  timestamp: string;
  data: Record<string, unknown>;
}
export interface Execution {
  id: string;
  agent: string;
  execution_key: string;
  attempt: number;
  status: string;
  error: string | null;
  started_at: string;
  completed_at: string | null;
  output: {
    summary: string;
    usage: { cost_usd: number; tokens: number; model: string };
    decisions: string[];
    tool_calls: Record<string, unknown>[];
  } | null;
}
export interface Detail extends Work {
  tasks: Task[];
  artifacts: Artifact[];
  events: Event[];
  executions: Execution[];
  decisions: { gate: string; actor: string; data: { approved: boolean; comment: string } }[];
  workflow: { id: string; engine: string; version: string };
}
export interface Config {
  mode: string;
  runtime?: string;
  auth_mode: string;
  model?: string | null;
  draft_only?: boolean;
  tenant: string;
  actor: string;
  roles: string[];
  repositories: string[];
  demo_login_enabled?: boolean;
  demo_accounts?: { username: string; label: string }[];
  draft_review_required?: boolean;
}
export interface Agent {
  name: string;
  responsibilities: string;
  capabilities: string[];
  tools: string[];
  permissions: string[];
  model_requirements: string;
  input_contract: string;
  output_contract: string;
  prompt_version: string;
  timeout_seconds: number;
  max_attempts: number;
  max_tokens: number;
  enabled: boolean;
}
