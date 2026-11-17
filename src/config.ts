export interface Config {
  databaseUrl: string;
  host: string;
  port: number;
  workerConcurrency: number;
}

export function loadConfig(env: NodeJS.ProcessEnv = process.env): Config {
  const databaseUrl = env.DATABASE_URL;
  if (!databaseUrl) {
    throw new Error('DATABASE_URL is not set. Example: postgres://user:password@localhost:5432/factory');
  }
  const port = Number(env.PORT ?? 3000);
  const workerConcurrency = Number(env.WORKER_CONCURRENCY ?? 4);
  if (!Number.isInteger(port) || port < 1 || port > 65535) throw new Error('PORT is not a valid port');
  if (!Number.isInteger(workerConcurrency) || workerConcurrency < 1) {
    throw new Error('WORKER_CONCURRENCY must be a positive integer');
  }
  return {
    databaseUrl,
    // The API has no authentication yet, so it listens on this machine only by default.
    host: env.HOST ?? '127.0.0.1',
    port,
    workerConcurrency,
  };
}
