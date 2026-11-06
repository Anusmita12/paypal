import { defineConfig, devices } from "@playwright/test";

// Offline end-to-end: real UI, API, Postgres and domain code; fixture AI and fixture PayPal.
// Uses its own database (tradepilot_e2e) and the built SPA served by FastAPI on one origin.
const PORT = 8010;
const env = {
  DATABASE_URL: "postgresql+psycopg://tradepilot:local-dev-only@localhost:5432/tradepilot_e2e",
  APP_ENV: "test",
  AI_MODE: "fixture",
  PAYMENT_MODE: "fixture",
  CHANNEL3_MODE: "fixture",
  ENABLE_SCENARIOS: "true",
  PUBLIC_BASE_URL: `http://localhost:${PORT}`,
  ALLOWED_ORIGINS: `http://localhost:${PORT}`,
  MAX_SESSIONS_PER_IP_PER_HOUR: "1000",
};

export default defineConfig({
  testDir: "e2e",
  timeout: 60_000,
  expect: { timeout: 10_000 },
  reporter: [["list"]],
  use: { baseURL: `http://localhost:${PORT}`, trace: "retain-on-failure" },
  projects: [
    { name: "desktop", use: { ...devices["Desktop Chrome"] }, testIgnore: /mobile/ },
    { name: "mobile", use: { ...devices["Pixel 7"] }, testMatch: /mobile/ },
  ],
  webServer: {
    command: `npm run build && cd ../backend && uv run python -m app.cli migrate && uv run uvicorn app.main:app --port ${PORT}`,
    url: `http://localhost:${PORT}/api/health`,
    env,
    timeout: 120_000,
    reuseExistingServer: false,
  },
});
