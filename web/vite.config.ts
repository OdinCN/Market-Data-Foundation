import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  test: {
    environment: "jsdom",
    setupFiles: ["./src/offlineTestSetup.ts"],
    // ASGI fixtures spawn real Python processes; bound cold-start contention.
    pool: "threads",
    maxWorkers: 1,
    fileParallelism: false,
    testTimeout: 15000,
    hookTimeout: 15000,
  },
});
