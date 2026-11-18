import { useState } from 'react';
import { ArrowRight, CheckCircle, GitBranch, Stack } from '@phosphor-icons/react';
import type { Config } from './types';

const stages = [
  { name: 'Intake', title: 'Save the request before work starts', text: 'A ticket creator supplies the outcome, context, repository label, acceptance criteria and budget. The API saves the request and a queued start command together, so accepted work is recorded before a background agent starts.', output: 'A tracking ID, saved request and queued workflow.' },
  { name: 'Triage', title: 'Understand scope and missing information', text: 'Claude classifies the request, assesses complexity and identifies risks. If the request needs clarification, the workflow pauses for human input. This is a real model call in Anthropic mode.', output: 'A structured scope assessment, suggested specialists and any clarification question.' },
  { name: 'Plan', title: 'Turn the request into a dependency graph', text: 'Claude proposes specialist tasks. Application policy validates the graph and adds the required test, review, security, documentation and validation stages. The model cannot grant itself deployment permissions.', output: 'An execution graph showing which tasks must finish before others can start.' },
  { name: 'Draft', title: 'Specialists work with shared context', text: 'Each agent is a role-specific prompt and output contract sent to the configured model. It receives the request and completed upstream artifacts. The supervisor starts up to four ready tasks together; tasks with unfinished dependencies wait.', output: 'Proposed code, design notes, documentation or analysis, each saved as an unexecuted draft.' },
  { name: 'Review', title: 'Inspect the work and make a human decision', text: 'The test, security and review agents produce assessments of the drafts. They do not execute tests or scanners in draft mode. When human draft review is enabled, the workflow waits for an Approver or Admin using a different account from the creator.', output: 'Review artifacts, unverified acceptance criteria and an auditable human approval or rejection.' },
  { name: 'Outcome', title: 'A draft is ready for your next step', text: 'Approved draft workflows finish as “Draft ready.” Declined drafts finish as “Declined.” Human approval records acceptance of the proposed work; it does not turn a draft into tested, deployed software.', output: 'Saved artifacts, agent attempts, token usage, estimated cost and the decision history.' },
];

export function HowItWorks({ config }: { config: Config | null }) {
  const [stage, setStage] = useState(0);
  const temporal = config?.mode === 'temporal';
  const cloud = config?.tenant === 'sffactorygpt';
  return <article className="how-page">
    <header className="page-heading"><div>
      <p className="eyebrow">A GUIDE TO YOUR FACTORY</p>
      <h1>How it works</h1>
      <p className="subtitle">Follow a request from an idea to agent drafts and a human decision.</p>
    </div></header>

    <section className="how-status" aria-label="Current runtime">
      <div><span className="how-kicker">THIS WORKSPACE</span><h2>{config?.mode === 'anthropic' ? 'Real Claude calls. Draft outputs.' : config?.mode === 'demo' ? 'Simulated agents. No model calls.' : temporal ? 'Temporal workflow mode' : 'Connecting to the workspace'}</h2>
        <p>{config?.mode === 'anthropic' ? `The agents use ${config.model}. They produce proposals from the context you provide; repository tools, test runners and deployments are not connected.` : 'The selected runtime determines whether work is simulated, drafted by a model, or sent through the execution gateway.'}</p></div>
      <dl><div><dt>Orchestration</dt><dd>{temporal ? 'Temporal workflows' : 'Application supervisor'}</dd></div><div><dt>Hosting</dt><dd>{cloud ? 'Google Cloud Run' : 'This application server'}</dd></div><div><dt>Records</dt><dd>{cloud ? 'Cloud SQL PostgreSQL' : 'Configured application database'}</dd></div><div><dt>Human draft review</dt><dd>{config?.draft_review_required ? 'Required for new drafts' : 'Not required'}</dd></div></dl>
    </section>

    <section className="how-section" aria-labelledby="journey-title">
      <p className="eyebrow">01 / REQUEST JOURNEY</p><h2 id="journey-title">What happens after you create a ticket?</h2>
      <p>Select a stage to see what it does. This walkthrough describes the Anthropic draft workflow.</p>
      <div className="how-steps" role="tablist" aria-label="Workflow stages">{stages.map((item, index) => <button key={item.name} role="tab" id={`stage-${index}`} aria-selected={stage === index} aria-controls="stage-detail" onClick={() => setStage(index)}><span>{String(index + 1).padStart(2, '0')}</span>{item.name}{index < stages.length - 1 && <ArrowRight size={14} aria-hidden="true" />}</button>)}</div>
      <div className="how-stage-detail" role="tabpanel" id="stage-detail" aria-labelledby={`stage-${stage}`}><h3>{stages[stage].title}</h3><p>{stages[stage].text}</p><div><CheckCircle size={18} /><span><strong>What you see:</strong> {stages[stage].output}</span></div></div>
    </section>

    <section className="how-section"><p className="eyebrow">02 / THE AGENTS</p><h2>Different roles, one coordinated workflow</h2>
      <p>An agent is a specialized model interaction with a defined job and a structured response. These are not separate people or independently trained models. In this draft workspace, the roles use the same configured Claude model.</p>
      <div className="how-grid">
        <div className="how-card"><h3>Triage & planner</h3><p>Assess the request, ask for missing context and propose the work breakdown. The application validates their outputs before proceeding.</p></div>
        <div className="how-card"><h3>Specialists</h3><p>Requirements, architecture, frontend, backend, database and documentation roles are selected according to the request. Not every ticket runs every agent.</p></div>
        <div className="how-card"><h3>Test, security & review</h3><p>Draft test plans and assessments using the supplied context and generated artifacts. A convincing review is not proof that code ran or passed a scanner.</p></div>
        <div className="how-card"><h3>Supervisor & validator</h3><p>The supervisor is application code: it enforces dependencies, budgets and human gates. The validation agent summarizes criteria; the server keeps draft evidence marked unverified.</p></div>
      </div>
      <p className="how-footnote">Use <a href="#agents">Agent registry</a> to inspect each role, and a request’s Agent executions tab to see which agents actually ran.</p>
    </section>

    <section className="how-section"><p className="eyebrow">03 / TEMPORAL</p><h2>{temporal ? 'How Temporal coordinates execution' : 'Temporal is implemented, but is not running here'}</h2>
      <p>{temporal ? 'This workspace is configured for the Temporal runtime.' : 'The current cloud draft deployment uses a background dispatcher inside the application. A PostgreSQL lock selects one dispatcher during overlapping deployments. There is no active Temporal Cloud workflow behind these draft requests.'}</p>
      <div className="how-grid">
        <div className="how-card"><GitBranch size={23} /><h3>Workflow: remember the sequence</h3><p>In Temporal mode, the supervisor runs as a durable workflow. Temporal records its event history and replays that history to reconstruct progress after a worker restart, reusing recorded activity results.</p></div>
        <div className="how-card"><Stack size={23} /><h3>Activities: do the outside work</h3><p>Workers poll a task queue and run activities for agent calls and database updates. Timeouts, heartbeats and retry policies handle failures. Activities must tolerate retries without duplicating external effects.</p></div>
      </div>
      <p>Human decisions and cancellations arrive as signals. A waiting workflow can resume when a worker returns. Temporal coordinates the steps; it does not provide the language model or prove that generated code is correct.</p>
      <p>The repository includes local Temporal execution, restart and replay tests. The production Temporal path still needs a live namespace, workers, an execution gateway, repository sandboxes and independent test evidence. Current draft recovery reuses saved outputs, but a crash after a paid call and before saving its result can repeat that charge.</p>
      <p className="how-footnote">Further reading: <a href="https://docs.temporal.io/workflows" target="_blank" rel="noreferrer">Temporal workflows</a> and <a href="https://docs.temporal.io/activities" target="_blank" rel="noreferrer">activities and retries</a>.</p>
    </section>

    <section className="how-section"><p className="eyebrow">04 / DRIVE THE DEMO</p><h2>A role for each part of the story</h2>
      <p>Use <strong>Sign in / Switch account</strong> in the top bar. The presenter supplies the shared demo password; it is not published on this page. These accounts demonstrate role permissions, not separate real-world identities.</p>
      <div className="how-table-wrap"><table className="how-table"><thead><tr><th>User ID</th><th>Role</th><th>What you can demonstrate</th></tr></thead><tbody>
        <tr><td><code>creator</code></td><td>Ticket Creator</td><td>Create a request and watch agents work; cannot approve or cancel.</td></tr>
        <tr><td><code>approver</code></td><td>Approver</td><td>Inspect drafts and accept or decline a waiting review; cannot create requests.</td></tr>
        <tr><td><code>admin</code></td><td>Admin</td><td>Create, review and cancel work. Cannot approve a request submitted by the same account.</td></tr>
        <tr><td><code>viewer</code></td><td>Viewer</td><td>Browse the workflow, artifacts and timeline with no write controls.</td></tr>
      </tbody></table></div>
      <ol className="how-demo-list"><li>Sign in as <strong>creator</strong>. Request a short document or a proposed code change with one clear acceptance criterion.</li><li>Open the request. Explain the execution graph, then inspect the agent outputs, recorded tokens and cost.</li><li>When it reaches human review, switch to <strong>approver</strong>. Open Human approvals, inspect the artifacts and record an approval or rejection with a note.</li><li>Switch to <strong>viewer</strong> to show the outcome and audit trail. Use <strong>admin</strong> on a separate active request to demonstrate cancellation.</li></ol>
      <p className="how-footnote">New tickets make real paid API calls in Anthropic mode. A small documentation request is a useful demo; timing and cost vary. The earlier completed smoke-test request predates the human review gate.</p>
    </section>

    <section className="how-section"><p className="eyebrow">05 / DATA, COST & TRUST</p><h2>What the screen is telling you</h2>
      <div className="how-grid"><div className="how-card"><h3>Persistent records</h3><p>Cloud SQL stores requests, tasks, artifacts, decisions and events. Closing your browser does not stop background work. Secret Manager supplies credentials to the server; the browser never receives the Anthropic key.</p></div><div className="how-card"><h3>Usage and budgets</h3><p>Token counts come from Anthropic responses. Dollar amounts are estimates from those counts and model rates. The supervisor checks the budget before starting more work. Cancellation cannot undo calls already billed.</p></div></div>
      <p><strong>Public visibility:</strong> this workspace’s requests, drafts and history are viewable by anyone with the URL. Use non-sensitive demo material. The shared demo password grants access to all four roles; anyone who knows it can act as Admin. Sign out after presenting.</p>
      <p><strong>“Draft ready” means proposed work:</strong> no repository was modified, no tests or security scans were executed, and no software was released. Human approval does not change that boundary.</p>
    </section>
  </article>;
}
