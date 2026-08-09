<script setup>
import { ref, computed, onMounted, onUnmounted } from "vue";

const emit = defineEmits(["simulationDone"]);

// ── Formulario ────────────────────────────────────────────────────────────────
const availableSymbols = ref([]);
const botConfig = ref(null);
const dropdownSym = ref("");
const dropdownStrat = ref("breakout"); // estrategia a añadir
const selectedSymbols = ref([]); // [{ id, symbol, strategy, filters }]
let _nextId = 1;
const capital = ref(100);
const maxPositions = ref(3);
const years = ref(10);
const leverage = ref(3);
const periodMode = ref("years"); // "years" | "range"
const dateFrom = ref("");
const dateTo = ref("");

// Símbolos únicos para el selector (filtra los ya añadidos con esa estrategia)
const selected = computed(() => [
  ...new Set(selectedSymbols.value.map((s) => s.symbol)),
]);

function _defaultFilters(sp = {}, strategy = "breakout") {
  if (strategy === "retest") {
    return {
      retest_min_move_pct: sp.retest_min_move_pct ?? 0.5,
      retest_tolerance_pct: sp.retest_tolerance_pct ?? 0.35,
      retest_pullback_vol_max: sp.retest_pullback_vol_max ?? 1.5,
    };
  }
  if (strategy === "bounce") {
    return {};
  }
  // breakout
  return {
    f1: {
      enabled: "momentum_q3_block" in sp,
      lo: sp.momentum_q3_block?.[0] ?? 0.3,
      hi: sp.momentum_q3_block?.[1] ?? 1.6,
    },
    f2b: {
      enabled: "session_block_hours" in sp,
      lo: sp.session_block_hours?.[0] ?? 8,
      hi: sp.session_block_hours?.[1] ?? 14,
    },
    f3: {
      enabled: "usdt_norm_block_range" in sp,
      lo: sp.usdt_norm_block_range?.[0] ?? 2.1,
      hi: sp.usdt_norm_block_range?.[1] ?? 2.7,
    },
    f4: {
      enabled: "rsi_overbought_block" in sp,
      threshold: sp.rsi_overbought_block ?? 70,
    },
    fr: { enabled: sp.failed_retest_filter !== false },
    volmax: {
      enabled: "volume_trigger_ratio_max" in sp,
      max: sp.volume_trigger_ratio_max ?? 2.8,
    },
  };
}

function addSymbol() {
  const sym = dropdownSym.value;
  const strat = dropdownStrat.value;
  if (!sym || !strat) return;
  // Prevenir duplicado (mismo símbolo + misma estrategia)
  if (
    selectedSymbols.value.find((s) => s.symbol === sym && s.strategy === strat)
  )
    return;
  if (selectedSymbols.value.length >= 16) return;
  const sp = botConfig.value?.symbol_params?.[sym] ?? {};
  selectedSymbols.value.push({
    id: _nextId++,
    symbol: sym,
    strategy: strat,
    filters: _defaultFilters(sp, strat),
  });
  dropdownSym.value = "";
}

function removeEntry(id) {
  selectedSymbols.value = selectedSymbols.value.filter((s) => s.id !== id);
}

// Construye la lista de entries para la API: [{symbol, strategy, ...params}]
function buildStrategyEntries() {
  return selectedSymbols.value.map(({ symbol, strategy, filters }) => {
    const entry = { symbol, strategy };
    if (strategy === "breakout") {
      if (filters.f1?.enabled)
        entry.momentum_q3_block = [
          Number(filters.f1.lo),
          Number(filters.f1.hi),
        ];
      if (filters.f2b?.enabled)
        entry.session_block_hours = [
          Number(filters.f2b.lo),
          Number(filters.f2b.hi),
        ];
      if (filters.f3?.enabled)
        entry.usdt_norm_block_range = [
          Number(filters.f3.lo),
          Number(filters.f3.hi),
        ];
      if (filters.f4?.enabled)
        entry.rsi_overbought_block = Number(filters.f4.threshold);
      if (!filters.fr?.enabled) entry.failed_retest_filter = false;
      if (filters.volmax?.enabled)
        entry.volume_trigger_ratio_max = Number(filters.volmax.max);
    } else if (strategy === "retest") {
      entry.retest_min_move_pct = Number(filters.retest_min_move_pct);
      entry.retest_tolerance_pct = Number(filters.retest_tolerance_pct);
      entry.retest_pullback_vol_max = Number(filters.retest_pullback_vol_max);
    }
    return entry;
  });
}

// Etiqueta corta para pestañas de resultado
function tabLabel(key) {
  // "ADA/USDT · breakout" → "ADA · Break"  |  "ADA/USDT" → "ADA"
  const parts = key.split(" · ");
  const base = parts[0].split("/")[0];
  const stMap = { breakout: "Break", retest: "Retest", bounce: "Bounce" };
  return parts.length > 1 ? `${base} · ${stMap[parts[1]] ?? parts[1]}` : base;
}

async function loadSymbols() {
  const [symRes, cfgRes] = await Promise.all([
    fetch("/api/lab/symbols"),
    fetch("/api/config"),
  ]);
  const symData = await symRes.json();
  botConfig.value = await cfgRes.json();
  availableSymbols.value = symData.available ?? [];
  // Pre-seleccionar los símbolos activos del bot con sus filtros configurados
  for (const sym of symData.active_symbols ?? []) {
    const sp = botConfig.value?.symbol_params?.[sym] ?? {};
    // Añadir una entrada por cada estrategia configurada en el símbolo
    const strats = sp.strategies ?? ["breakout"];
    for (const strat of strats) {
      if (
        selectedSymbols.value.find(
          (s) => s.symbol === sym && s.strategy === strat,
        )
      )
        continue;
      selectedSymbols.value.push({
        id: _nextId++,
        symbol: sym,
        strategy: strat,
        filters: _defaultFilters(sp, strat),
      });
    }
  }
}
loadSymbols();

// ── Job ───────────────────────────────────────────────────────────────────────
const jobId = ref(null);
const jobStatus = ref(null); // pending | running | done | error | cancelled
const progress = ref([]);
const result = ref(null);
const errorMsg = ref(null);
const actualYears = ref(null);
const capped = ref(false);
let pollTimer = null;

const JOB_STORAGE_KEY = "lab_active_job";

// Recuperar job en curso si el usuario refrescó la página mientras simulaba
onMounted(async () => {
  const saved = localStorage.getItem(JOB_STORAGE_KEY);
  if (!saved) return;
  try {
    const { id } = JSON.parse(saved);
    const res = await fetch(`/api/lab/jobs/${id}`);
    if (!res.ok) {
      localStorage.removeItem(JOB_STORAGE_KEY);
      return;
    }
    const data = await res.json();
    jobId.value = id;
    jobStatus.value = data.status;
    progress.value = data.progress ?? [];
    if (data.status === "done") {
      result.value = data.result;
      localStorage.removeItem(JOB_STORAGE_KEY);
    } else if (data.status === "error" || data.status === "cancelled") {
      errorMsg.value = data.error ?? "Simulación cancelada.";
      localStorage.removeItem(JOB_STORAGE_KEY);
    } else {
      // Todavía corriendo — retomar el polling
      startPolling();
    }
  } catch {
    localStorage.removeItem(JOB_STORAGE_KEY);
  }
});
const activeResultTab = ref("general");
const selectedTrade = ref(null);
const tradeFilter = ref("con"); // 'con' | 'sin'

// ── Modal de cancelación ──────────────────────────────────────────────────────
const showCancelModal = ref(false);

async function simulate() {
  if (!selectedSymbols.value.length) return;
  errorMsg.value = null;
  result.value = null;
  progress.value = [];
  jobStatus.value = "pending";
  const stratEntries = buildStrategyEntries();
  const uniqueSyms = [...new Set(stratEntries.map((e) => e.symbol))];
  const res = await fetch("/api/lab/simulate", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      symbols: uniqueSyms,
      capital: capital.value,
      max_positions: maxPositions.value,
      years: periodMode.value === "years" ? years.value : 1,
      leverage: leverage.value,
      strategy_entries: stratEntries,
      date_from: periodMode.value === "range" ? dateFrom.value || null : null,
      date_to: periodMode.value === "range" ? dateTo.value || null : null,
    }),
  });
  const data = await res.json();
  jobId.value = data.job_id;
  actualYears.value = data.actual_years;
  capped.value = data.capped ?? false;
  // Persistir jobId para sobrevivir un refresco de página
  localStorage.setItem(
    JOB_STORAGE_KEY,
    JSON.stringify({ id: data.job_id, syms: selected.value }),
  );
  startPolling();
}

async function confirmCancel() {
  showCancelModal.value = false;
  if (!jobId.value) return;
  await fetch(`/api/lab/jobs/${jobId.value}/cancel`, { method: "POST" });
  stopPolling();
  jobStatus.value = "cancelled";
  progress.value = []; // limpiar log para que no se muestre el spinner
  errorMsg.value = "Simulación cancelada por el usuario.";
}

function startPolling() {
  stopPolling();
  pollTimer = setInterval(pollJob, 2000);
}
function stopPolling() {
  if (pollTimer) {
    clearInterval(pollTimer);
    pollTimer = null;
  }
}

async function pollJob() {
  if (!jobId.value) return;
  try {
    const res = await fetch(`/api/lab/jobs/${jobId.value}`);
    if (!res.ok) {
      // 404 = job expirado; otro código = error de servidor
      errorMsg.value =
        res.status === 404
          ? "El job ha expirado (simulación muy larga o reinicio del servidor). Vuelve a simular."
          : `Error del servidor (HTTP ${res.status}).`;
      stopPolling();
      jobStatus.value = "error";
      progress.value = [];
      return;
    }
    const data = await res.json();
    jobStatus.value = data.status;
    progress.value = data.progress ?? [];
    if (data.status === "done") {
      result.value = data.result;
      stopPolling();
      localStorage.removeItem(JOB_STORAGE_KEY);
      emit("simulationDone");
    } else if (data.status === "error" || data.status === "cancelled") {
      errorMsg.value = data.error ?? "Simulación cancelada.";
      progress.value = [];
      stopPolling();
      localStorage.removeItem(JOB_STORAGE_KEY);
    }
  } catch (e) {
    // Error de red o de parseo de JSON (respuesta muy grande, timeout, etc.)
    console.error("Poll error:", e);
    errorMsg.value = `Error de conexión durante la simulación: ${e.message}. Verifica la consola del servidor.`;
    stopPolling();
    jobStatus.value = "error";
    progress.value = [];
  }
}

onUnmounted(stopPolling);

const isRunning = computed(() =>
  ["pending", "running"].includes(jobStatus.value),
);

function fmtPct(v) {
  return v != null ? (v >= 0 ? "+" : "") + Number(v).toFixed(1) + "%" : "—";
}
function fmtUsd(v) {
  return v != null ? "$" + Number(v).toFixed(2) : "—";
}
function pc(v) {
  return Number(v) >= 0 ? "positive" : "negative";
}

// ── Helpers para vista de símbolo ─────────────────────────────────────────────
function tradesForTab(which) {
  if (!result.value || activeResultTab.value === "general") return [];
  const src =
    which === "sin" ? result.value.sin_filtros : result.value.con_filtros;
  return src?.trades_por_simbolo?.[activeResultTab.value] ?? [];
}
function symSin(sym) {
  return result.value?.sin_filtros?.por_simbolo?.find((s) => s.symbol === sym);
}
function symCon(sym) {
  return result.value?.con_filtros?.por_simbolo?.find((s) => s.symbol === sym);
}

const FILTER_NAMES = {
  momentum_q3_block: "Trampa de momentum",
  session_block_hours: "Horario restringido",
  usdt_norm_block_range: "Trampa de volumen",
  rsi_overbought_block: "Sobrecompra RSI",
  failed_retest_filter: "Anti-fakeout",
  volume_trigger_ratio_max: "Spike extremo",
};
function activeFilters(sym) {
  const sp = result.value?.params?.symbol_params?.[sym];
  if (!sp) return [];
  return Object.keys(sp)
    .filter((k) => sp[k] != null && sp[k] !== false)
    .map((k) => FILTER_NAMES[k] ?? k);
}
function fmtDate(s) {
  if (!s) return "—";
  return String(s).slice(0, 16).replace("T", " ");
}

// Parsea los mensajes estructurados del backend en objetos para renderizar
const parsedProgress = computed(() => {
  let currentPass = 0;
  return progress.value.map((raw) => {
    if (raw.startsWith("PASS:")) {
      currentPass = parseInt(raw.split(":")[1]);
      return { type: "pass", pass: currentPass };
    }
    if (raw.startsWith("SYM:")) {
      const [, idx, total, sym] = raw.split(":");
      return { type: "sym_start", idx: +idx, total: +total, sym };
    }
    if (raw.startsWith("SYMDONE:")) {
      const [, sym, trades, wr, longs, shorts] = raw.split(":");
      return {
        type: "sym_done",
        sym,
        trades: +trades,
        wr: +wr,
        longs: +longs,
        shorts: +shorts,
      };
    }
    if (raw === "DONE") return { type: "done" };
    if (raw === "FILTERPASS") return { type: "filterpass" };
    if (raw.startsWith("FILTERANAL:")) {
      return { type: "filteranal", sym: raw.split(":")[1] };
    }
    return { type: "info", text: raw };
  });
});

const currentSymbol = computed(() => {
  const starts = parsedProgress.value.filter((p) => p.type === "sym_start");
  const dones = parsedProgress.value
    .filter((p) => p.type === "sym_done")
    .map((p) => p.sym);
  const pending = starts.find((p) => !dones.includes(p.sym));
  return pending ?? null;
});

// ── Análisis de filtros ───────────────────────────────────────────────────────
// undefined = resultado antiguo sin filter_analysis; objeto = datos disponibles
function filterAnalysis(sym) {
  if (!result.value) return undefined;
  if (!("filter_analysis" in result.value)) return undefined;
  return result.value.filter_analysis[sym] ?? undefined;
}
function filterAnalysisRows(sym) {
  const fa = filterAnalysis(sym);
  if (!fa) return [];
  return Object.entries(fa)
    .filter(([k]) => k !== "baseline")
    .map(([name, data]) => ({ name, ...data }));
}

// Deriva los tabs de las claves reales de por_simbolo (coinciden con trades_por_simbolo)
// Necesario: params.symbols puede ser el formato antiguo sin "· breakout"
const resultTabSymbols = computed(() => {
  if (!result.value) return [];
  const conSyms =
    result.value.con_filtros?.por_simbolo?.map((s) => s.symbol) ?? [];
  const sinSyms =
    result.value.sin_filtros?.por_simbolo?.map((s) => s.symbol) ?? [];
  const merged = [...new Set([...conSyms, ...sinSyms])];
  return merged.length > 0 ? merged : (result.value.params?.symbols ?? []);
});

// ── Simulaciones guardadas ────────────────────────────────────────────────────
const labTab = ref("sim"); // "sim" | "saved"
const savedSimulations = ref([]);
const isSavedView = ref(false); // true cuando se visualiza una simulación guardada
const showSaveModal = ref(false);
const saveName = ref("");
const savePending = ref(false);
const savedLoadError = ref(null);

async function loadSavedSims() {
  try {
    const res = await fetch("/api/lab/simulations");
    savedSimulations.value = await res.json();
  } catch {
    savedLoadError.value = "No se pudieron cargar las simulaciones guardadas.";
  }
}

async function openSavedTab() {
  labTab.value = "saved";
  isSavedView.value = false;
  result.value = null;
  progress.value = [];
  jobStatus.value = null;
  await loadSavedSims();
}

async function viewSavedSim(id) {
  const res = await fetch(`/api/lab/simulations/${id}`);
  if (!res.ok) return;
  const data = await res.json();
  result.value = data.result;
  isSavedView.value = true;
  labTab.value = "sim";
  activeResultTab.value = "general";
  progress.value = [];
  jobStatus.value = null;
}

function backToSavedList() {
  result.value = null;
  isSavedView.value = false;
  labTab.value = "saved";
  progress.value = [];
  jobStatus.value = null;
}

const savedOk = ref(false);
const showFiltersInfo = ref(false);

async function saveSimulation() {
  if (!saveName.value.trim() || !result.value || savePending.value) return;
  savePending.value = true;
  try {
    const res = await fetch("/api/lab/simulations", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        name: saveName.value.trim(),
        result: result.value,
      }),
    });
    if (!res.ok) throw new Error();
    showSaveModal.value = false;
    saveName.value = "";
    // Limpiar estado anterior y mostrar formulario limpio listo para nueva simulación
    result.value = null;
    progress.value = [];
    jobStatus.value = null;
    jobId.value = null;
    isSavedView.value = false;
    localStorage.removeItem(JOB_STORAGE_KEY);
    savedOk.value = true;
    setTimeout(() => {
      savedOk.value = false;
    }, 4000);
    await loadSavedSims();
  } catch {
    /* silencioso */
  } finally {
    savePending.value = false;
  }
}

async function deleteSavedSim(id) {
  await fetch(`/api/lab/simulations/${id}`, { method: "DELETE" });
  savedSimulations.value = savedSimulations.value.filter((s) => s.id !== id);
}

function fmtSavedDate(iso) {
  if (!iso) return "";
  return new Date(iso).toLocaleString("es-ES", {
    dateStyle: "short",
    timeStyle: "short",
  });
}
</script>

<template>
  <div class="lab">
    <!-- ── Pestañas del Laboratorio ── -->
    <div class="lab-tabs">
      <button
        class="lab-tab"
        :class="{ active: labTab === 'sim' }"
        @click="
          labTab = 'sim';
          isSavedView = false;
        "
      >
        Simulación
      </button>
      <button
        class="lab-tab"
        :class="{ active: labTab === 'saved' }"
        @click="openSavedTab"
      >
        Guardadas
        <span v-if="savedSimulations.length" class="saved-count">{{
          savedSimulations.length
        }}</span>
      </button>
    </div>

    <!-- ════════════════════ Vista: Guardadas ════════════════════ -->
    <div v-if="labTab === 'saved'" class="saved-view">
      <div v-if="!savedSimulations.length" class="saved-empty">
        No hay simulaciones guardadas. Ejecuta una y pulsa
        <strong>💾 Guardar</strong>.
      </div>
      <div v-else class="saved-list">
        <div v-for="sim in savedSimulations" :key="sim.id" class="saved-card">
          <div class="saved-card-header">
            <div>
              <div class="saved-name">{{ sim.name }}</div>
              <div class="saved-meta">
                {{ fmtSavedDate(sim.created_at) }} ·
                {{ sim.symbols?.join(", ") }} · ${{ sim.capital }} ·
                {{ sim.leverage }}× · {{ sim.actual_years }}a
              </div>
            </div>
            <div class="saved-actions">
              <button class="btn-view-saved" @click="viewSavedSim(sim.id)">
                Ver →
              </button>
              <button class="btn-del-saved" @click="deleteSavedSim(sim.id)">
                ✕
              </button>
            </div>
          </div>
        </div>
      </div>
    </div>

    <!-- ════════════════════ Vista: Simulación ════════════════════ -->
    <template v-if="labTab === 'sim'">
      <p v-if="!isSavedView" class="subtitle">
        Configura los parámetros y simula la cartera. Los resultados comparan
        <strong>con y sin filtros F1-F4</strong>.
      </p>

      <!-- ── Formulario (solo cuando no se visualiza una guardada) ── -->
      <template v-if="!isSavedView">
        <div v-if="savedOk" class="saved-ok-banner">
          ✓ Simulación guardada. Puedes verla en la pestaña
          <strong>Guardadas</strong>.
        </div>
        <div class="form-grid">
          <!-- Selector de símbolo -->
          <div class="field field-full">
            <label
              >Añadir entrada
              <span class="hint">(símbolo + estrategia)</span></label
            >
            <div class="sym-add-row">
              <select v-model="dropdownSym" class="sym-select">
                <option value="" disabled>
                  — Selecciona una criptomoneda —
                </option>
                <optgroup
                  v-for="group in [
                    {
                      label: 'Majors',
                      syms: [
                        'BTC/USDT',
                        'ETH/USDT',
                        'BNB/USDT',
                        'XRP/USDT',
                        'LTC/USDT',
                        'TRX/USDT',
                      ],
                    },
                    {
                      label: 'Smart contracts',
                      syms: [
                        'ADA/USDT',
                        'SOL/USDT',
                        'DOT/USDT',
                        'AVAX/USDT',
                        'ATOM/USDT',
                        'EGLD/USDT',
                        'NEAR/USDT',
                        'ALGO/USDT',
                        'ICP/USDT',
                        'THETA/USDT',
                        'VET/USDT',
                      ],
                    },
                    {
                      label: 'L2 / Infra',
                      syms: [
                        'MATIC/USDT',
                        'ARB/USDT',
                        'OP/USDT',
                        'APT/USDT',
                        'STX/USDT',
                      ],
                    },
                    {
                      label: 'DeFi',
                      syms: ['LINK/USDT', 'AAVE/USDT', 'UNI/USDT', 'INJ/USDT'],
                    },
                    {
                      label: 'Otros',
                      syms: [
                        'DOGE/USDT',
                        'XLM/USDT',
                        'HBAR/USDT',
                        'FIL/USDT',
                        'TON/USDT',
                        'AXS/USDT',
                        'SAND/USDT',
                      ],
                    },
                  ]"
                  :key="group.label"
                  :label="group.label"
                >
                  <option
                    v-for="sym in group.syms.filter(
                      (s) =>
                        !selectedSymbols.find(
                          (e) => e.symbol === s && e.strategy === dropdownStrat,
                        ),
                    )"
                    :key="sym"
                    :value="sym"
                  >
                    {{ sym }}
                  </option>
                </optgroup>
              </select>
              <select v-model="dropdownStrat" class="strat-select">
                <option value="breakout">Breakout</option>
                <option value="retest">Retest</option>
                <option value="bounce">Bounce</option>
              </select>
              <button
                class="btn-add-sym"
                :disabled="
                  !dropdownSym ||
                  selectedSymbols.length >= 16 ||
                  !!selectedSymbols.find(
                    (s) =>
                      s.symbol === dropdownSym && s.strategy === dropdownStrat,
                  )
                "
                @click="addSymbol"
              >
                + Añadir
              </button>
            </div>
            <div v-if="!selectedSymbols.length" class="field-warn">
              Añade al menos una criptomoneda.
            </div>
          </div>

          <!-- Tarjetas por (símbolo + estrategia) -->
          <div v-if="selectedSymbols.length" class="field field-full sym-cards">
            <div
              v-for="item in selectedSymbols"
              :key="item.id"
              class="sym-filter-card"
            >
              <div class="sfc-header">
                <span class="sfc-name">{{ item.symbol }}</span>
                <span
                  class="sfc-strat-badge"
                  :class="`strat-${item.strategy}`"
                  >{{ item.strategy }}</span
                >
                <button class="sfc-remove" @click="removeEntry(item.id)">
                  ✕
                </button>
              </div>
              <div class="sfc-filters">
                <!-- ── Breakout: filtros F1-F4 ── -->
                <template v-if="item.strategy === 'breakout'">
                  <div class="sfc-section-label">
                    Filtros de calidad
                    <button
                      class="btn-filter-info"
                      @click.prevent="showFiltersInfo = true"
                      title="Qué hace cada filtro"
                    >
                      ℹ
                    </button>
                  </div>
                  <label class="sfc-row">
                    <input type="checkbox" v-model="item.filters.f1.enabled" />
                    <span class="sfc-fname">Trampa de momentum</span>
                    <template v-if="item.filters.f1.enabled">
                      <input
                        class="sfc-num"
                        type="number"
                        v-model.number="item.filters.f1.lo"
                        step="0.1"
                      />
                      <span class="sfc-sep">–</span>
                      <input
                        class="sfc-num"
                        type="number"
                        v-model.number="item.filters.f1.hi"
                        step="0.1"
                      />
                      <span class="sfc-unit">%</span>
                    </template>
                    <span v-else class="sfc-hint"
                      >evita la zona donde el precio acelera antes de
                      revertir</span
                    >
                  </label>
                  <label class="sfc-row">
                    <input type="checkbox" v-model="item.filters.f2b.enabled" />
                    <span class="sfc-fname">Horario restringido</span>
                    <template v-if="item.filters.f2b.enabled">
                      <input
                        class="sfc-num"
                        type="number"
                        v-model.number="item.filters.f2b.lo"
                        min="0"
                        max="23"
                      />
                      <span class="sfc-sep">–</span>
                      <input
                        class="sfc-num"
                        type="number"
                        v-model.number="item.filters.f2b.hi"
                        min="1"
                        max="24"
                      />
                      <span class="sfc-unit">h UTC</span>
                    </template>
                    <span v-else class="sfc-hint"
                      >apertura Londres/NY — WR 23-26% vs 34% fuera</span
                    >
                  </label>
                  <label class="sfc-row">
                    <input type="checkbox" v-model="item.filters.f3.enabled" />
                    <span class="sfc-fname">Trampa de volumen</span>
                    <template v-if="item.filters.f3.enabled">
                      <input
                        class="sfc-num"
                        type="number"
                        v-model.number="item.filters.f3.lo"
                        step="0.1"
                      />
                      <span class="sfc-sep">–</span>
                      <input
                        class="sfc-num"
                        type="number"
                        v-model.number="item.filters.f3.hi"
                        step="0.1"
                      />
                      <span class="sfc-unit">×</span>
                    </template>
                    <span v-else class="sfc-hint"
                      >volumen intermedio 2.1-2.7× — zona de fakeouts
                      frecuentes</span
                    >
                  </label>
                  <label class="sfc-row">
                    <input type="checkbox" v-model="item.filters.f4.enabled" />
                    <span class="sfc-fname">Sobrecompra RSI</span>
                    <template v-if="item.filters.f4.enabled">
                      <input
                        class="sfc-num"
                        type="number"
                        v-model.number="item.filters.f4.threshold"
                        min="50"
                        max="100"
                      />
                      <span class="sfc-unit">(sobrecompra)</span>
                    </template>
                    <span v-else class="sfc-hint"
                      >RSI ≥ 70 → WR 25.9% vs 34.2% normal</span
                    >
                  </label>
                  <label class="sfc-row">
                    <input type="checkbox" v-model="item.filters.fr.enabled" />
                    <span class="sfc-fname">Anti-fakeout</span>
                    <span class="sfc-hint"
                      >espera que la rotura sea confirmada
                      (auto-calibrado)</span
                    >
                  </label>
                  <label class="sfc-row">
                    <input
                      type="checkbox"
                      v-model="item.filters.volmax.enabled"
                    />
                    <span class="sfc-fname">Spike extremo ≤</span>
                    <template v-if="item.filters.volmax.enabled">
                      <input
                        class="sfc-num"
                        type="number"
                        v-model.number="item.filters.volmax.max"
                        step="0.1"
                      />
                      <span class="sfc-unit">×</span>
                    </template>
                    <span v-else class="sfc-hint"
                      >spikes >2.8× suelen ser trampas de ballenas</span
                    >
                  </label>
                </template>

                <!-- ── Retest: parámetros específicos ── -->
                <template v-else-if="item.strategy === 'retest'">
                  <div class="sfc-section-label">Parámetros de retest</div>
                  <div class="retest-param">
                    <span class="sfc-fname retest-label">Distancia mínima</span>
                    <input
                      class="sfc-num"
                      type="number"
                      step="0.1"
                      v-model.number="item.filters.retest_min_move_pct"
                    />
                    <span class="sfc-unit">% (distancia mínima al nivel)</span>
                  </div>
                  <div class="retest-param">
                    <span class="sfc-fname retest-label"
                      >Proximidad máxima</span
                    >
                    <input
                      class="sfc-num"
                      type="number"
                      step="0.05"
                      v-model.number="item.filters.retest_tolerance_pct"
                    />
                    <span class="sfc-unit">% al nivel</span>
                  </div>
                  <div class="retest-param">
                    <span class="sfc-fname retest-label"
                      >Vol. pullback máx</span
                    >
                    <input
                      class="sfc-num"
                      type="number"
                      step="0.1"
                      v-model.number="item.filters.retest_pullback_vol_max"
                    />
                    <span class="sfc-unit">× (pullback silencioso)</span>
                  </div>
                </template>

                <!-- ── Bounce: sin parámetros adicionales ── -->
                <template v-else-if="item.strategy === 'bounce'">
                  <div class="sfc-hint" style="padding: 0.4rem 0">
                    Sin parámetros configurables — usa el midpoint del rango
                    mensual como TP.
                  </div>
                </template>
              </div>
            </div>
          </div>

          <!-- Parámetros globales -->
          <div class="field">
            <label>Capital inicial ($)</label>
            <input v-model.number="capital" type="number" min="1" step="10" />
          </div>
          <div class="field">
            <label>Posiciones simultáneas</label>
            <input
              v-model.number="maxPositions"
              type="number"
              min="1"
              max="10"
            />
            <span class="hint">{{
              maxPositions > 1
                ? (100 / maxPositions).toFixed(1) + "% por trade"
                : "100% por trade"
            }}</span>
          </div>
          <!-- Período: toggle años / rango de fechas -->
          <div class="field field-full">
            <label>Período a simular</label>
            <div class="period-toggle">
              <button
                class="ptbtn"
                :class="{ active: periodMode === 'years' }"
                @click="periodMode = 'years'"
              >
                Nº de años
              </button>
              <button
                class="ptbtn"
                :class="{ active: periodMode === 'range' }"
                @click="periodMode = 'range'"
              >
                Rango de fechas
              </button>
            </div>
          </div>
          <div v-if="periodMode === 'years'" class="field">
            <label>Años a simular</label>
            <input v-model.number="years" type="number" min="1" max="15" />
          </div>
          <template v-else>
            <div class="field">
              <label>Desde</label>
              <input v-model="dateFrom" type="date" class="date-input" />
            </div>
            <div class="field">
              <label>Hasta</label>
              <input v-model="dateTo" type="date" class="date-input" />
            </div>
          </template>
          <div class="field">
            <label>Apalancamiento (×)</label>
            <input v-model.number="leverage" type="number" min="1" max="10" />
          </div>

          <div class="field field-full btn-row">
            <button
              class="btn-simulate"
              :disabled="isRunning || !selectedSymbols.length"
              @click="simulate"
            >
              {{ isRunning ? "Simulando…" : "▶ Simular" }}
            </button>
            <button
              v-if="isRunning"
              class="btn-cancel"
              @click="showCancelModal = true"
            >
              ✕ Cancelar
            </button>
          </div>
        </div> </template
      ><!-- fin v-if="!isSavedView" -->

      <!-- ── Aviso años recortados ───────────────────────────────────────────── -->
      <div v-if="capped && actualYears" class="info-banner">
        ℹ️ Datos disponibles para los símbolos seleccionados:
        <strong>{{ actualYears }} años</strong> (pediste {{ years }}). Simulando
        con lo disponible.
      </div>

      <!-- ── Progreso visual ────────────────────────────────────────────────── -->
      <div v-if="isRunning || (progress.length && !result)" class="prog-box">
        <!-- Cabecera con estado actual -->
        <div class="prog-header">
          <span class="spinner" />
          <span v-if="currentSymbol">
            Analizando <strong>{{ currentSymbol.sym }}</strong> ({{
              currentSymbol.idx
            }}
            de {{ currentSymbol.total }})
          </span>
          <span v-else>Preparando análisis…</span>
        </div>

        <div v-for="(item, i) in parsedProgress" :key="i">
          <!-- Cabecera de pasada -->
          <div v-if="item.type === 'pass'" class="prog-pass">
            <span class="prog-pass-badge">{{ item.pass }}/2</span>
            {{
              item.pass === 1
                ? "Análisis base sin filtros"
                : "Análisis con filtros F1-F4"
            }}
          </div>

          <!-- Análisis de filtros -->
          <div v-else-if="item.type === 'filterpass'" class="prog-pass">
            <span class="prog-pass-badge" style="background: var(--green)"
              >3/3</span
            >
            Impacto individual de cada filtro por criptomoneda
          </div>
          <div
            v-else-if="item.type === 'filteranal'"
            class="prog-sym prog-sym-active"
          >
            <span class="spinner-sm" /> Analizando filtros de {{ item.sym }}…
          </div>

          <!-- Símbolo en curso (solo el actual) -->
          <div
            v-else-if="
              item.type === 'sym_start' && currentSymbol?.sym === item.sym
            "
            class="prog-sym prog-sym-active"
          >
            <span class="spinner-sm" />
            <span>{{ item.sym }}</span>
            <span class="prog-sym-sub">descargando datos históricos…</span>
          </div>

          <!-- Símbolo completado -->
          <div
            v-else-if="item.type === 'sym_done'"
            class="prog-sym prog-sym-done"
          >
            <span class="prog-check">✓</span>
            <span>{{ item.sym }}</span>
            <span class="prog-sym-stats">
              {{ item.trades }} operaciones
              <span class="prog-wr" :class="item.wr >= 30 ? 'wr-ok' : 'wr-low'">
                · WR {{ item.wr }}%
              </span>
              <span class="prog-dir"
                >· {{ item.longs }}↑ {{ item.shorts }}↓</span
              >
            </span>
          </div>

          <!-- Mensajes informativos -->
          <div v-else-if="item.type === 'info' && item.text" class="prog-info">
            {{ item.text }}
          </div>
        </div>
      </div>

      <!-- ── Error ─────────────────────────────────────────────────────────── -->
      <div v-if="errorMsg && !isRunning" class="error-box">{{ errorMsg }}</div>

      <!-- ── Resultados ─────────────────────────────────────────────────────── -->
      <template v-if="result">
        <div class="result-params">
          <span v-if="isSavedView">
            <button class="btn-back-saved" @click="backToSavedList">
              ← Guardadas
            </button>
          </span>
          Simulación:
          <strong>{{ resultTabSymbols.map(tabLabel).join(", ") }}</strong> · ${{
            result.params.capital
          }}
          · {{ result.params.max_positions }} posiciones ·
          {{ result.params.leverage }}× · {{ result.params.actual_years }} años
          <button
            v-if="!isSavedView"
            class="btn-save-sim"
            @click="showSaveModal = true"
          >
            💾 Guardar
          </button>
        </div>

        <!-- Modal guardar simulación -->
        <div
          v-if="showSaveModal"
          class="modal-overlay"
          @click.self="showSaveModal = false"
        >
          <div class="modal-box">
            <div class="modal-title">Guardar simulación</div>
            <input
              v-model="saveName"
              class="save-name-input"
              placeholder="Nombre de la simulación…"
              maxlength="100"
              @keyup.enter="saveSimulation"
              autofocus
            />
            <div class="modal-actions">
              <button class="btn-cancel-modal" @click="showSaveModal = false">
                Cancelar
              </button>
              <button
                class="btn-confirm-save"
                :disabled="!saveName.trim() || savePending"
                @click="saveSimulation"
              >
                {{ savePending ? "Guardando…" : "💾 Guardar" }}
              </button>
            </div>
          </div>
        </div>

        <!-- Pestañas General + una por criptomoneda -->
        <div class="result-tabs">
          <button
            class="rtab"
            :class="{ active: activeResultTab === 'general' }"
            @click="activeResultTab = 'general'"
          >
            General
          </button>
          <button
            v-for="sym in resultTabSymbols"
            :key="sym"
            class="rtab"
            :class="{ active: activeResultTab === sym }"
            @click="
              activeResultTab = sym;
              tradeFilter = 'con';
            "
          >
            {{ tabLabel(sym) }}
          </button>
        </div>

        <!-- ── Vista General ── -->
        <div v-show="activeResultTab === 'general'" class="compare-grid">
          <div class="sim-block">
            <div class="sim-title">
              Sin filtros F1-F4 <span class="badge-base">baseline</span>
            </div>
            <div v-if="result.sin_filtros?.error" class="sim-error">
              {{ result.sin_filtros.error }}
            </div>
            <template v-else>
              <div class="kpi-row">
                <div class="kpi">
                  <div class="kpi-label">Capital final</div>
                  <div class="kpi-val" :class="pc(result.sin_filtros.pnl_pct)">
                    {{ fmtUsd(result.sin_filtros.capital_final) }}
                  </div>
                  <div class="kpi-sub" :class="pc(result.sin_filtros.pnl_pct)">
                    {{ fmtPct(result.sin_filtros.pnl_pct) }}
                  </div>
                </div>
                <div class="kpi">
                  <div class="kpi-label">Win Rate</div>
                  <div class="kpi-val">
                    {{ result.sin_filtros.win_rate?.toFixed(1) }}%
                  </div>
                  <div class="kpi-sub">
                    {{ result.sin_filtros.wins }}W /
                    {{ result.sin_filtros.losses }}L
                  </div>
                </div>
                <div class="kpi">
                  <div class="kpi-label">Max Drawdown</div>
                  <div class="kpi-val negative">
                    {{ result.sin_filtros.max_drawdown_pct?.toFixed(1) }}%
                  </div>
                  <div class="kpi-sub">
                    Mín. {{ fmtUsd(result.sin_filtros.min_equity) }}
                  </div>
                </div>
                <div class="kpi">
                  <div class="kpi-label">Trades / Señales</div>
                  <div class="kpi-val">
                    {{ result.sin_filtros.total_trades }}
                  </div>
                  <div class="kpi-sub">
                    de {{ result.sin_filtros.total_signals }} señales
                  </div>
                </div>
              </div>
              <div class="section-label">Por criptomoneda</div>
              <table class="mini-table">
                <thead>
                  <tr>
                    <th>Criptomoneda</th>
                    <th>Operaciones</th>
                    <th>Win Rate</th>
                  </tr>
                </thead>
                <tbody>
                  <tr
                    v-for="s in result.sin_filtros.por_simbolo"
                    :key="s.symbol"
                  >
                    <td>{{ s.symbol }}</td>
                    <td>{{ s.trades }}</td>
                    <td :class="s.win_rate >= 30 ? 'positive' : 'negative'">
                      {{ s.win_rate }}%
                    </td>
                  </tr>
                </tbody>
              </table>
              <div class="section-label">Año a año</div>
              <table class="mini-table">
                <thead>
                  <tr>
                    <th>Año</th>
                    <th>Capital</th>
                    <th>PnL acum.</th>
                  </tr>
                </thead>
                <tbody>
                  <tr
                    v-for="row in result.sin_filtros.por_anio"
                    :key="row.year"
                  >
                    <td>{{ row.year }}</td>
                    <td>{{ fmtUsd(row.capital) }}</td>
                    <td :class="pc(row.pnl_pct)">{{ fmtPct(row.pnl_pct) }}</td>
                  </tr>
                </tbody>
              </table>
              <div class="section-label">Volumen</div>
              <table class="mini-table">
                <thead>
                  <tr>
                    <th>Bucket</th>
                    <th>Trades</th>
                    <th>WR</th>
                  </tr>
                </thead>
                <tbody>
                  <tr
                    v-for="b in result.sin_filtros.analisis_volumen"
                    :key="b.bucket"
                  >
                    <td>{{ b.bucket }}</td>
                    <td>{{ b.trades }}</td>
                    <td :class="b.win_rate >= 30 ? 'positive' : 'negative'">
                      {{ b.win_rate }}%
                    </td>
                  </tr>
                </tbody>
              </table>
              <div class="section-label">Long vs Short</div>
              <table class="mini-table">
                <thead>
                  <tr>
                    <th>Dirección</th>
                    <th>Trades</th>
                    <th>WR</th>
                  </tr>
                </thead>
                <tbody>
                  <tr
                    v-for="d in result.sin_filtros.analisis_direccion"
                    :key="d.direction"
                  >
                    <td>
                      <span
                        :class="
                          d.direction === 'long' ? 'dir-long' : 'dir-short'
                        "
                        >{{ d.direction.toUpperCase() }}</span
                      >
                    </td>
                    <td>{{ d.trades }}</td>
                    <td :class="d.win_rate >= 30 ? 'positive' : 'negative'">
                      {{ d.win_rate }}%
                    </td>
                  </tr>
                </tbody>
              </table>
            </template>
          </div>

          <div class="sim-block highlighted">
            <div class="sim-title">
              Con filtros F1-F4 <span class="badge-filters">activos</span>
            </div>
            <div v-if="result.con_filtros?.error" class="sim-error">
              {{ result.con_filtros.error }}
            </div>
            <template v-else>
              <div class="kpi-row">
                <div class="kpi">
                  <div class="kpi-label">Capital final</div>
                  <div class="kpi-val" :class="pc(result.con_filtros.pnl_pct)">
                    {{ fmtUsd(result.con_filtros.capital_final) }}
                  </div>
                  <div class="kpi-sub" :class="pc(result.con_filtros.pnl_pct)">
                    {{ fmtPct(result.con_filtros.pnl_pct) }}
                  </div>
                </div>
                <div class="kpi">
                  <div class="kpi-label">Win Rate</div>
                  <div class="kpi-val">
                    {{ result.con_filtros.win_rate?.toFixed(1) }}%
                  </div>
                  <div class="kpi-sub">
                    {{ result.con_filtros.wins }}W /
                    {{ result.con_filtros.losses }}L
                  </div>
                </div>
                <div class="kpi">
                  <div class="kpi-label">Max Drawdown</div>
                  <div class="kpi-val negative">
                    {{ result.con_filtros.max_drawdown_pct?.toFixed(1) }}%
                  </div>
                  <div class="kpi-sub">
                    Mín. {{ fmtUsd(result.con_filtros.min_equity) }}
                  </div>
                </div>
                <div class="kpi">
                  <div class="kpi-label">Trades / Señales</div>
                  <div class="kpi-val">
                    {{ result.con_filtros.total_trades }}
                  </div>
                  <div class="kpi-sub">
                    de {{ result.con_filtros.total_signals }} señales
                  </div>
                </div>
              </div>
              <div class="section-label">Por criptomoneda</div>
              <table class="mini-table">
                <thead>
                  <tr>
                    <th>Criptomoneda</th>
                    <th>Operaciones</th>
                    <th>Win Rate</th>
                  </tr>
                </thead>
                <tbody>
                  <tr
                    v-for="s in result.con_filtros.por_simbolo"
                    :key="s.symbol"
                  >
                    <td>{{ s.symbol }}</td>
                    <td>{{ s.trades }}</td>
                    <td :class="s.win_rate >= 30 ? 'positive' : 'negative'">
                      {{ s.win_rate }}%
                    </td>
                  </tr>
                </tbody>
              </table>
              <div class="section-label">Año a año</div>
              <table class="mini-table">
                <thead>
                  <tr>
                    <th>Año</th>
                    <th>Capital</th>
                    <th>PnL acum.</th>
                  </tr>
                </thead>
                <tbody>
                  <tr
                    v-for="row in result.con_filtros.por_anio"
                    :key="row.year"
                  >
                    <td>{{ row.year }}</td>
                    <td>{{ fmtUsd(row.capital) }}</td>
                    <td :class="pc(row.pnl_pct)">{{ fmtPct(row.pnl_pct) }}</td>
                  </tr>
                </tbody>
              </table>
              <div class="section-label">Volumen</div>
              <table class="mini-table">
                <thead>
                  <tr>
                    <th>Bucket</th>
                    <th>Trades</th>
                    <th>WR</th>
                  </tr>
                </thead>
                <tbody>
                  <tr
                    v-for="b in result.con_filtros.analisis_volumen"
                    :key="b.bucket"
                  >
                    <td>{{ b.bucket }}</td>
                    <td>{{ b.trades }}</td>
                    <td :class="b.win_rate >= 30 ? 'positive' : 'negative'">
                      {{ b.win_rate }}%
                    </td>
                  </tr>
                </tbody>
              </table>
              <div class="section-label">Long vs Short</div>
              <table class="mini-table">
                <thead>
                  <tr>
                    <th>Dirección</th>
                    <th>Trades</th>
                    <th>WR</th>
                  </tr>
                </thead>
                <tbody>
                  <tr
                    v-for="d in result.con_filtros.analisis_direccion"
                    :key="d.direction"
                  >
                    <td>
                      <span
                        :class="
                          d.direction === 'long' ? 'dir-long' : 'dir-short'
                        "
                        >{{ d.direction.toUpperCase() }}</span
                      >
                    </td>
                    <td>{{ d.trades }}</td>
                    <td :class="d.win_rate >= 30 ? 'positive' : 'negative'">
                      {{ d.win_rate }}%
                    </td>
                  </tr>
                </tbody>
              </table>
            </template>
          </div>
        </div>
        <!-- /compare-grid -->

        <!-- ── Vista por símbolo ── -->
        <template v-if="activeResultTab !== 'general'">
          <!-- Filtros activos -->
          <div class="sym-filters-row">
            <span class="sym-filters-label">Filtros activos:</span>
            <span v-if="!activeFilters(activeResultTab).length" class="tag-none"
              >Ninguno</span
            >
            <span
              v-for="f in activeFilters(activeResultTab)"
              :key="f"
              class="tag-filter"
              >{{ f }}</span
            >
          </div>

          <!-- Comparativa sin vs con para este símbolo -->
          <div class="sym-compare">
            <div class="sym-compare-block">
              <div class="sym-compare-title">Sin filtros</div>
              <template v-if="symSin(activeResultTab)">
                <div class="sym-kpi-row">
                  <div class="sym-kpi">
                    <span class="sym-kpi-label">Trades</span>
                    {{ symSin(activeResultTab).trades }}
                  </div>
                  <div class="sym-kpi">
                    <span class="sym-kpi-label">WR</span>
                    {{ symSin(activeResultTab).win_rate }}%
                  </div>
                </div>
              </template>
            </div>
            <div class="sym-compare-arrow">→</div>
            <div class="sym-compare-block highlighted-light">
              <div class="sym-compare-title">Con filtros</div>
              <template
                v-if="symCon(activeResultTab) && symSin(activeResultTab)"
              >
                <div class="sym-kpi-row">
                  <div class="sym-kpi">
                    <span class="sym-kpi-label">Trades</span>
                    {{ symCon(activeResultTab).trades }}
                    <span
                      class="delta"
                      :class="
                        symCon(activeResultTab).trades <=
                        symSin(activeResultTab).trades
                          ? 'negative'
                          : 'positive'
                      "
                    >
                      ({{
                        symCon(activeResultTab).trades -
                          symSin(activeResultTab).trades >=
                        0
                          ? "+"
                          : ""
                      }}{{
                        symCon(activeResultTab).trades -
                        symSin(activeResultTab).trades
                      }})
                    </span>
                  </div>
                  <div class="sym-kpi">
                    <span class="sym-kpi-label">WR</span>
                    {{ symCon(activeResultTab).win_rate }}%
                    <span
                      class="delta"
                      :class="
                        symCon(activeResultTab).win_rate -
                          symSin(activeResultTab).win_rate >=
                        0
                          ? 'positive'
                          : 'negative'
                      "
                    >
                      ({{
                        symCon(activeResultTab).win_rate -
                          symSin(activeResultTab).win_rate >=
                        0
                          ? "+"
                          : ""
                      }}{{
                        (
                          symCon(activeResultTab).win_rate -
                          symSin(activeResultTab).win_rate
                        ).toFixed(1)
                      }}%)
                    </span>
                  </div>
                </div>
              </template>
            </div>
          </div>

          <!-- Tabla de impacto por filtro -->
          <template v-if="filterAnalysis(activeResultTab)">
            <div class="section-label">
              Impacto de cada filtro
              <span class="fa-legend">
                <span class="fa-badge-on">✓ activo</span> = ya configurado en
                config.yaml · <span class="fa-badge-off">◌ test</span> = umbral
                estándar sin configurar
              </span>
            </div>
            <div class="filter-analysis-wrap">
              <table class="mini-table">
                <thead>
                  <tr>
                    <th>Filtro</th>
                    <th>Estado</th>
                    <th>Operaciones</th>
                    <th>Win Rate</th>
                    <th>Δ WR vs base</th>
                    <th>Trades filtrados</th>
                  </tr>
                </thead>
                <tbody>
                  <tr class="row-baseline">
                    <td><strong>Sin filtros (base)</strong></td>
                    <td>—</td>
                    <td>
                      {{ filterAnalysis(activeResultTab).baseline.trades }}
                    </td>
                    <td>
                      {{ filterAnalysis(activeResultTab).baseline.win_rate }}%
                    </td>
                    <td>—</td>
                    <td>—</td>
                  </tr>
                  <tr
                    v-for="row in filterAnalysisRows(activeResultTab)"
                    :key="row.name"
                    :class="
                      row.wr_delta > 0
                        ? 'row-beneficial'
                        : row.wr_delta < 0
                          ? 'row-harmful'
                          : ''
                    "
                  >
                    <td>{{ row.name }}</td>
                    <td>
                      <span v-if="row.configured" class="fa-badge-on"
                        >✓ activo</span
                      >
                      <span v-else class="fa-badge-off">◌ test</span>
                    </td>
                    <td>{{ row.trades }}</td>
                    <td :class="row.wr_delta >= 0 ? 'positive' : 'negative'">
                      {{ row.win_rate }}%
                    </td>
                    <td>
                      <span
                        class="fa-delta"
                        :class="row.wr_delta >= 0 ? 'positive' : 'negative'"
                      >
                        {{ row.wr_delta >= 0 ? "+" : "" }}{{ row.wr_delta }}%
                      </span>
                    </td>
                    <td class="fa-filtered">-{{ row.trades_filtered }}</td>
                  </tr>
                </tbody>
              </table>
            </div>
            <div class="fa-hint">
              Δ WR &gt; 0 = el filtro mejora el Win Rate eliminando entradas
              malas. Añade los filtros beneficiosos en la pestaña
              <strong>Configuración</strong>.
            </div>
          </template>

          <!-- Resultado antiguo: necesita re-simulación -->
          <div v-else class="fa-notice">
            ↻ Vuelve a simular para ver el análisis de filtros. La pasada de
            análisis se ejecuta automáticamente al finalizar cada simulación.
          </div>

          <!-- Toggle sin/con y tabla de trades -->
          <div class="trade-table-header">
            <div class="section-label" style="margin: 0">
              Trades ejecutados
              <span
                v-if="
                  result.con_filtros?.trades_total_por_simbolo?.[
                    activeResultTab
                  ] > 500
                "
                class="trades-capped-note"
              >
                (mostrando 500 más recientes de
                {{
                  result.con_filtros.trades_total_por_simbolo[activeResultTab]
                }})
              </span>
            </div>
            <div class="toggle-row">
              <button
                class="toggle-btn"
                :class="{ active: tradeFilter === 'sin' }"
                @click="tradeFilter = 'sin'"
              >
                Sin filtros
              </button>
              <button
                class="toggle-btn"
                :class="{ active: tradeFilter === 'con' }"
                @click="tradeFilter = 'con'"
              >
                Con filtros
              </button>
            </div>
          </div>

          <div class="trades-wrap">
            <table class="mini-table trades-table">
              <thead>
                <tr>
                  <th>Fecha entrada</th>
                  <th>Fecha salida</th>
                  <th>Dir.</th>
                  <th>Entrada</th>
                  <th>Salida</th>
                  <th>PnL%</th>
                  <th>R</th>
                </tr>
              </thead>
              <tbody>
                <tr
                  v-for="(t, i) in tradesForTab(tradeFilter)"
                  :key="i"
                  class="trade-row"
                  @click="selectedTrade = { ...t, symbol: activeResultTab }"
                >
                  <td class="ts-col">{{ fmtDate(t.entry_ts) }}</td>
                  <td class="ts-col">{{ fmtDate(t.exit_ts) }}</td>
                  <td>
                    <span
                      :class="t.direction === 'long' ? 'dir-long' : 'dir-short'"
                      >{{ t.direction?.toUpperCase() }}</span
                    >
                  </td>
                  <td>{{ t.entry_price }}</td>
                  <td>{{ t.exit_price }}</td>
                  <td :class="t.pnl_pct >= 0 ? 'positive' : 'negative'">
                    {{ fmtPct(t.pnl_pct) }}
                  </td>
                  <td>
                    <span
                      :class="t.result === 'win' ? 'positive' : 'negative'"
                      style="font-weight: 700"
                      >{{ t.result === "win" ? "✓" : "✗" }}</span
                    >
                  </td>
                </tr>
                <tr v-if="!tradesForTab(tradeFilter).length">
                  <td
                    colspan="7"
                    style="
                      color: var(--text-muted);
                      text-align: center;
                      padding: 1rem;
                    "
                  >
                    Sin trades para este símbolo y filtro
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
        </template>
      </template>

      <!-- ── Modal detalle trade ────────────────────────────────────────────── -->
      <Teleport to="body">
        <div
          v-if="selectedTrade"
          class="modal-overlay"
          @click.self="selectedTrade = null"
        >
          <div class="modal">
            <div class="modal-title">
              {{ selectedTrade.symbol }} —
              <span
                :class="
                  selectedTrade.direction === 'long' ? 'dir-long' : 'dir-short'
                "
              >
                {{ selectedTrade.direction?.toUpperCase() }}
              </span>
            </div>
            <table class="detail-table">
              <tbody>
                <tr>
                  <td>Fecha entrada</td>
                  <td>{{ fmtDate(selectedTrade.entry_ts) }} UTC</td>
                </tr>
                <tr>
                  <td>Fecha salida</td>
                  <td>{{ fmtDate(selectedTrade.exit_ts) }} UTC</td>
                </tr>
                <tr>
                  <td>Precio entrada</td>
                  <td>
                    <strong>{{ selectedTrade.entry_price }}</strong>
                  </td>
                </tr>
                <tr>
                  <td>Precio salida</td>
                  <td>
                    <strong>{{ selectedTrade.exit_price }}</strong>
                  </td>
                </tr>
                <tr>
                  <td>Resultado</td>
                  <td>
                    <span
                      :class="
                        selectedTrade.result === 'win' ? 'positive' : 'negative'
                      "
                      style="font-weight: 700"
                    >
                      {{ selectedTrade.result === "win" ? "✓ WIN" : "✗ LOSS" }}
                    </span>
                  </td>
                </tr>
                <tr>
                  <td>PnL</td>
                  <td
                    :class="
                      selectedTrade.pnl_pct >= 0 ? 'positive' : 'negative'
                    "
                  >
                    <strong>{{ fmtPct(selectedTrade.pnl_pct) }}</strong>
                  </td>
                </tr>
                <tr>
                  <td>Vol. ratio</td>
                  <td>{{ selectedTrade.volume_ratio }}×</td>
                </tr>
              </tbody>
            </table>
            <div class="modal-actions">
              <button class="btn-modal-keep" @click="selectedTrade = null">
                Cerrar
              </button>
            </div>
          </div>
        </div>
      </Teleport>

      <!-- ── Modal cancelación ──────────────────────────────────────────────── -->
      <Teleport to="body">
        <div
          v-if="showCancelModal"
          class="modal-overlay"
          @click.self="showCancelModal = false"
        >
          <div class="modal">
            <div class="modal-title">¿Detener la simulación?</div>
            <p class="modal-body">
              La simulación está en curso. Si la cancelas entre símbolos se
              detendrá y perderás los resultados parciales.
            </p>
            <div class="modal-actions">
              <button class="btn-modal-keep" @click="showCancelModal = false">
                Continuar simulando
              </button>
              <button class="btn-modal-stop" @click="confirmCancel">
                Sí, cancelar
              </button>
            </div>
          </div>
        </div>
      </Teleport>

      <!-- ── Modal de información de filtros ── -->
      <Teleport to="body">
        <div
          v-if="showFiltersInfo"
          class="modal-overlay"
          @click.self="showFiltersInfo = false"
        >
          <div class="modal filters-info-modal">
            <div class="modal-title">📖 Guía de filtros y estrategias</div>
            <div class="filters-info-body">
              <div class="fi-section">Estrategia <strong>Breakout</strong></div>

              <div class="fi-filter">
                <div class="fi-name">🕐 Horario restringido</div>
                <div class="fi-desc">
                  Bloquea entradas en la franja horaria configurada (hora UTC).
                  La apertura de Londres (8-14h) y el cierre europeo generan
                  spikes falsos — institucionales barren stops antes del
                  movimiento real. WR en esas horas: 23-26% vs 34% el resto del
                  día.
                </div>
                <div class="fi-config">
                  Config: <code>[hora_inicio, hora_fin]</code> en UTC. Ejemplo:
                  <code>[8, 14]</code> bloquea 08:00-13:59h UTC.
                </div>
              </div>

              <div class="fi-filter">
                <div class="fi-name">🌊 Trampa de volumen</div>
                <div class="fi-desc">
                  Bloquea entradas cuando el volumen USDT normalizado cae en la
                  zona intermedia configurada. Esa franja es la "zona trampa Q3"
                  — suficiente volumen para parecer real, demasiado bajo para
                  confirmar rotura institucional. WR en esa zona: &lt;25%.
                </div>
                <div class="fi-config">
                  Config: <code>[mín, máx]</code> multiplicadores sobre la media
                  de 50 velas. Valor validado: <code>[2.1, 2.7]</code> para ADA,
                  ATOM, DOGE.
                </div>
              </div>

              <div class="fi-filter">
                <div class="fi-name">📈 Trampa de momentum</div>
                <div class="fi-desc">
                  Bloquea entradas cuando el cambio de precio en las últimas 5
                  velas está en la zona trampa configurada. Ese rango exacto
                  corresponde a aceleración previa a un rechazo del nivel — el
                  precio llega caliente y revierte.
                </div>
                <div class="fi-config">
                  Config: <code>[% mín, % máx]</code> de cambio en 5 velas.
                  Valor validado: <code>[0.3, 1.6]</code> para ADA y ATOM.
                </div>
              </div>

              <div class="fi-filter">
                <div class="fi-name">📉 Sobrecompra RSI</div>
                <div class="fi-desc">
                  Bloquea entradas cuando el RSI de 14 velas supera el umbral.
                  Entrar con RSI alto indica mercado sobrecomprado a corto plazo
                  — la probabilidad de reversión sube. WR con RSI&gt;70: 25.9%
                  vs 34.2% normal.
                </div>
                <div class="fi-config">
                  Config: umbral entero (50-100). Valor validado:
                  <code>70</code> para EGLD y ATOM.
                </div>
              </div>

              <div class="fi-filter">
                <div class="fi-name">🔄 Anti-fakeout</div>
                <div class="fi-desc">
                  Modo automático: el bot analiza las últimas 2.500 velas del
                  nivel y detecta si hay un patrón de fakeout habitual. Si sí,
                  espera a que el precio rompa, rebote hacia el nivel, y fracase
                  en recuperarlo — confirma que la rotura es real. Elimina el
                  35% de las pérdidas.
                </div>
                <div class="fi-config">
                  Activo por defecto (auto-calibrado). Desactivar solo en "clean
                  breakers" históricos como LINK.
                </div>
              </div>

              <div class="fi-filter">
                <div class="fi-name">⚡ Spike extremo</div>
                <div class="fi-desc">
                  Descarta entradas cuando el spike de volumen supera el máximo
                  configurado. Spikes &gt;2.8× suelen ser trampas de ballenas —
                  acumulación institucional que después revierten. WR con
                  &gt;2.8×: 20-24%.
                </div>
                <div class="fi-config">
                  Config: multiplicador máximo. Valor validado:
                  <code>2.8</code> para LINK y EGLD.
                </div>
              </div>

              <div class="fi-section">Estrategia <strong>Retest</strong></div>

              <div class="fi-filter">
                <div class="fi-name">📏 Distancia mínima</div>
                <div class="fi-desc">
                  Cuánto debe alejarse el precio del nivel roto antes de hacer
                  el pullback. Movimiento mínimo para confirmar que el nivel fue
                  roto significativamente (no un micro-fakeout).
                </div>
                <div class="fi-config">
                  Config: % de distancia. Valor base: <code>0.5%</code>. EGLD
                  validado con <code>1.5%</code>.
                </div>
              </div>

              <div class="fi-filter">
                <div class="fi-name">🎯 Proximidad máxima</div>
                <div class="fi-desc">
                  Hasta qué distancia del nivel original se acepta el pullback
                  como válido. Si el precio no se acerca suficiente al nivel, no
                  hay retest real.
                </div>
                <div class="fi-config">
                  Config: % de proximidad al nivel. Valor base:
                  <code>0.35%</code>. EGLD validado con <code>0.15%</code>.
                </div>
              </div>

              <div class="fi-filter">
                <div class="fi-name">🤫 Volumen pullback máx</div>
                <div class="fi-desc">
                  El pullback debe ocurrir en bajo volumen — confirma que es una
                  corrección natural, no otro intento de rotura en dirección
                  contraria.
                </div>
                <div class="fi-config">
                  Config: multiplicador máximo. Valor base: <code>1.5×</code>.
                  EGLD validado con <code>1.2×</code>.
                </div>
              </div>

              <div class="fi-section">
                Filtros globales
                <small>(se configuran en Configuración → niveles)</small>
              </div>

              <div class="fi-filter">
                <div class="fi-name">📊 ADX mínimo</div>
                <div class="fi-desc">
                  Bloquea entradas cuando el ADX diario es menor al umbral. ADX
                  &lt;20 = mercado lateral sin tendencia. Aplica a
                  <strong>Breakout y Retest</strong>. Los mercados laterales
                  hacen fallar ambas estrategias porque no hay momentum
                  direccional.
                  <em
                    >No aplica a Bounce: los bounces funcionan mejor
                    precisamente en mercados laterales.</em
                  >
                </div>
                <div class="fi-config">
                  Config: <code>adx_min: 20</code> en config.yaml (0 =
                  desactivado).
                </div>
              </div>

              <div class="fi-filter">
                <div class="fi-name">💤 Volumen diario mínimo</div>
                <div class="fi-desc">
                  Bloquea entradas en días con volumen total diario inferior al
                  ratio configurado sobre la media de 20 días. Aplica a
                  <strong>Breakout y Retest</strong>. Días dormidos generan
                  spikes de 5m que son ruido.
                  <em
                    >No aplica a Bounce: tiene su propia validación por mecha de
                    vela.</em
                  >
                </div>
                <div class="fi-config">
                  Config: <code>daily_vol_min_ratio: 0.8</code> en config.yaml
                  (mínimo 80% del volumen habitual). 0 = desactivado.
                </div>
              </div>
            </div>
            <div class="modal-actions">
              <button class="btn-modal-keep" @click="showFiltersInfo = false">
                Cerrar
              </button>
            </div>
          </div>
        </div>
      </Teleport> </template
    ><!-- fin v-if="labTab === 'sim'" -->
  </div>
</template>

<style scoped>
.lab {
  display: flex;
  flex-direction: column;
  gap: 1.25rem;
}
h2 {
  font-size: 1.1rem;
  font-weight: 700;
  color: var(--text);
}
.subtitle {
  font-size: 0.82rem;
  color: var(--text-muted);
}

.form-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
  gap: 1rem;
}
.field {
  display: flex;
  flex-direction: column;
  gap: 0.3rem;
}
.field-full {
  grid-column: 1 / -1;
}
.btn-row {
  flex-direction: row;
  align-items: center;
  gap: 0.75rem;
  flex-wrap: wrap;
}

label {
  font-size: 0.8rem;
  color: var(--text-muted);
  font-weight: 500;
}
input[type="number"] {
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: 0.5rem;
  color: var(--text);
  padding: 0.4rem 0.75rem;
  font-size: 0.9rem;
  width: 100%;
}
input[type="number"]:focus {
  outline: none;
  border-color: var(--accent);
}
.hint {
  font-size: 0.75rem;
  color: var(--text-muted);
}
.field-warn {
  font-size: 0.75rem;
  color: var(--red);
}
.selected-list {
  font-size: 0.75rem;
  color: var(--accent);
}

.sym-grid {
  display: flex;
  flex-wrap: wrap;
  gap: 0.4rem;
}
.sym-chip {
  display: flex;
  flex-direction: column;
  gap: 1px;
  padding: 0.3rem 0.6rem;
  border-radius: 0.5rem;
  border: 1px solid var(--border);
  background: var(--bg);
  cursor: pointer;
  transition: all 0.15s;
}
.sym-chip:hover {
  border-color: var(--accent);
}
.sym-chip.selected {
  border-color: var(--accent);
  background: rgba(99, 102, 241, 0.12);
}
.chip-sym {
  font-size: 0.82rem;
  font-weight: 600;
  color: var(--text);
}
.chip-since {
  font-size: 0.68rem;
  color: var(--text-muted);
}

.btn-simulate {
  padding: 0.6rem 2rem;
  border-radius: 0.6rem;
  border: none;
  background: var(--accent);
  color: #fff;
  font-weight: 700;
  font-size: 0.9rem;
  cursor: pointer;
}
.btn-simulate:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
.btn-simulate:hover:not(:disabled) {
  opacity: 0.88;
}

.btn-cancel {
  padding: 0.55rem 1.25rem;
  border-radius: 0.6rem;
  border: 1px solid var(--red);
  background: transparent;
  color: var(--red);
  font-weight: 600;
  font-size: 0.85rem;
  cursor: pointer;
}
.btn-cancel:hover {
  background: rgba(239, 68, 68, 0.1);
}

.saved-ok-banner {
  background: rgba(34, 197, 94, 0.1);
  border: 1px solid rgba(34, 197, 94, 0.3);
  border-radius: 0.5rem;
  padding: 0.6rem 1rem;
  font-size: 0.82rem;
  color: #4ade80;
}
.info-banner {
  background: rgba(99, 102, 241, 0.1);
  border: 1px solid rgba(99, 102, 241, 0.3);
  border-radius: 0.5rem;
  padding: 0.6rem 1rem;
  font-size: 0.82rem;
  color: #a5b4fc;
}
.error-box {
  background: rgba(239, 68, 68, 0.1);
  border: 1px solid rgba(239, 68, 68, 0.3);
  border-radius: 0.5rem;
  padding: 0.75rem 1rem;
  font-size: 0.85rem;
  color: var(--red);
}

.progress-box {
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: 0.75rem;
  padding: 0.75rem 1rem;
  font-family: monospace;
  font-size: 0.78rem;
  color: var(--text-muted);
  max-height: 220px;
  overflow-y: auto;
}
.progress-title {
  color: var(--text);
  font-weight: 600;
  margin-bottom: 0.4rem;
  display: flex;
  align-items: center;
  gap: 0.4rem;
}
.progress-line {
  line-height: 1.6;
}

@keyframes spin {
  to {
    transform: rotate(360deg);
  }
}
.spinner {
  display: inline-block;
  width: 12px;
  height: 12px;
  border: 2px solid var(--accent);
  border-top-color: transparent;
  border-radius: 50%;
  animation: spin 0.8s linear infinite;
}

.result-params {
  font-size: 0.8rem;
  color: var(--text-muted);
  padding-bottom: 0.5rem;
  border-bottom: 1px solid var(--border);
  display: flex;
  align-items: center;
  gap: 0.5rem;
  flex-wrap: wrap;
}
.btn-save-sim {
  margin-left: auto;
  background: rgba(99, 102, 241, 0.12);
  border: 1px solid var(--accent);
  border-radius: 0.4rem;
  color: #a5b4fc;
  font-size: 0.78rem;
  font-weight: 600;
  padding: 0.25rem 0.7rem;
  cursor: pointer;
  white-space: nowrap;
}
.btn-save-sim:hover {
  background: rgba(99, 102, 241, 0.22);
}
.btn-back-saved {
  background: transparent;
  border: 1px solid var(--border);
  border-radius: 0.4rem;
  color: var(--text-muted);
  font-size: 0.78rem;
  padding: 0.2rem 0.5rem;
  cursor: pointer;
  white-space: nowrap;
}
.btn-back-saved:hover {
  color: var(--text);
}

/* Modal guardar */
.modal-box {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 0.75rem;
  padding: 1.25rem;
  display: flex;
  flex-direction: column;
  gap: 0.75rem;
  min-width: 320px;
  max-width: 460px;
}
.modal-title {
  font-weight: 700;
  font-size: 0.95rem;
  color: var(--text);
}
.save-name-input {
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: 0.5rem;
  color: var(--text);
  padding: 0.45rem 0.75rem;
  font-size: 0.9rem;
  width: 100%;
}
.save-name-input:focus {
  outline: none;
  border-color: var(--accent);
}
.modal-actions {
  display: flex;
  gap: 0.5rem;
  justify-content: flex-end;
}
.btn-cancel-modal {
  background: transparent;
  border: 1px solid var(--border);
  border-radius: 0.4rem;
  color: var(--text-muted);
  padding: 0.35rem 0.8rem;
  cursor: pointer;
  font-size: 0.85rem;
}
.btn-confirm-save {
  background: rgba(99, 102, 241, 0.15);
  border: 1px solid var(--accent);
  border-radius: 0.4rem;
  color: #a5b4fc;
  padding: 0.35rem 0.9rem;
  cursor: pointer;
  font-size: 0.85rem;
  font-weight: 600;
}
.btn-confirm-save:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}

/* Tab bar del Lab */
.lab-tabs {
  display: flex;
  gap: 0.25rem;
  border-bottom: 1px solid var(--border);
  padding-bottom: 0.5rem;
  margin-bottom: 0.25rem;
}
.lab-tab {
  background: transparent;
  border: 1px solid transparent;
  border-radius: 0.4rem;
  color: var(--text-muted);
  cursor: pointer;
  font-size: 0.85rem;
  font-weight: 500;
  padding: 0.3rem 0.9rem;
  display: flex;
  align-items: center;
  gap: 0.4rem;
}
.lab-tab.active {
  background: var(--surface);
  border-color: var(--border);
  color: var(--text);
}
.lab-tab:hover:not(.active) {
  color: var(--text);
}
.saved-count {
  font-size: 0.68rem;
  background: rgba(99, 102, 241, 0.2);
  color: #a5b4fc;
  border-radius: 0.8rem;
  padding: 0.05rem 0.4rem;
}

/* Vista de guardadas */
.saved-view {
  display: flex;
  flex-direction: column;
  gap: 0.75rem;
}
.saved-empty {
  font-size: 0.82rem;
  color: var(--text-muted);
  padding: 1rem;
}
.saved-list {
  display: flex;
  flex-direction: column;
  gap: 0.6rem;
}
.saved-card {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 0.6rem;
  padding: 0.65rem 0.9rem;
}
.saved-card-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 0.75rem;
}
.saved-name {
  font-weight: 700;
  font-size: 0.9rem;
  color: var(--text);
}
.saved-meta {
  font-size: 0.75rem;
  color: var(--text-muted);
  margin-top: 0.15rem;
}
.saved-actions {
  display: flex;
  gap: 0.4rem;
  flex-shrink: 0;
}
.btn-view-saved {
  background: rgba(99, 102, 241, 0.12);
  border: 1px solid var(--accent);
  border-radius: 0.4rem;
  color: #a5b4fc;
  font-size: 0.78rem;
  font-weight: 600;
  padding: 0.2rem 0.6rem;
  cursor: pointer;
  white-space: nowrap;
}
.btn-view-saved:hover {
  background: rgba(99, 102, 241, 0.22);
}
.btn-del-saved {
  background: transparent;
  border: 1px solid var(--border);
  border-radius: 0.4rem;
  color: var(--text-muted);
  font-size: 0.8rem;
  padding: 0.2rem 0.5rem;
  cursor: pointer;
}
.btn-del-saved:hover {
  color: var(--red);
  border-color: var(--red);
  background: rgba(239, 68, 68, 0.08);
}
.compare-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
  gap: 1rem;
}

.sim-block {
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: 0.75rem;
  padding: 1rem;
  display: flex;
  flex-direction: column;
  gap: 0.75rem;
}
.sim-block.highlighted {
  border-color: var(--accent);
}
.sim-title {
  font-weight: 700;
  font-size: 0.9rem;
  color: var(--text);
  display: flex;
  align-items: center;
  gap: 0.5rem;
}
.sim-error {
  color: var(--red);
  font-size: 0.82rem;
}

.badge-base {
  font-size: 0.65rem;
  background: rgba(100, 116, 139, 0.2);
  color: var(--text-muted);
  padding: 0.1rem 0.45rem;
  border-radius: 0.3rem;
}
.badge-filters {
  font-size: 0.65rem;
  background: rgba(99, 102, 241, 0.2);
  color: #a5b4fc;
  padding: 0.1rem 0.45rem;
  border-radius: 0.3rem;
}

.kpi-row {
  display: grid;
  grid-template-columns: repeat(2, 1fr);
  gap: 0.5rem;
}
.kpi {
  background: var(--surface);
  border-radius: 0.5rem;
  padding: 0.5rem 0.75rem;
}
.kpi-label {
  font-size: 0.68rem;
  text-transform: uppercase;
  color: var(--text-muted);
  letter-spacing: 0.04em;
}
.kpi-val {
  font-size: 1.1rem;
  font-weight: 700;
  color: var(--text);
}
.kpi-sub {
  font-size: 0.75rem;
  color: var(--text-muted);
}

.section-label {
  font-size: 0.72rem;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  color: var(--text-muted);
  margin-top: 0.25rem;
}

.mini-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 0.8rem;
}
.mini-table th {
  text-align: left;
  color: var(--text-muted);
  font-size: 0.7rem;
  padding: 0.25rem 0.4rem;
  border-bottom: 1px solid var(--border);
}
.mini-table td {
  padding: 0.3rem 0.4rem;
  border-bottom: 1px solid rgba(255, 255, 255, 0.04);
  color: var(--text);
}

.positive {
  color: var(--green);
}
.negative {
  color: var(--red);
}
.dir-long {
  color: var(--green);
  font-weight: 600;
}
.dir-short {
  color: var(--red);
  font-weight: 600;
}

/* Modal */
.modal-overlay {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.65);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 100;
}
.modal {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 1rem;
  padding: 1.5rem;
  max-width: 380px;
  width: 90%;
  display: flex;
  flex-direction: column;
  gap: 1rem;
}
.modal-title {
  font-weight: 700;
  font-size: 1rem;
  color: var(--text);
}
.modal-body {
  font-size: 0.85rem;
  color: var(--text-muted);
  line-height: 1.5;
  margin: 0;
}
.modal-actions {
  display: flex;
  gap: 0.75rem;
  justify-content: flex-end;
}
.btn-modal-keep {
  padding: 0.45rem 1rem;
  border-radius: 0.5rem;
  border: 1px solid var(--border);
  background: transparent;
  color: var(--text-muted);
  cursor: pointer;
  font-size: 0.85rem;
}
.btn-modal-stop {
  padding: 0.45rem 1rem;
  border-radius: 0.5rem;
  border: none;
  background: var(--red);
  color: #fff;
  cursor: pointer;
  font-weight: 600;
  font-size: 0.85rem;
}

/* Progreso visual */
.prog-box {
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: 0.75rem;
  padding: 1rem 1.25rem;
  display: flex;
  flex-direction: column;
  gap: 0.5rem;
}
.prog-header {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  font-size: 0.88rem;
  font-weight: 600;
  color: var(--text);
  padding-bottom: 0.5rem;
  border-bottom: 1px solid var(--border);
}
.prog-pass {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  font-size: 0.82rem;
  font-weight: 700;
  color: var(--text);
  margin-top: 0.25rem;
}
.prog-pass-badge {
  background: var(--accent);
  color: #fff;
  font-size: 0.65rem;
  font-weight: 700;
  padding: 0.1rem 0.45rem;
  border-radius: 0.3rem;
}
.prog-sym {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  font-size: 0.82rem;
  padding: 0.25rem 0.5rem;
  border-radius: 0.4rem;
}
.prog-sym-active {
  background: rgba(99, 102, 241, 0.08);
  color: var(--text);
}
.prog-sym-done {
  color: var(--text-muted);
}
.prog-check {
  color: var(--green);
  font-weight: 700;
  flex-shrink: 0;
}
.prog-sym-sub {
  font-size: 0.75rem;
  color: var(--text-muted);
}
.prog-sym-stats {
  font-size: 0.75rem;
  color: var(--text-muted);
  margin-left: auto;
}
.wr-ok {
  color: var(--green);
}
.wr-low {
  color: #eab308;
}
.prog-dir {
  color: var(--text-muted);
}
.prog-info {
  font-size: 0.78rem;
  color: var(--text-muted);
  padding-left: 0.5rem;
}

@keyframes spin-sm {
  to {
    transform: rotate(360deg);
  }
}
.spinner-sm {
  display: inline-block;
  width: 10px;
  height: 10px;
  flex-shrink: 0;
  border: 2px solid var(--accent);
  border-top-color: transparent;
  border-radius: 50%;
  animation: spin-sm 0.8s linear infinite;
}

/* Tabla análisis por filtro */
.filter-analysis-wrap {
  overflow-x: auto;
}
.row-baseline td {
  color: var(--text-muted);
  font-size: 0.78rem;
}
.fa-delta {
  font-weight: 700;
}
.fa-filtered {
  color: var(--text-muted);
}

/* Pestañas de resultados */
.result-tabs {
  display: flex;
  gap: 0.35rem;
  flex-wrap: wrap;
  border-bottom: 1px solid var(--border);
  padding-bottom: 0.5rem;
}
.rtab {
  padding: 0.3rem 0.85rem;
  border-radius: 0.4rem;
  border: 1px solid transparent;
  background: transparent;
  color: var(--text-muted);
  cursor: pointer;
  font-size: 0.82rem;
  transition: all 0.15s;
}
.rtab:hover {
  border-color: var(--border);
  color: var(--text);
}
.rtab.active {
  background: var(--surface);
  border-color: var(--accent);
  color: var(--text);
  font-weight: 600;
}

/* Vista símbolo */
.sym-filters-row {
  display: flex;
  align-items: center;
  gap: 0.4rem;
  flex-wrap: wrap;
}
.sym-filters-label {
  font-size: 0.75rem;
  color: var(--text-muted);
}
.tag-filter {
  font-size: 0.7rem;
  background: rgba(99, 102, 241, 0.15);
  color: #a5b4fc;
  padding: 0.1rem 0.45rem;
  border-radius: 0.3rem;
}
.tag-none {
  font-size: 0.7rem;
  color: var(--text-muted);
}

.sym-compare {
  display: flex;
  align-items: center;
  gap: 0.75rem;
  flex-wrap: wrap;
}
.sym-compare-block {
  flex: 1;
  min-width: 150px;
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: 0.75rem;
  padding: 0.75rem 1rem;
}
.sym-compare-block.highlighted-light {
  border-color: var(--accent);
}
.sym-compare-title {
  font-size: 0.75rem;
  font-weight: 600;
  color: var(--text-muted);
  margin-bottom: 0.4rem;
}
.sym-compare-arrow {
  font-size: 1.2rem;
  color: var(--text-muted);
  flex-shrink: 0;
}
.sym-kpi-row {
  display: flex;
  gap: 1rem;
}
.sym-kpi {
  font-size: 0.85rem;
  color: var(--text);
}
.sym-kpi-label {
  font-size: 0.7rem;
  color: var(--text-muted);
  margin-right: 0.25rem;
}
.delta {
  font-size: 0.75rem;
  margin-left: 0.2rem;
}

/* Toggle sin/con */
.trades-capped-note {
  font-size: 0.68rem;
  color: var(--text-muted);
  font-weight: 400;
  margin-left: 0.4rem;
}
.trade-table-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.toggle-row {
  display: flex;
  gap: 0.25rem;
}
.toggle-btn {
  padding: 0.2rem 0.7rem;
  border-radius: 0.35rem;
  font-size: 0.75rem;
  border: 1px solid var(--border);
  background: transparent;
  color: var(--text-muted);
  cursor: pointer;
}
.toggle-btn.active {
  background: var(--surface);
  color: var(--text);
  border-color: var(--accent);
}

/* Tabla de trades */
.trades-wrap {
  overflow-x: auto;
  max-height: 400px;
  overflow-y: auto;
}
.trades-table .trade-row {
  cursor: pointer;
}
.trades-table .trade-row:hover td {
  background: rgba(255, 255, 255, 0.03);
}
.ts-col {
  white-space: nowrap;
  font-size: 0.75rem;
  font-family: monospace;
}

/* Modal detalle trade */
.detail-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 0.85rem;
}
.detail-table td {
  padding: 0.4rem 0.5rem;
  border-bottom: 1px solid rgba(255, 255, 255, 0.05);
}
.detail-table td:first-child {
  color: var(--text-muted);
  width: 42%;
}
.detail-table td:last-child {
  color: var(--text);
  font-weight: 500;
}

/* Tabla análisis por filtro */
.filter-analysis-wrap {
  overflow-x: auto;
}
.fa-legend {
  font-size: 0.68rem;
  font-weight: 400;
  color: var(--text-muted);
  margin-left: 0.5rem;
}
.fa-badge-on {
  font-size: 0.68rem;
  background: rgba(34, 197, 94, 0.15);
  color: var(--green);
  padding: 0.1rem 0.4rem;
  border-radius: 0.25rem;
}
.fa-badge-off {
  font-size: 0.68rem;
  background: rgba(100, 116, 139, 0.15);
  color: var(--text-muted);
  padding: 0.1rem 0.4rem;
  border-radius: 0.25rem;
}
.row-baseline td {
  color: var(--text-muted);
  font-size: 0.78rem;
}
.row-beneficial td {
  background: rgba(34, 197, 94, 0.04);
}
.row-harmful td {
  background: rgba(239, 68, 68, 0.04);
}
.fa-delta {
  font-weight: 700;
}
.fa-filtered {
  color: var(--text-muted);
}
.fa-hint {
  font-size: 0.75rem;
  color: var(--text-muted);
  margin-top: 0.25rem;
  padding: 0.4rem 0.6rem;
  background: var(--bg);
  border-radius: 0.4rem;
  border-left: 2px solid var(--accent);
}
.fa-notice {
  font-size: 0.8rem;
  color: var(--text-muted);
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: 0.5rem;
  padding: 0.6rem 0.9rem;
}

/* Selector de símbolo + tarjetas de filtros */
.sym-add-row {
  display: flex;
  gap: 0.5rem;
}
.sym-select {
  flex: 1;
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: 0.5rem;
  color: var(--text);
  padding: 0.4rem 0.75rem;
  font-size: 0.88rem;
}
.sym-select:focus,
.strat-select:focus {
  outline: none;
  border-color: var(--accent);
}
.strat-select {
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: 0.5rem;
  color: var(--text);
  padding: 0.4rem 0.55rem;
  font-size: 0.85rem;
  font-weight: 600;
}
.btn-add-sym {
  padding: 0.4rem 1rem;
  border-radius: 0.5rem;
  border: 1px solid var(--accent);
  background: rgba(99, 102, 241, 0.12);
  color: #a5b4fc;
  font-weight: 600;
  font-size: 0.85rem;
  cursor: pointer;
  white-space: nowrap;
}
.btn-add-sym:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}
.btn-add-sym:hover:not(:disabled) {
  background: rgba(99, 102, 241, 0.2);
}

.sym-cards {
  display: flex;
  flex-direction: column;
  gap: 0.75rem;
}
.sym-filter-card {
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: 0.75rem;
  overflow: hidden;
}
.sfc-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 0.5rem;
  padding: 0.5rem 0.9rem;
  background: rgba(255, 255, 255, 0.03);
  border-bottom: 1px solid var(--border);
}
.sfc-strat-badge {
  font-size: 0.7rem;
  font-weight: 700;
  padding: 0.15rem 0.45rem;
  border-radius: 0.9rem;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  flex-shrink: 0;
}
.strat-breakout {
  background: rgba(99, 102, 241, 0.18);
  color: #a5b4fc;
}
.strat-retest {
  background: rgba(34, 197, 94, 0.15);
  color: #4ade80;
}
.strat-bounce {
  background: rgba(251, 146, 60, 0.15);
  color: #fb923c;
}
.sfc-name {
  font-weight: 700;
  font-size: 0.9rem;
  color: var(--text);
}
.sfc-remove {
  background: transparent;
  border: none;
  color: var(--text-muted);
  cursor: pointer;
  font-size: 0.85rem;
  padding: 0.1rem 0.3rem;
  border-radius: 0.3rem;
}
.sfc-remove:hover {
  color: var(--red);
  background: rgba(239, 68, 68, 0.1);
}
.sfc-filters {
  padding: 0.5rem 0.9rem;
  display: flex;
  flex-direction: column;
  gap: 0.3rem;
}
.sfc-row {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  cursor: pointer;
  font-size: 0.82rem;
  color: var(--text);
}
.sfc-row input[type="checkbox"] {
  flex-shrink: 0;
  accent-color: var(--accent);
}
.sfc-fname {
  font-weight: 600;
  min-width: 130px;
}
.sfc-num {
  width: 58px;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 0.35rem;
  color: var(--text);
  padding: 0.15rem 0.4rem;
  font-size: 0.8rem;
  text-align: center;
}
.sfc-num:focus {
  outline: none;
  border-color: var(--accent);
}
.sfc-sep {
  color: var(--text-muted);
}
.sfc-unit {
  font-size: 0.75rem;
  color: var(--text-muted);
}
.sfc-hint {
  font-size: 0.72rem;
  color: var(--text-muted);
}
.sfc-section-label {
  font-size: 0.68rem;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  color: var(--text-muted);
  font-weight: 600;
  padding-top: 0.15rem;
  display: flex;
  align-items: center;
  gap: 0.4rem;
}
.btn-filter-info {
  background: transparent;
  border: 1px solid var(--border);
  border-radius: 50%;
  color: var(--accent);
  cursor: pointer;
  font-size: 0.68rem;
  width: 1.1rem;
  height: 1.1rem;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 0;
  line-height: 1;
  flex-shrink: 0;
}
.btn-filter-info:hover {
  background: rgba(99, 102, 241, 0.15);
}

/* Modal de información de filtros */
.filters-info-modal {
  max-width: 640px;
  width: 95vw;
  max-height: 80vh;
  overflow-y: auto;
}
.filters-info-body {
  display: flex;
  flex-direction: column;
  gap: 0.5rem;
  padding: 0.25rem 0;
}
.fi-section {
  font-size: 0.78rem;
  font-weight: 700;
  color: var(--accent);
  text-transform: uppercase;
  letter-spacing: 0.04em;
  padding: 0.5rem 0 0.15rem;
  border-top: 1px solid var(--border);
  margin-top: 0.25rem;
}
.fi-section:first-child {
  border-top: none;
  margin-top: 0;
}
.fi-section small {
  font-size: 0.7rem;
  font-weight: 400;
  text-transform: none;
  letter-spacing: 0;
  color: var(--text-muted);
}
.fi-filter {
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: 0.5rem;
  padding: 0.55rem 0.75rem;
  display: flex;
  flex-direction: column;
  gap: 0.2rem;
}
.fi-name {
  font-size: 0.82rem;
  font-weight: 700;
  color: var(--text);
}
.fi-desc {
  font-size: 0.75rem;
  color: var(--text-muted);
  line-height: 1.45;
}
.fi-config {
  font-size: 0.72rem;
  color: #a5b4fc;
  margin-top: 0.1rem;
}
.fi-config code {
  background: rgba(99, 102, 241, 0.12);
  padding: 0.05rem 0.3rem;
  border-radius: 0.25rem;
  font-size: 0.7rem;
}

/* Toggle de período */
.period-toggle {
  display: flex;
  border: 1px solid var(--border);
  border-radius: 0.5rem;
  overflow: hidden;
}
.ptbtn {
  flex: 1;
  padding: 0.35rem 0;
  border: none;
  background: transparent;
  color: var(--text-muted);
  cursor: pointer;
  font-size: 0.82rem;
  transition: all 0.15s;
}
.ptbtn.active {
  background: var(--accent);
  color: #fff;
  font-weight: 600;
}
.date-input {
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: 0.5rem;
  color: var(--text);
  padding: 0.4rem 0.75rem;
  font-size: 0.88rem;
  width: 100%;
}
.date-input:focus {
  outline: none;
  border-color: var(--accent);
}

/* Parámetros de retest expandibles */
.retest-params {
  margin-left: 1.5rem;
  display: flex;
  flex-direction: column;
  gap: 0.25rem;
  padding: 0.3rem 0.6rem;
  background: rgba(99, 102, 241, 0.06);
  border-left: 2px solid rgba(99, 102, 241, 0.3);
  border-radius: 0 0.35rem 0.35rem 0;
}
.retest-param {
  display: flex;
  align-items: center;
  gap: 0.4rem;
  font-size: 0.78rem;
}
.retest-label {
  min-width: 110px;
  color: var(--text-muted);
  font-weight: 500;
}
</style>
