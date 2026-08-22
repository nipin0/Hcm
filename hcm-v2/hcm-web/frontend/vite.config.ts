import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import tailwindcss from '@tailwindcss/vite';

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    port: 3000,
    proxy: {
      '/api': {
        target: 'http://localhost:8000',
        changeOrigin: true,
      },
      '/ws': {
        target: 'ws://localhost:8000',
        ws: true,
      },
    },
  },
  optimizeDeps: {
    include: ['@mui/icons-material', '@mui/material', '@emotion/react', '@emotion/styled'],
  },
  build: {
    commonjsOptions: {
      transformMixedEsModules: true,
      exclude: ['@mui/icons-material/**', '@mui/material/**', '@emotion/**'],
    },
  },
});
