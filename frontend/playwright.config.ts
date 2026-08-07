import { defineConfig, devices } from '@playwright/test'

const python = process.env.E2E_PYTHON ?? (process.platform === 'win32' ? '../.venv/Scripts/python.exe' : 'python')
const chromiumOverride = process.env.E2E_BROWSER_PATH
  ? { launchOptions: { executablePath: process.env.E2E_BROWSER_PATH } }
  : {}

export default defineConfig({
  testDir: './e2e',
  fullyParallel: false,
  workers: 1,
  retries: process.env.CI ? 2 : 0,
  reporter: [['list'], ['html', { open: 'never' }]],
  use: {
    trace: 'retain-on-failure',
  },
  webServer: [
    {
      command: `"${python}" ../scripts/run-e2e-backend.py`,
      url: 'http://127.0.0.1:8081/api/health/live',
      reuseExistingServer: !process.env.CI,
      timeout: 30_000,
    },
    {
      command: 'npm run preview:demo',
      url: 'http://127.0.0.1:4173',
      reuseExistingServer: !process.env.CI,
      timeout: 30_000,
    },
  ],
  projects: [
    {
      name: 'private-chromium',
      testMatch: /private\.spec\.ts/,
      use: { ...devices['Desktop Chrome'], ...chromiumOverride, baseURL: 'http://127.0.0.1:8081' },
    },
    {
      name: 'private-firefox',
      testMatch: /private\.spec\.ts/,
      use: { ...devices['Desktop Firefox'], baseURL: 'http://127.0.0.1:8081' },
    },
    {
      name: 'private-webkit',
      testMatch: /private\.spec\.ts/,
      use: { ...devices['Desktop Safari'], baseURL: 'http://127.0.0.1:8081' },
    },
    {
      name: 'private-mobile-320',
      testMatch: /private\.spec\.ts/,
      use: {
        ...devices['Pixel 7'],
        ...chromiumOverride,
        viewport: { width: 320, height: 760 },
        baseURL: 'http://127.0.0.1:8081',
      },
    },
    {
      name: 'demo-chromium',
      testMatch: /demo\.spec\.ts/,
      use: { ...devices['Desktop Chrome'], ...chromiumOverride, baseURL: 'http://127.0.0.1:4173' },
    },
    {
      name: 'demo-mobile',
      testMatch: /demo\.spec\.ts/,
      use: { ...devices['Pixel 7'], ...chromiumOverride, baseURL: 'http://127.0.0.1:4173' },
    },
  ],
})
