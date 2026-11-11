import path from 'node:path'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import { defineConfig } from 'vite'

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      '@': path.resolve(import.meta.dirname, './src'),
    },
  },
  server: {
    proxy: {
      '/api': 'http://127.0.0.1:5100',
      '/login': 'http://127.0.0.1:5100',
      '/logout': 'http://127.0.0.1:5100',
      '/favicon.svg': 'http://127.0.0.1:5100',
    },
  },
})
