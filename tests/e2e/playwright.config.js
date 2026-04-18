// @ts-check
const { defineConfig, devices } = require('@playwright/test');

module.exports = defineConfig({
  testDir: '.',
  timeout: 40_000,
  retries: 1,
  reporter: [['list']],
  use: {
    baseURL: 'http://127.0.0.1:18888',
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
