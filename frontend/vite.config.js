import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
    plugins: [vue()],
    server: {
        proxy: {
            // ws: true permite proxear también WebSocket upgrades en /api/*
            '/api': { target: 'http://localhost:8000', changeOrigin: true, ws: true }
        }
    }
})
