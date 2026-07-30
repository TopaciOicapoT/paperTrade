/**
 * useBot.js — estado global del bot compartido entre todos los componentes.
 * WebSocket para actualizaciones en tiempo real + helpers para llamadas REST.
 */
import { reactive, ref, readonly } from 'vue'

// ── Estado reactivo compartido ────────────────────────────────────────────────

const state = reactive({
    balance_usdt: null,
    balance_peak: null,
    initial_balance: null,
    pnl_usdt: null,
    pnl_pct: null,
    drawdown_pct: null,
    trading_halted: false,
    stop_requested: false,
    news_paused: false,
    news_paused_until: null,
    open_positions: 0,
    total_trades: 0,
    wins: 0,
    losses: 0,
    manual_closes: 0,
    win_rate: 0,
    leverage: 1,
    futures_enabled: false,
    mode: '',
    symbols: [],
    open_orders: {},
})

const history = ref([])
const historyTotal = ref(0)
const connected = ref(false)
const lastError = ref(null)

// ── WebSocket ─────────────────────────────────────────────────────────────────

let ws = null
let reconnectTimer = null

function connectWS() {
    const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
    const url = `${proto}//${window.location.host}/api/ws`
    ws = new WebSocket(url)

    ws.onopen = () => {
        connected.value = true
        lastError.value = null
        if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null }
    }

    ws.onmessage = (event) => {
        try {
            const data = JSON.parse(event.data)
            Object.assign(state, data)
        } catch { /* ignorar mensajes malformados */ }
    }

    ws.onclose = () => {
        connected.value = false
        // Reconectar tras 3 segundos
        reconnectTimer = setTimeout(connectWS, 3000)
    }

    ws.onerror = (err) => {
        lastError.value = 'Error de conexión WebSocket'
        ws.close()
    }
}

connectWS()

// ── REST helpers ──────────────────────────────────────────────────────────────

async function _fetch(path, options = {}) {
    const res = await fetch(path, {
        headers: { 'Content-Type': 'application/json' },
        ...options,
    })
    const body = await res.json()
    if (!res.ok) throw new Error(body.detail ?? `HTTP ${res.status}`)
    return body
}

async function closePosition(symbol) {
    const slug = symbol.replace('/', '-')
    return _fetch(`/api/positions/${slug}/close`, { method: 'POST' })
}

async function emergencyStop() {
    return _fetch('/api/emergency', { method: 'POST' })
}

async function resumeTrading() {
    return _fetch('/api/resume', { method: 'POST' })
}

async function fetchHistory(limit = 50, offset = 0) {
    const data = await _fetch(`/api/history?limit=${limit}&offset=${offset}`)
    history.value = data.trades
    historyTotal.value = data.total
    return data
}

async function fetchStats() {
    return _fetch('/api/history/stats')
}

export function useBot() {
    return {
        state: readonly(state),
        history,
        historyTotal,
        connected,
        lastError,
        closePosition,
        emergencyStop,
        resumeTrading,
        fetchHistory,
        fetchStats,
    }
}
