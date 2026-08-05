import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

export default defineConfig({
  plugins: [react()],
  build: {
    rollupOptions: {
      output: {
        manualChunks(id) {
          if (!id.includes("node_modules")) return undefined;
          if (id.includes("@auth0")) return "auth";
          if (id.includes("@base-ui") || id.includes("@floating-ui")) return "ui-primitives";
          if (id.includes("motion") || id.includes("framer-motion")) return "motion";
          if (id.includes("lucide-react")) return "icons";
          if (/node_modules\/(react|react-dom|scheduler)\//.test(id)) return "react";
          return undefined;
        },
      },
    },
  },
});
