import { buildServer } from './api/server.ts';
import { loadConfig } from './config.ts';
import { createPool, migrate } from './db.ts';
import { defaultEngineOptions, type EngineOptions } from './engine/engine.ts';
import { startWorker } from './engine/worker.ts';
import { humanApproval } from './handlers/human-approval.ts';
import { onTaskDone } from './policy.ts';
import type { TaskHandler } from './types.ts';

const config = loadConfig();
const pool = createPool(config.databaseUrl);
await migrate(pool);

const options: EngineOptions = { ...defaultEngineOptions, onTaskDone };

// Agent and CI handlers (triage, implement, ci, review, verify) are not built
// yet. Until they are registered here, tasks of those types fail on first run.
const handlers: Record<string, TaskHandler> = {
  human_approval: humanApproval,
};

const worker = await startWorker({
  pool,
  options,
  handlers,
  concurrency: config.workerConcurrency,
});
const server = buildServer({ pool, options });
await server.listen({ host: config.host, port: config.port });
console.log(`Software factory listening on http://${config.host}:${config.port}`);
console.log(`Task types with a handler: ${Object.keys(handlers).join(', ')}`);

let stopping = false;
async function shutdown(signal: string): Promise<void> {
  if (stopping) return;
  stopping = true;
  console.log(`${signal} received, shutting down`);
  await server.close();
  await worker.stop();
  await pool.end();
}
process.on('SIGINT', () => void shutdown('SIGINT'));
process.on('SIGTERM', () => void shutdown('SIGTERM'));
