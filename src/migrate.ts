import { loadConfig } from './config.ts';
import { createPool, migrate } from './db.ts';

const pool = createPool(loadConfig().databaseUrl);
try {
  await migrate(pool);
  console.log('Migrations applied');
} finally {
  await pool.end();
}
