/** API and WebSocket URL configuration — injected at build time via Vite env vars */
export const API_URL = import.meta.env.VITE_API_URL ?? 'http://localhost:3001';
export const WS_URL = import.meta.env.VITE_WS_URL ?? 'ws://localhost:3001/ws';
