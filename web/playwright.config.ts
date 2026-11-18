import { defineConfig } from '@playwright/test';
export default defineConfig({
  testDir: './tests',
  timeout: 45000,
  fullyParallel: false,
  workers: 1,
  use: {
    baseURL: 'http://127.0.0.1:8010',
    viewport: { width: 1512, height: 1100 },
    trace: 'retain-on-failure',
  },
  reporter: 'list',
  expect: { timeout: 15000 },
  webServer: {
    command: `${process.platform === 'win32' ? '".venv\\Scripts\\python.exe"' : 'python'} -m uvicorn factory.api:app --host 127.0.0.1 --port 8010 --no-access-log`,
    cwd: '..',
    url: 'http://127.0.0.1:8010/healthz',
    reuseExistingServer: false,
    timeout: 30000,
    env: {
      FACTORY_MODE: 'demo',
      FACTORY_AUTH_MODE: 'demo',
      FACTORY_DATABASE_URL: `sqlite:///./data/browser-tests-${process.pid}.db`,
      FACTORY_DEMO_DELAY: '0.03',
    },
  },
});
