import React, { useCallback, useEffect, useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';
import {
  ArrowDown,
  ArrowLeft,
  ArrowRight,
  ArrowUpRight,
  ChartBar,
  Check,
  CheckCircle,
  Clock,
  Code,
  Cube,
  FileText,
  GitBranch,
  GithubLogo,
  Lightning,
  MagnifyingGlass,
  PauseCircle,
  Plus,
  ShieldCheck,
  SquaresFour,
  Stack,
  Terminal,
  Users,
  WarningCircle,
  X,
} from '@phosphor-icons/react';
import '@fontsource/geist/latin-400.css';
import '@fontsource/geist/latin-500.css';
import '@fontsource/geist/latin-600.css';
import '@fontsource/geist-mono/latin-400.css';
import { api, connectEvents, setToken } from './api';
import type { Agent, Config, Detail, TaskSpec, Work } from './types';
import './styles.css';
import { HowItWorks } from './HowItWorks';
import { AccountDialog } from './AccountDialog';

const pages = new Set(['overview', 'requests', 'approvals', 'agents', 'activity', 'how-it-works']);
const currentPage = () => pages.has(location.hash.slice(1)) ? location.hash.slice(1) : 'overview';

const terminal = new Set(['completed', 'simulated', 'drafted', 'failed', 'rejected', 'cancelled']);
const names: Record<string, string> = {
  waiting_for_human: 'Needs your input',
  running: 'In progress',
  triaging: 'Triage',
  planning: 'Planning',
  validating: 'Validating',
  queued: 'Queued',
  completed: 'Completed',
  simulated: 'Simulated',
  drafted: 'Draft ready',
  failed: 'Failed',
  rejected: 'Declined',
  cancelled: 'Cancelled',
  pending: 'Pending',
  waiting: 'Waiting',
};
const money = (n: number) => new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD' }).format(n);
const date = (s: string) => new Date(s.endsWith('Z') || /[+-]\d\d:\d\d$/.test(s) ? s : s + 'Z');
const shortId = (s: string) => 'WR-' + s.slice(0, 6).toUpperCase();
function Badge({ status }: { status: string }) {
  return (
    <span className={`badge status-${status}`}>
      <span />
      {names[status] || status}
    </span>
  );
}
function ErrorBox({ error }: { error: string }) {
  return (
    <div role="alert" className="error-box">
      <WarningCircle size={18} />
      {error}
    </div>
  );
}

function App() {
  const [config, setConfig] = useState<Config | null>(null),
    [works, setWorks] = useState<Work[]>([]),
    [agents, setAgents] = useState<Agent[]>([]);
  const [utilization, setUtilization] = useState<{
    agents: Record<string, { active: number; completed: number; failed: number }>;
    active_tasks: number;
  }>({ agents: {}, active_tasks: 0 });
  const [page, setPage] = useState(currentPage),
    [selected, setSelected] = useState<string | null>(null),
    [detail, setDetail] = useState<Detail | null>(null);
  const [error, setError] = useState(''),
    [loading, setLoading] = useState(true),
    [connected, setConnected] = useState(false),
    [modal, setModal] = useState(false);
  const [accountModal, setAccountModal] = useState(false);
  const [search, setSearch] = useState(''),
    [filter, setFilter] = useState('all'),
    [login, setLogin] = useState(''),
    [busy, setBusy] = useState(false);
  const selectedRef = useRef(selected);
  selectedRef.current = selected;
  const refresh = useCallback(async () => {
    try {
      const [result, stats] = await Promise.all([
        api<{ items: Work[] }>('/api/requests?limit=200'),
        api<typeof utilization>('/api/stats'),
      ]);
      setWorks(result.items);
      setUtilization(stats);
      const id = selectedRef.current;
      if (id) {
        const item = await api<Detail>(`/api/requests/${id}`);
        if (selectedRef.current === id) setDetail(item);
      }
      setError('');
    } catch (e) {
      setError((e as Error).message);
    }
  }, []);
  const initialize = useCallback(async () => {
    setLoading(true);
    try {
      const [c, a] = await Promise.all([api<Config>('/api/config'), api<Agent[]>('/api/agents')]);
      setConfig(c);
      setAgents(a);
      await refresh();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoading(false);
    }
  }, [refresh]);
  useEffect(() => {
    void initialize();
  }, [initialize]);
  useEffect(() => {
    const change = () => { setPage(currentPage()); setSelected(null); };
    window.addEventListener('hashchange', change);
    window.addEventListener('popstate', change);
    return () => { window.removeEventListener('hashchange', change); window.removeEventListener('popstate', change); };
  }, []);
  useEffect(() => {
    if (!config) return;
    let pending: ReturnType<typeof setTimeout> | undefined;
    const close = connectEvents(() => {
      if (!pending)
        pending = setTimeout(() => {
          pending = undefined;
          void refresh();
        }, 180);
    }, setConnected);
    const fallback = setInterval(() => void refresh(), 15000);
    return () => {
      close();
      clearTimeout(pending);
      clearInterval(fallback);
    };
  }, [config, refresh]);
  useEffect(() => {
    if (!selected) {
      setDetail(null);
      return;
    }
    setDetail(null);
    void refresh();
  }, [selected, refresh]);
  const navigate = (next: string) => {
    setPage(next);
    history.pushState(null, '', '#' + next);
    setSelected(null);
    setFilter('all');
    setSearch('');
  };
  const active = works.filter(
    (w) => !terminal.has(w.status) && w.status !== 'waiting_for_human' && w.status !== 'queued',
  );
  const waiting = works.filter((w) => w.status === 'waiting_for_human');
  const done = works.filter((w) => w.status === (config?.mode === 'anthropic' ? 'drafted' : config?.mode === 'demo' ? 'simulated' : 'completed'));
  const failed = works.filter((w) => w.status === 'failed');
  const visible = works.filter(
    (w) =>
      (page !== 'approvals' || w.status === 'waiting_for_human') &&
      (filter === 'all' || w.status === filter) &&
      (w.data.title + ' ' + w.data.repository + ' ' + shortId(w.id))
        .toLowerCase()
        .includes(search.toLowerCase()),
  );
  const seed = async () => {
    setBusy(true);
    try {
      await api('/api/demo/seed', { method: 'POST' });
      await refresh();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };
  const open = (id: string) => {
    setSelected(id);
  };
  const avg = done.length
    ? done.reduce((s, w) => s + (date(w.completed_at!).getTime() - date(w.created_at).getTime()) / 60000, 0) /
      done.length
    : 0;
  return (
    <div className="app-shell min-h-[100dvh]">
      <aside className="sidebar flex flex-col">
        <a
          href="#"
          className="brand"
          onClick={(e) => {
            e.preventDefault();
            navigate('overview');
          }}
        >
          <div className="brand-mark">
            <Stack weight="bold" size={24} />
          </div>
          foundry<span className="brand-dot">.</span>
        </a>
        <div className="workspace">
          <span className="workspace-icon">SF</span>
          <div>
            <strong>Software Factory</strong>
            <small>{config?.tenant === 'demo' ? 'Local workspace' : config?.tenant || 'Connecting'}</small>
          </div>
          <span className="workspace-version">v1</span>
        </div>
        <p className="nav-label">WORKSPACE</p>
        <nav aria-label="Main navigation">
          {[
            ['overview', 'Overview', SquaresFour],
            ['requests', 'Work requests', Stack],
            ['approvals', 'Human approvals', ShieldCheck],
            ['agents', 'Agent registry', Cube],
            ['activity', 'Activity log', Terminal],
          ].map(([id, label, Icon]) => (
            <button
              key={id as string}
              aria-label={label as string}
              className={page === id ? 'nav-item selected' : 'nav-item'}
              onClick={() => navigate(id as string)}
            >
              <Icon size={19} />
              <span>{label as string}</span>
              {id === 'approvals' && waiting.length > 0 && <b>{waiting.length}</b>}
            </button>
          ))}
          <a href="#how-it-works" aria-label="How it works" className={page === 'how-it-works' ? 'nav-item selected' : 'nav-item'} onClick={e => { e.preventDefault(); navigate('how-it-works'); }}><FileText size={19} /><span>How it works</span></a>
        </nav>
        <div className="sidebar-note">
          <GitBranch size={21} />
          <strong>Every step, accounted for.</strong>
          <p>{config?.mode === 'anthropic' ? 'From your request to an AI draft. Review proposed work before execution.' : 'From first request to verified delivery. Follow the work as it happens.'}</p>
          <a href="/docs" target="_blank" rel="noreferrer">
            Explore the API <ArrowUpRight />
          </a>
        </div>
        <div className="sidebar-bottom">
          <div className="avatar">LO</div>
          <div>
            <strong>{config?.actor || 'Local operator'}</strong>
            <small>{config?.mode === 'anthropic' ? 'Claude draft workspace' : config?.mode === 'demo' ? 'Demonstration workspace' : 'Enterprise workspace'}</small>
          </div>
        </div>
      </aside>
      <div className="main-shell">
        <header className="topbar">
          <div className="breadcrumbs">
            Workspace <span>/</span>{' '}
            <strong>
              {selected
                ? 'Request detail'
                : {
                    overview: 'Overview',
                    requests: 'Work requests',
                    approvals: 'Human approvals',
                    agents: 'Agent registry',
                    activity: 'Activity log',
                    'how-it-works': 'How it works',
                  }[page]}
            </strong>
          </div>
          <div className="topbar-right">
            {config?.demo_login_enabled && <button className="secondary account-switch" onClick={() => setAccountModal(true)}><Users size={16} /><span>{config.actor.startsWith('demo:') ? `${config.actor.slice(5)} · Switch account` : 'Sign in / Switch account'}</span></button>}
            <span className={`connection ${connected ? 'is-live' : ''}`}>
              <i />
              {connected ? 'Live updates' : 'Reconnecting'}
            </span>
            <span className="topbar-divider" />
            <span className="env-label">{config?.mode === 'anthropic' ? 'CLAUDE · DRAFTS' : config?.mode === 'demo' ? 'DEMO' : 'GATEWAY'}</span>
          </div>
        </header>
        <main className="max-w-[1440px] mx-auto">
          {config && !config.roles.includes('requester') && !config.roles.includes('admin') && (
            <div className="demo-banner">
              <span>Public view. Browse requests and drafts. Creating requests and managing workflows require authorized access.</span>
              <span className="demo-pill">READ ONLY</span>
            </div>
          )}
          {config?.mode === 'anthropic' && (
            <div className="demo-banner">
              <Code size={16} />
              <span>Claude generates real AI drafts using {config.model}. API usage is billed. No repository access, test execution or deployments are connected.</span>
              <span className="demo-pill">DRAFTS</span>
            </div>
          )}
          {config?.mode === 'demo' && (
            <div className="demo-banner">
              <Code size={16} />
              <span>
                Demonstration mode. Agent output is simulated; no repositories or deployments are changed.
              </span>
              <span className="demo-pill">LOCAL</span>
            </div>
          )}
          {error && <ErrorBox error={error} />}
          {!config && !loading && (
            <form
              className="login-panel"
              onSubmit={(e) => {
                e.preventDefault();
                setToken(login);
                void initialize();
              }}
            >
              <h2>Connect your workspace</h2>
              <p>
                Enter an enterprise access token issued for this API. It is held in memory for this session.
              </p>
              <label>
                Access token
                <input
                  type="password"
                  value={login}
                  onChange={(e) => setLogin(e.target.value)}
                  autoComplete="off"
                  required
                />
              </label>
              <button className="primary">Connect</button>
            </form>
          )}
          {loading ? (
            <div aria-label="Loading workspace" className="skeleton-layout">
              <div />
              <div />
              <div />
            </div>
          ) : page === 'how-it-works' ? (
            <HowItWorks config={config} />
          ) : selected ? (
            detail ? (
              <RequestDetail
                detail={detail}
                config={config!}
                back={() => setSelected(null)}
                refresh={refresh}
              />
            ) : (
              <div className="skeleton-layout">
                <div />
                <div />
              </div>
            )
          ) : (
            <>
              <div className="page-heading">
                <div>
                  <p className="eyebrow">THE SOFTWARE DELIVERY CONTROL ROOM</p>
                  <h1>
                    {
                      {
                        overview: 'Factory overview',
                        requests: 'Work requests',
                        approvals: 'Human approvals',
                        agents: 'Agent registry',
                        activity: 'Activity log',
                      }[page]
                    }
                  </h1>
                  <p className="subtitle">
                    {
                      {
                        overview: 'A clear view of the work moving through your factory.',
                        requests: 'One place for every request, from intake to completion.',
                        approvals: 'Your judgment, right where the workflow needs it.',
                        agents: 'Specialized capabilities. Explicit permissions. Bounded execution.',
                        activity: 'The recorded decisions and events behind every request.',
                      }[page]
                    }
                  </p>
                </div>
                {page !== 'agents' && page !== 'activity' && (config?.roles.includes('requester') || config?.roles.includes('admin')) && (
                  <button className="primary" onClick={() => setModal(true)}>
                    <Plus size={17} />
                    New request
                  </button>
                )}
              </div>
              {page === 'overview' && (
                <>
                  <div className="metrics-grid grid grid-cols-2 xl:grid-cols-4">
                    <Metric
                      label="Active workflows"
                      value={String(active.length).padStart(2, '0')}
                      icon={<Lightning size={20} />}
                      detail={`${works.filter((w) => w.status === 'queued').length} queued for execution`}
                      accent
                    />
                    <Metric
                      label="Waiting for you"
                      value={String(waiting.length).padStart(2, '0')}
                      icon={<PauseCircle size={20} />}
                      detail="Human decisions needed"
                    />
                    <Metric
                      label={config?.mode === 'anthropic' ? 'AI drafts ready' : config?.mode === 'demo' ? 'Simulations finished' : 'Verified completions'}
                      value={String(done.length).padStart(2, '0')}
                      icon={<CheckCircle size={20} />}
                      detail={`${failed.length} failed · ${works.length} total requests`}
                    />
                    <Metric
                      label={config?.mode === 'anthropic' ? 'Estimated model spend' : 'Model & tool spend'}
                      value={money(works.reduce((s, w) => s + w.cost, 0))}
                      icon={<ChartBar size={20} />}
                      detail={
                        config?.mode === 'demo'
                          ? 'No paid models used'
                          : `${works.reduce((s, w) => s + w.tokens, 0).toLocaleString()} tokens recorded${config?.mode === 'anthropic' ? ' · provider rates' : ''}`
                      }
                    />
                  </div>
                  <div className="overview-band">
                    <div>
                      <span className="band-icon">
                        <GitBranch size={24} />
                      </span>
                      <div>
                        <h3>Built to keep work moving</h3>
                        <p>
                          {config?.mode === 'demo'
                            ? 'Explore planning, parallel execution, and human checkpoints.'
                            : config?.mode === 'anthropic' ? 'Claude drafts plans and proposed changes for your review.' : 'Durable workflows coordinate every agent and checkpoint.'}
                        </p>
                      </div>
                    </div>
                    <div className="band-stats">
                      <span>
                        <strong>{avg ? `${avg.toFixed(1)}m` : '—'}</strong>Avg. completion
                      </span>
                      <span>
                        <strong>
                          {works.length ? `${Math.round((failed.length / works.length) * 100)}%` : '—'}
                        </strong>
                        Failure rate
                      </span>
                      <span>
                        <strong>{agents.filter((a) => a.enabled).length}</strong>Available agents
                      </span>
                    </div>
                  </div>
                </>
              )}
              {(page === 'overview' || page === 'requests' || page === 'approvals') && (
                <div className="work-layout">
                  <section className="request-panel">
                    <div className="section-heading">
                      <div>
                        <h2>
                          {page === 'approvals' ? 'Open decisions' : 'Request pipeline'}
                          <span className="count">{visible.length}</span>
                        </h2>
                        <p>
                          {page === 'approvals'
                            ? 'Approve, decline, or clarify to move the request forward.'
                            : 'Track progress across your delivery lifecycle.'}
                        </p>
                      </div>
                      {page === 'overview' && (
                        <button className="text-button" onClick={() => navigate('requests')}>
                          View all <ArrowRight size={15} />
                        </button>
                      )}
                    </div>
                    <div className="table-controls">
                      <div className="filter-tabs">
                        <button className={filter === 'all' ? 'active' : ''} onClick={() => setFilter('all')}>
                          All requests
                        </button>
                        <button
                          className={filter === 'running' ? 'active' : ''}
                          onClick={() => setFilter('running')}
                        >
                          Running
                        </button>
                        <button
                          className={filter === 'waiting_for_human' ? 'active' : ''}
                          onClick={() => setFilter('waiting_for_human')}
                        >
                          Needs input{waiting.length > 0 && <span>{waiting.length}</span>}
                        </button>
                      </div>
                      <label className="search-field">
                        <MagnifyingGlass size={17} />
                        <input
                          aria-label="Search requests"
                          placeholder="Search requests..."
                          value={search}
                          onChange={(e) => setSearch(e.target.value)}
                        />
                      </label>
                    </div>
                    {visible.length ? (
                      <div className="table-scroll">
                        <table>
                          <thead>
                            <tr>
                              <th>REQUEST</th>
                              <th>STATUS</th>
                              <th>PRIORITY</th>
                              <th>CREATED</th>
                              <th aria-label="Open request" />
                            </tr>
                          </thead>
                          <tbody>
                            {visible.map((w) => (
                              <tr key={w.id}>
                                <td>
                                  <button className="request-link" onClick={() => open(w.id)}>
                                    <span className="request-id">
                                      {shortId(w.id)} <span>· {w.data.source}</span>
                                    </span>
                                    <strong>{w.data.title}</strong>
                                    <span className="repository">
                                      <GithubLogo size={13} />
                                      {w.data.repository}
                                    </span>
                                  </button>
                                </td>
                                <td>
                                  <Badge status={w.status} />
                                </td>
                                <td>
                                  <span className={`priority priority-${w.data.priority}`}>
                                    <i />
                                    <i />
                                    <i />
                                    {w.data.priority}
                                  </span>
                                </td>
                                <td className="date-cell">
                                  {date(w.created_at).toLocaleDateString('en-US', {
                                    month: 'short',
                                    day: 'numeric',
                                  })}
                                  <small>
                                    {date(w.created_at).toLocaleTimeString('en-US', {
                                      hour: '2-digit',
                                      minute: '2-digit',
                                    })}
                                  </small>
                                </td>
                                <td>
                                  <button
                                    aria-label={`Open ${w.data.title}`}
                                    className="icon-button"
                                    onClick={() => open(w.id)}
                                  >
                                    <ArrowUpRight size={18} />
                                  </button>
                                </td>
                              </tr>
                            ))}
                          </tbody>
                        </table>
                      </div>
                    ) : (
                      <div className="empty-state">
                        <div className="empty-icon">
                          <Stack size={28} />
                        </div>
                        <h3>
                          {works.length
                            ? 'No matching requests'
                            : 'Your factory is ready for its first request'}
                        </h3>
                        <p>
                          {works.length
                            ? 'Adjust the filters to find your work.'
                            : 'Describe a change and define what successful delivery looks like.'}
                        </p>
                        {!works.length && (config?.roles.includes('requester') || config?.roles.includes('admin')) && (
                          <div className="flex flex-wrap justify-center gap-3">
                            <button className="primary" onClick={() => setModal(true)}>
                              Create a request <ArrowRight size={16} />
                            </button>
                            {config?.mode === 'demo' && (
                              <button className="secondary" disabled={busy} onClick={() => void seed()}>
                                {busy ? 'Loading...' : 'Load sample requests'}
                              </button>
                            )}
                          </div>
                        )}
                      </div>
                    )}
                    <div className="table-footer">
                      <span>Showing {visible.length} requests · up to 200 most recent</span>
                      <span>
                        <span className="tiny-dot" />
                        Updates automatically
                      </span>
                    </div>
                  </section>
                  {page === 'overview' && (
                    <aside className="right-column">
                      <section className="attention-panel">
                        <div className="section-heading">
                          <h2>
                            <ShieldCheck size={19} />
                            Your attention
                          </h2>
                          <span className="count">{waiting.length}</span>
                        </div>
                        {waiting.length ? (
                          waiting.slice(0, 3).map((w) => (
                            <button className="attention-item" key={w.id} onClick={() => open(w.id)}>
                              <small>
                                {w.gate?.startsWith('clarification') ? 'CLARIFICATION' : 'RELEASE APPROVAL'}
                              </small>
                              <strong>{w.data.title}</strong>
                              <span>
                                Review request <ArrowUpRight size={14} />
                              </span>
                            </button>
                          ))
                        ) : (
                          <div className="all-clear">
                            <CheckCircle size={30} />
                            <strong>No decisions waiting</strong>
                            <p>Approval requests will appear here when a workflow needs you.</p>
                          </div>
                        )}
                      </section>
                      <section className="agents-summary">
                        <div className="section-heading">
                          <h2>Agent activity</h2>
                          <span className="small-label">LIVE</span>
                        </div>
                        {['triage', 'backend', 'frontend', 'test', 'security'].map((name) => (
                          <div className="agent-row" key={name}>
                            <div className="agent-avatar">
                              <Code size={16} />
                            </div>
                            <div>
                              <strong>{name.charAt(0).toUpperCase() + name.slice(1)}</strong>
                              <small>{utilization.agents[name]?.completed || 0} attempts finished</small>
                            </div>
                            <span className="agent-ready">
                              {utilization.agents[name]?.active
                                ? `${utilization.agents[name].active} active`
                                : 'Idle'}
                            </span>
                          </div>
                        ))}
                        <p className="helper">
                          {utilization.active_tasks} active agent attempts across this workspace.
                        </p>
                        <button className="text-button" onClick={() => navigate('agents')}>
                          Explore registry <ArrowRight size={15} />
                        </button>
                      </section>
                    </aside>
                  )}
                </div>
              )}
              {page === 'agents' && <AgentRegistry agents={agents} />}
              {page === 'activity' && <ActivityLog works={works} open={open} />}
            </>
          )}
          <footer className="page-footer">
            <span>
              FOUNDRY <span>/</span> SOFTWARE FACTORY
            </span>
            <span>Observable by design.</span>
          </footer>
        </main>
      </div>
      {modal && config && (
        <IntakeModal
          config={config}
          close={() => setModal(false)}
          created={(id) => {
            setModal(false);
            setSelected(id);
            void refresh();
          }}
        />
      )}
      {accountModal && config && <AccountDialog config={config} close={() => setAccountModal(false)} changed={initialize} />}
    </div>
  );
}

function Metric({
  label,
  value,
  icon,
  detail,
  accent = false,
}: {
  label: string;
  value: string;
  icon: React.ReactNode;
  detail: string;
  accent?: boolean;
}) {
  return (
    <div className={`metric ${accent ? 'metric-accent' : ''}`}>
      <div className="metric-top">
        <span>{label}</span>
        {icon}
      </div>
      <strong>{value}</strong>
      <small>{detail}</small>
    </div>
  );
}

function RequestDetail({
  detail: d,
  config,
  back,
  refresh,
}: {
  detail: Detail;
  config: Config;
  back: () => void;
  refresh: () => Promise<void>;
}) {
  const [tab, setTab] = useState('workflow'),
    [node, setNode] = useState<string | null>(null),
    [comment, setComment] = useState(''),
    [criteria, setCriteria] = useState(''),
    [busy, setBusy] = useState(false),
    [error, setError] = useState('');
  useEffect(() => {
    setNode(null);
    setTab('workflow');
    setComment('');
    setCriteria('');
    setError('');
  }, [d.id]);
  const decide = async (approved: boolean) => {
    setBusy(true);
    setError('');
    try {
      await api(`/api/requests/${d.id}/decisions`, {
        method: 'POST',
        body: JSON.stringify({
          gate: d.gate,
          approved,
          comment,
          ...(d.gate?.startsWith('clarification') && approved
            ? { acceptance_criteria: criteria.split('\n').filter(Boolean) }
            : {}),
        }),
      });
      setComment('');
      await refresh();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };
  const cancel = async () => {
    setBusy(true);
    try {
      await api(`/api/requests/${d.id}/cancel`, { method: 'POST' });
      await refresh();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };
  const currentTasks = d.tasks.filter((t) => t.node_id.startsWith(`r${d.plan?.revision}:`));
  const selectedTask = currentTasks.find((t) => t.spec.id === node);
  const canApprove = config.roles.includes('approver') || config.roles.includes('admin');
  const recorded = d.decisions.some((item) => item.gate === d.gate);
  return (
    <>
      <button className="back-link" onClick={back}>
        <ArrowLeft size={16} />
        Back to requests
      </button>
      <div className="detail-heading">
        <div>
          <div className="flex items-center gap-3">
            <span className="request-id">{shortId(d.id)}</span>
            <Badge status={d.status} />
          </div>
          <h1>{d.data.title}</h1>
          <div className="detail-meta">
            <span>
              <GithubLogo size={16} />
              {d.data.repository}
            </span>
            <span>
              <Users size={16} />
              {d.data.requestor}
            </span>
            <span>
              <Clock size={16} />
              {date(d.created_at).toLocaleString()}
            </span>
          </div>
        </div>
        {!terminal.has(d.status) && (config.roles.includes('operator') || config.roles.includes('admin')) && (
          <button className="secondary" disabled={busy} onClick={() => void cancel()}>
            Cancel workflow
          </button>
        )}
      </div>
      {error && <ErrorBox error={error} />}
      {d.status === 'waiting_for_human' && (
        <section className="approval-box">
          <div>
            <ShieldCheck size={25} />
            <div>
              <h2>{d.stage}</h2>
              <p>
                {recorded
                  ? 'Decision recorded. Waiting for the workflow to resume.'
                  : d.gate?.startsWith('clarification')
                    ? 'Add measurable criteria and context to continue.'
                    : d.gate === 'r0:policy-draft-review' ? 'Review the proposed artifacts. Approval accepts the drafts; it does not verify or deploy software.'
                    : 'Review artifacts and verification results before authorizing release.'}
              </p>
            </div>
          </div>
          {canApprove && !recorded && (
            <form
              onSubmit={(e) => {
                e.preventDefault();
                void decide(true);
              }}
            >
              {d.gate?.startsWith('clarification') && (
                <label>
                  Acceptance criteria · one per line
                  <textarea
                    value={criteria}
                    onChange={(e) => setCriteria(e.target.value)}
                    required
                    rows={3}
                  />
                </label>
              )}
              <label>
                Decision notes
                <textarea
                  placeholder="Explain the decision for the audit trail..."
                  value={comment}
                  onChange={(e) => setComment(e.target.value)}
                  minLength={3}
                  maxLength={2000}
                  required
                  rows={2}
                />
              </label>
              <div className="flex gap-3">
                <button className="primary" disabled={busy || comment.trim().length < 3}>
                  <Check size={16} />
                  {busy
                    ? 'Submitting...'
                    : d.gate?.startsWith('clarification')
                      ? 'Submit clarification'
                      : d.gate === 'r0:policy-draft-review' ? 'Approve drafts' : 'Approve release'}
                </button>
                <button
                  type="button"
                  className="secondary"
                  disabled={busy || comment.trim().length < 3}
                  onClick={() => void decide(false)}
                >
                  Decline
                </button>
              </div>
            </form>
          )}
        </section>
      )}
      <div className="detail-stats">
        <div>
          <small>CURRENT ACTIVITY</small>
          <strong>{d.stage}</strong>
        </div>
        <div>
          <small>TASKS FINISHED</small>
          <strong className="font-mono">
            {currentTasks.filter((t) => t.status === 'completed' || t.status === 'drafted').length} <span>/ {currentTasks.length}</span>
          </strong>
        </div>
        <div>
          <small>SPEND / BUDGET</small>
          <strong className="font-mono">
            {money(d.cost)} <span>/ {money(d.data.budget_usd)}</span>
          </strong>
        </div>
      </div>
      <div className="detail-tabs" role="tablist" aria-label="Request views">
        {[
          ['workflow', 'Execution plan', GitBranch],
          ['request', 'Request & triage', FileText],
          ['artifacts', `Artifacts (${d.artifacts.length})`, Code],
          ['timeline', 'Timeline', Clock],
          ['executions', 'Agent executions', Terminal],
        ].map(([key, title, Icon]) => (
          <button
            key={key as string}
            role="tab"
            aria-selected={tab === key}
            className={tab === key ? 'active' : ''}
            onClick={() => setTab(key as string)}
          >
            <Icon size={17} />
            {title as string}
          </button>
        ))}
      </div>
      {tab === 'workflow' && (
        <section className="graph-panel">
          <div className="section-heading">
            <div>
              <h2>Dynamic execution graph</h2>
              <p>{d.plan?.rationale || 'The graph will appear after triage and planning.'}</p>
            </div>
            {d.plan && <span className="outline-tag">REVISION {d.plan.revision + 1}</span>}
          </div>
          {d.plan ? (
            <>
              <DAG
                tasks={d.plan.tasks}
                states={Object.fromEntries(currentTasks.map((t) => [t.spec.id, t.status]))}
                selected={node}
                select={setNode}
              />
              {selectedTask && (
                <div className="node-inspector">
                  <h3>{selectedTask.spec.title}</h3>
                  <Badge status={selectedTask.status} />
                  <p>
                    Agent: <strong>{selectedTask.spec.agent}</strong> · Dependencies:{' '}
                    {selectedTask.spec.dependencies.join(', ') || 'None'}
                  </p>
                  <p className="helper">
                    {d.executions
                      .filter((e) => e.execution_key === selectedTask.node_id)
                      .map(
                        (e) =>
                          `${e.output?.summary || e.error || 'Execution in progress'} (attempt ${e.attempt})`,
                      )
                      .join('\n') || 'No agent execution recorded yet.'}
                  </p>
                </div>
              )}
              <div className="graph-legend">
                <span>
                  <i className="done" />
                  Completed
                </span>
                <span>
                  <i className="running" />
                  Running
                </span>
                <span>
                  <i className="waiting" />
                  Human checkpoint
                </span>
                <span>
                  <i />
                  Pending
                </span>
              </div>
            </>
          ) : (
            <div className="empty-state compact">
              <GitBranch size={32} />
              <h3>Planning is ahead</h3>
              <p>{d.stage}</p>
            </div>
          )}
        </section>
      )}
      {tab === 'request' && (
        <div className="request-info-grid">
          <section className="content-panel">
            <h2>Original request</h2>
            <p className="whitespace-pre-wrap">{d.data.description}</p>
            {d.data.business_context && (
              <>
                <h3>Business context</h3>
                <p>{d.data.business_context}</p>
              </>
            )}
            <h3>Acceptance criteria</h3>
            <ul className="criteria-list">
              {d.data.acceptance_criteria.length ? (
                d.data.acceptance_criteria.map((c) => (
                  <li key={c}>
                    <CheckCircle size={17} />
                    {c}
                  </li>
                ))
              ) : (
                <li>Awaiting clarification</li>
              )}
            </ul>
            <h3>Delivery requirements</h3>
            <p>
              {d.data.deploy
                ? 'Deployment requested; human release approval required.'
                : 'Implementation and verification only.'}
            </p>
            <p>Due date: {d.data.due_date || 'Not specified'}</p>
            <p>Security: {d.data.security_requirements.join('; ') || 'Baseline security checks required'}</p>
          </section>
          <section className="content-panel">
            <h2>Triage assessment</h2>
            {d.triage ? (
              <>
                <span className="outline-tag">{d.triage.category.replaceAll('_', ' ')}</span>
                <p>{d.triage.summary}</p>
                <h3>Selected capabilities</h3>
                <div className="tag-list">
                  {d.triage.agents.map((a) => (
                    <span key={a}>{a}</span>
                  ))}
                </div>
                <h3>Risk findings</h3>
                <p>{d.triage.risks.join('\n') || 'No additional risks recorded'}</p>
              </>
            ) : (
              <p>Triage has not finished.</p>
            )}
            <h3>Execution identity</h3>
            <code className="break-all">{d.workflow.id}</code>
            <p className="helper">
              {d.workflow.engine} · contract {d.workflow.version}
            </p>
          </section>
        </div>
      )}
      {tab === 'artifacts' && (
        <section className="content-panel">
          <h2>Structured handoffs</h2>
          <p className="subtitle">Versioned outputs shared between agents and retained for review.</p>
          {d.artifacts.length ? (
            d.artifacts.map((a) => (
              <details className="artifact" key={a.id}>
                <summary>
                  <FileText size={19} />
                  <span>
                    <strong>{a.name}</strong>
                    <small>
                      {a.node_id} · {a.data.type.replaceAll('_', ' ')}
                    </small>
                  </span>
                  <span className="outline-tag">v1</span>
                </summary>
                <pre>{a.data.content}</pre>
                <p className="helper break-all">SHA-256: {a.data.sha256}</p>
                {a.data.location && (
                  <a href={a.data.location} target="_blank" rel="noreferrer">
                    Open evidence <ArrowUpRight />
                  </a>
                )}
              </details>
            ))
          ) : (
            <div className="empty-state compact">
              <FileText size={30} />
              <h3>No artifacts yet</h3>
              <p>Agent outputs will appear here as tasks complete.</p>
            </div>
          )}
        </section>
      )}
      {tab === 'timeline' && (
        <section className="content-panel">
          <h2>Execution timeline</h2>
          <div className="timeline">
            {[...d.events].reverse().map((e) => (
              <div className="timeline-event" key={e.id}>
                <span className="timeline-dot" />
                <div>
                  <small>{e.type}</small>
                  <p>{e.message}</p>
                </div>
                <time>{date(e.timestamp).toLocaleTimeString()}</time>
              </div>
            ))}
          </div>
        </section>
      )}
      {tab === 'executions' && (
        <section className="content-panel">
          <h2>Agent execution ledger</h2>
          <p className="subtitle">Each attempt records its result, model usage, and error.</p>
          {d.executions.map((e) => (
            <details className="artifact" key={e.id}>
              <summary>
                <Terminal size={18} />
                <span>
                  <strong>
                    {e.agent} · {e.execution_key}
                  </strong>
                  <small>
                    Attempt {e.attempt} · {e.output?.usage.model || 'Awaiting result'}
                  </small>
                </span>
                <Badge status={e.status} />
              </summary>
              <pre>{JSON.stringify(e.output || { error: e.error, status: e.status }, null, 2)}</pre>
            </details>
          ))}
        </section>
      )}
    </>
  );
}

function DAG({
  tasks,
  states,
  selected,
  select,
}: {
  tasks: TaskSpec[];
  states: Record<string, string>;
  selected: string | null;
  select: (id: string) => void;
}) {
  const levels: TaskSpec[][] = [];
  const done = new Set<string>();
  while (done.size < tasks.length) {
    const ready = tasks.filter((t) => !done.has(t.id) && t.dependencies.every((id) => done.has(id)));
    if (!ready.length) break;
    levels.push(ready);
    ready.forEach((t) => done.add(t.id));
  }
  return (
    <div className="dag" aria-label="Workflow dependency graph">
      {levels.map((level, i) => (
        <React.Fragment key={i}>
          {i > 0 && (
            <div className="dag-connector">
              <ArrowDown size={21} />
            </div>
          )}
          <div className="dag-level">
            {level.map((t) => (
              <button
                key={t.id}
                className={`dag-node ${states[t.id] || 'pending'} ${selected === t.id ? 'node-selected' : ''}`}
                onClick={() => select(t.id)}
              >
                <div className="dag-node-top">
                  <span className="node-icon">
                    {t.kind === 'approval' ? (
                      <ShieldCheck size={18} />
                    ) : t.agent === 'validation' ? (
                      <CheckCircle size={18} />
                    ) : (
                      <Code size={18} />
                    )}
                  </span>
                  <span className="node-agent">{t.agent}</span>
                  {states[t.id] === 'completed' && <Check size={15} className="node-check" />}
                </div>
                <strong>{t.title}</strong>
                <span className="node-state">{names[states[t.id] || 'pending'] || states[t.id]}</span>
              </button>
            ))}
          </div>
        </React.Fragment>
      ))}
    </div>
  );
}

function IntakeModal({
  config,
  close,
  created,
}: {
  config: Config;
  close: () => void;
  created: (id: string) => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null),
    key = useRef(crypto.randomUUID());
  const [error, setError] = useState(''),
    [busy, setBusy] = useState(false);
  useEffect(() => {
    dialog.current?.showModal();
  }, []);
  const submit = async (e: React.FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    setBusy(true);
    setError('');
    const f = new FormData(e.currentTarget);
    const body = {
      title: f.get('title'),
      description: f.get('description'),
      requestor: f.get('requestor'),
      repository: f.get('repository'),
      priority: f.get('priority'),
      source: 'web',
      acceptance_criteria: String(f.get('criteria'))
        .split('\n')
        .map((s) => s.trim())
        .filter(Boolean),
      security_requirements: String(f.get('security')).split('\n').filter(Boolean),
      budget_usd: Number(f.get('budget')),
      deploy: f.get('deploy') === 'on',
      due_date: f.get('due') || null,
      business_context: f.get('context'),
    };
    try {
      const result = await api<{ request_id: string }>('/api/requests', {
        method: 'POST',
        headers: { 'Idempotency-Key': key.current },
        body: JSON.stringify(body),
      });
      created(result.request_id);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };
  return (
    <dialog
      ref={dialog}
      className="intake-dialog"
      onCancel={close}
      onClick={(e) => {
        if (e.target === e.currentTarget) close();
      }}
    >
      <div className="modal-heading">
        <div>
          <p className="eyebrow">INTAKE / NEW WORK</p>
          <h2>What should we build?</h2>
          <p>Define the outcome. The factory assembles the plan.</p>
        </div>
        <button className="icon-button" aria-label="Close new request" onClick={close}>
          <X size={20} />
        </button>
      </div>
      <form onSubmit={submit}>
        {error && <ErrorBox error={error} />}
        <label>
          Request title
          <input
            name="title"
            placeholder="e.g. Add export to the customer activity feed"
            required
            minLength={5}
            maxLength={180}
            autoFocus
          />
        </label>
        <label>
          Description
          <textarea
            name="description"
            placeholder="Describe the problem, expected behavior, and relevant context."
            required
            minLength={20}
            maxLength={12000}
            rows={3}
          />
        </label>
        <div className="form-grid">
          <label>
            Repository
            <select name="repository">
              {config.repositories.map((r) => (
                <option key={r}>{r}</option>
              ))}
            </select>
          </label>
          <label>
            Requested by
            <input name="requestor" defaultValue={config.actor} required minLength={2} />
          </label>
          <label>
            Priority
            <select name="priority" defaultValue="normal">
              <option value="low">Low</option>
              <option value="normal">Normal</option>
              <option value="high">High</option>
              <option value="critical">Critical</option>
            </select>
          </label>
          <label>
            Due date
            <input name="due" type="date" />
          </label>
        </div>
        <label>
          Acceptance criteria
          <textarea
            name="criteria"
            placeholder="One measurable criterion per line"
            maxLength={12000}
            rows={3}
          />
          <small>Leave empty to explore the clarification workflow.</small>
        </label>
        <details className="advanced">
          <summary>Business context, security & budget</summary>
          <label>
            Business context
            <textarea name="context" maxLength={4000} />
          </label>
          <label>
            Security requirements
            <textarea name="security" placeholder="One requirement per line" />
          </label>
          <label>
            Maximum budget (USD)
            <input name="budget" type="number" min={1} max={10000} defaultValue={50} />
          </label>
        </details>
        <label className="checkbox-label">
          <input type="checkbox" name="deploy" disabled={config.draft_only} />
          <span>
            Include deployment<small>{config.draft_only ? 'Unavailable in AI draft mode.' : 'A human must authorize the release.'}</small>
          </span>
        </label>
        <div className="modal-footer">
          <span>
            <ShieldCheck size={16} />
            Policy checks included
          </span>
          <button className="primary" disabled={busy}>
            {busy ? 'Submitting...' : 'Submit request'}
            <ArrowRight size={16} />
          </button>
        </div>
      </form>
    </dialog>
  );
}

function AgentRegistry({ agents }: { agents: Agent[] }) {
  return (
    <div className="registry-grid grid grid-cols-1 lg:grid-cols-2">
      {agents.map((a) => (
        <details className="registry-item" key={a.name}>
          <summary>
            <div className="registry-icon">
              <Cube size={22} />
            </div>
            <div>
              <h2>{a.name.charAt(0).toUpperCase() + a.name.slice(1)} agent</h2>
              <p>{a.responsibilities}</p>
            </div>
            <span className={a.enabled ? 'agent-ready' : 'small-label'}>
              {a.enabled ? 'Enabled' : 'Unavailable'}
            </span>
          </summary>
          <div className="registry-content">
            <h3>Allowed tools</h3>
            <div className="tag-list">
              {a.tools.length ? a.tools.map((t) => <span key={t}>{t}</span>) : <span>No tools</span>}
            </div>
            <h3>Permissions</h3>
            <p>{a.permissions.join(', ') || 'No external permissions'}</p>
            <h3>Contracts & execution limits</h3>
            <p>
              {a.input_contract} → {a.output_contract}
            </p>
            <p>
              {a.timeout_seconds}s timeout · {a.max_attempts} attempts · {a.max_tokens.toLocaleString()}{' '}
              tokens
            </p>
            <p className="helper">
              Prompt {a.prompt_version} · {a.model_requirements}
            </p>
          </div>
        </details>
      ))}
    </div>
  );
}

function ActivityLog({ works, open }: { works: Work[]; open: (id: string) => void }) {
  const [events, setEvents] = useState<(Detail['events'][number] & { requestId: string; title: string })[]>(
      [],
    ),
    [error, setError] = useState('');
  useEffect(() => {
    let cancelled = false;
    void Promise.all(works.slice(0, 30).map((w) => api<Detail>(`/api/requests/${w.id}`)))
      .then((rows) => {
        if (!cancelled)
          setEvents(
            rows
              .flatMap((d) => d.events.map((e) => ({ ...e, requestId: d.id, title: d.data.title })))
              .sort((a, b) => b.id - a.id)
              .slice(0, 100),
          );
      })
      .catch((e) => {
        if (!cancelled) setError(e.message);
      });
    return () => {
      cancelled = true;
    };
  }, [works]);
  return (
    <section className="content-panel">
      <h2>Recent events</h2>
      <p className="subtitle">Latest 100 events from the 30 most recent visible requests.</p>
      {error && <ErrorBox error={error} />}
      <div className="timeline">
        {events.map((e) => (
          <div className="timeline-event" key={e.id}>
            <span className="timeline-dot" />
            <div>
              <button className="text-button" onClick={() => open(e.requestId)}>
                {e.title}
                <ArrowUpRight size={14} />
              </button>
              <small>{e.type}</small>
              <p>{e.message}</p>
            </div>
            <time>{date(e.timestamp).toLocaleTimeString()}</time>
          </div>
        ))}
      </div>
      {!events.length && (
        <div className="empty-state compact">
          <Terminal size={28} />
          <h3>No events recorded yet</h3>
        </div>
      )}
    </section>
  );
}

createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
