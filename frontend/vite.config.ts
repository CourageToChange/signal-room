import fs from 'node:fs'
import path from 'node:path'
import { defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'

export default defineConfig(({ mode }) => {
  const demo = mode === 'demo'
  const outDir = demo ? 'dist-demo' : 'dist-private'
  return {
    plugins: [
      react(),
      {
        name: 'signal-room-entry-isolation',
        closeBundle() {
          fs.mkdirSync(outDir, { recursive: true })
          if (demo) {
            const builtDemo = path.join(outDir, 'demo.html')
            if (fs.existsSync(builtDemo)) fs.renameSync(builtDemo, path.join(outDir, 'index.html'))
          }
          const connect = demo ? "connect-src 'none'" : "connect-src 'self'"
          fs.writeFileSync(
            path.join(outDir, '_headers'),
            `/*\n  Content-Security-Policy: default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; ${connect}; font-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'\n  X-Content-Type-Options: nosniff\n  X-Frame-Options: DENY\n  Referrer-Policy: no-referrer\n  Permissions-Policy: camera=(), microphone=(), geolocation=()\n  Cache-Control: no-cache\n/assets/*\n  Cache-Control: public, max-age=31536000, immutable\n`,
            'utf8',
          )
          if (demo) {
            fs.copyFileSync(
              path.resolve('../docs/screenshots/pressure-drop-desktop-incident.png'),
              path.join(outDir, 'og-signal-room.png'),
            )
          }
        },
      },
    ],
    build: {
      outDir,
      emptyOutDir: true,
      sourcemap: false,
      target: 'es2022',
      rollupOptions: {
        input: path.resolve(demo ? 'demo.html' : 'index.html'),
      },
    },
    server: {
      host: '127.0.0.1',
      port: 5173,
      proxy: { '/api': 'http://127.0.0.1:8080' },
    },
    test: {
      environment: 'jsdom',
      setupFiles: ['./src/test/setup.ts'],
      include: ['src/**/*.test.{ts,tsx}'],
      exclude: ['e2e/**', 'node_modules/**', 'dist*/**'],
      coverage: {
        provider: 'v8',
        reporter: ['text', 'html'],
        include: ['src/**/*.{ts,tsx}'],
        exclude: ['src/private-main.tsx', 'src/demo-main.tsx', 'src/test/**', 'src/types.ts'],
        thresholds: { lines: 85, branches: 85 },
      },
    },
  }
})
