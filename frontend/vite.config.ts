import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

const backend = "http://127.0.0.1:5001";

export default defineConfig({
  plugins: [react()],
  server: { host: "0.0.0.0", proxy: { "/api": backend } },
});
