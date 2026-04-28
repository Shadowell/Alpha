// @ts-check
const { defineConfig, devices } = require('@playwright/test');

module.exports = defineConfig({
  testDir: '.',
  timeout: 40_000,
  retries: 1,
  reporter: [['list']],
  use: {
    baseURL: process.env.ALPHA_TEST_BASE_URL || 'http://127.0.0.1:18890',
    headless: true,
    actionTimeout: 12_000,
    navigationTimeout: 30_000,
    screenshot: 'only-on-failure',
    video: 'off',
    launchOptions: {
      args: ['--no-proxy-server'],
    },
  },
  projects: [
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'] },
    },
  ],
});
