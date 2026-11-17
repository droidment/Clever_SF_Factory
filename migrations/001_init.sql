-- Core schema for the software factory. See README.md, "Data model".

create sequence work_request_tracking_seq;

create table work_request (
  id uuid primary key default gen_random_uuid(),
  tracking_id text not null unique
    default ('REQ-' || lpad(nextval('work_request_tracking_seq')::text, 6, '0')),
  tenant_id text not null default 'default',
  source text not null,
  requestor text not null,
  title text not null,
  description text not null,
  repository text,
  priority text not null default 'normal'
    check (priority in ('low', 'normal', 'high', 'urgent')),
  acceptance_criteria jsonb not null default '[]',
  status text not null default 'new'
    check (status in ('new', 'in_progress', 'waiting_human', 'failed', 'completed')),
  dedupe_key text not null,
  cost_cap_usd numeric(10, 2) not null default 10,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (tenant_id, dedupe_key)
);

create table plan_revision (
  id uuid primary key default gen_random_uuid(),
  request_id uuid not null references work_request (id) on delete cascade,
  revision_no integer not null,
  template text not null,
  created_by text not null,
  reason text not null,
  created_at timestamptz not null default now(),
  unique (request_id, revision_no)
);

create table task (
  id uuid primary key default gen_random_uuid(),
  request_id uuid not null references work_request (id) on delete cascade,
  plan_revision_id uuid not null references plan_revision (id) on delete cascade,
  key text not null,
  type text not null,
  profile text,
  status text not null default 'pending'
    check (status in ('pending', 'ready', 'running', 'waiting_human', 'done', 'failed')),
  attempt integer not null default 0,
  max_attempts integer not null default 3,
  heartbeat_at timestamptz,
  lease_until timestamptz,
  input jsonb not null default '{}',
  output jsonb,
  error text,
  started_at timestamptz,
  completed_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (plan_revision_id, key)
);

create index task_request_idx on task (request_id);
create index task_running_lease_idx on task (lease_until) where status = 'running';

create table task_dependency (
  task_id uuid not null references task (id) on delete cascade,
  depends_on_task_id uuid not null references task (id) on delete cascade,
  primary key (task_id, depends_on_task_id),
  check (task_id <> depends_on_task_id)
);

create index task_dependency_reverse_idx on task_dependency (depends_on_task_id);

create table agent_run (
  id uuid primary key default gen_random_uuid(),
  task_id uuid not null references task (id) on delete cascade,
  profile text not null,
  model text not null,
  prompt_version text not null,
  tokens_in integer not null default 0,
  tokens_out integer not null default 0,
  cost_usd numeric(10, 4) not null default 0,
  duration_ms integer not null default 0,
  status text not null check (status in ('succeeded', 'failed')),
  error text,
  created_at timestamptz not null default now()
);

create index agent_run_task_idx on agent_run (task_id);

create table artifact (
  id uuid primary key default gen_random_uuid(),
  task_id uuid not null references task (id) on delete cascade,
  type text not null,
  schema_version integer not null default 1,
  content jsonb,
  location text,
  created_at timestamptz not null default now(),
  check (content is not null or location is not null)
);

create index artifact_task_idx on artifact (task_id);

create table approval (
  id uuid primary key default gen_random_uuid(),
  task_id uuid not null references task (id) on delete cascade,
  kind text not null check (kind in ('approval', 'clarification', 'budget')),
  prompt text not null,
  requested_from text,
  decision text check (decision in ('approved', 'rejected', 'answered')),
  decided_by text,
  decided_at timestamptz,
  comment text,
  created_at timestamptz not null default now()
);

create index approval_task_idx on approval (task_id);

create table event (
  id bigint generated always as identity primary key,
  request_id uuid not null references work_request (id) on delete cascade,
  task_id uuid references task (id) on delete cascade,
  type text not null,
  payload jsonb not null default '{}',
  created_at timestamptz not null default now()
);

create index event_request_idx on event (request_id, id);

create table agent_profile (
  name text primary key,
  prompt_version text not null,
  model text not null,
  tools jsonb not null default '[]',
  permissions jsonb not null default '{}',
  limits jsonb not null default '{}'
);

create table audit_log (
  id bigint generated always as identity primary key,
  actor text not null,
  action text not null,
  target text not null,
  detail jsonb not null default '{}',
  created_at timestamptz not null default now()
);
