<script setup>
import BotStatus from "./components/BotStatus.vue";
import OpenPositions from "./components/OpenPositions.vue";
import TradeHistory from "./components/TradeHistory.vue";
</script>

<template>
  <div class="layout">
    <header class="topbar">
      <div class="logo">📈 PaperTrade</div>
      <nav class="nav">
        <a
          href="http://localhost:8000/docs"
          target="_blank"
          rel="noopener"
          class="nav-link"
        >
          API Docs
        </a>
      </nav>
    </header>

    <main class="main">
      <section class="panel">
        <BotStatus />
      </section>

      <section class="panel">
        <OpenPositions />
      </section>

      <section class="panel">
        <TradeHistory />
      </section>
    </main>
  </div>
</template>

<style scoped>
.layout {
  display: flex;
  flex-direction: column;
  min-height: 100vh;
}

.topbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0.75rem 1.5rem;
  border-bottom: 1px solid var(--border);
  background: var(--surface);
  position: sticky;
  top: 0;
  z-index: 10;
}

.logo {
  font-size: 1rem;
  font-weight: 700;
  color: var(--text);
  letter-spacing: -0.02em;
}

.nav-link {
  font-size: 0.8rem;
  color: var(--text-muted);
  text-decoration: none;
}
.nav-link:hover {
  color: var(--text);
}

.main {
  padding: 1.5rem;
  display: flex;
  flex-direction: column;
  gap: 1.5rem;
  max-width: 1400px;
  width: 100%;
  margin: 0 auto;
}

.panel {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 1rem;
  padding: 1.25rem 1.5rem;
}

@media (max-width: 640px) {
  .main {
    padding: 1rem;
  }
  .panel {
    padding: 1rem;
  }
}
</style>
