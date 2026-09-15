import react from '@vitejs/plugin-react';
import { defineConfig } from 'vite';

// In compose this points at the backend service; locally it falls back to the
// host port. Everything is proxied so the browser only ever sees one origin:
// no CORS, no mixed cookie domains, and the websocket URL can be derived from
// window.location instead of being baked in at build time.
const backendOrigin = process.env.BACKEND_ORIGIN ?? 'http://localhost:8000';

// Configurable so the dashboard can coexist with other Vite apps on the same
// machine. strictPort stays off: if 5173 is taken, Vite moves up a port and
// prints the real URL rather than refusing to start.
const port = Number(process.env.PORT ?? 5173);

export default defineConfig({
  plugins: [react()],
  server: {
    host: true,
    port,
    strictPort: false,
    // Bind-mounted source on macOS/Windows does not emit inotify events.
    watch:
      process.env.CHOKIDAR_USEPOLLING === 'true' ? { usePolling: true, interval: 300 } : undefined,
    proxy: {
      '/api': { target: backendOrigin, changeOrigin: true },
      '/webhook': { target: backendOrigin, changeOrigin: true },
      // ws:true is what makes the proxy issue an Upgrade instead of a 404.
      '/ws': { target: backendOrigin, ws: true, changeOrigin: true },
    },
  },
  build: { outDir: 'dist', sourcemap: true },
});
