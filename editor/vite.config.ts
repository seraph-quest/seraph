import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import path from "path";
import { fileURLToPath } from "url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const frontendPublic = path.resolve(__dirname, "../frontend/public");

export default defineConfig({
  plugins: [react()],
  publicDir: frontendPublic,
  server: {
    port: 3001,
    fs: { allow: ["..", frontendPublic] },
  },
});
