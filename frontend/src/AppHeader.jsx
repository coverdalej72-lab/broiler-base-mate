import { useEffect, useState } from "react";
import { fetchAlerts, fetchDashboard } from "./lib/api";
import { motion } from "framer-motion";
import { Eye } from "@phosphor-icons/react";

export const AppHeader = ({ current = "dashboard", onNav }) => {
  const [pulse, setPulse] = useState(0);
  const [counts, setCounts] = useState({ healthy: 0, warning: 0, critical: 0, offline: 0 });
  const [alerts, setAlerts] = useState(0);
  const [totalSheds, setTotalSheds] = useState(0);

  useEffect(() => {
    let alive = true;
    const load = async () => {
      try {
        const [d, a] = await Promise.all([fetchDashboard(), fetchAlerts(true)]);
        if (!alive) return;
        setCounts(d.counts || {});
        setTotalSheds(d.total_sheds || 0);
        setAlerts(a.length || 0);
      } catch (_e) { /* silent */ }
    };
    load();
    const t = setInterval(load, 15000);
    const p = setInterval(() => setPulse((v) => v + 1), 1200);
    return () => { alive = false; clearInterval(t); clearInterval(p); };
  }, []);

  const items = [
    { key: "dashboard", label: "OVERVIEW" },
    { key: "batches", label: "BATCHES" },
    { key: "mother-hen", label: "MOTHER HEN" },
    { key: "alerts", label: "ALERTS" },
    { key: "settings", label: "SETTINGS" },
  ];

  return (
    <header className="sticky top-0 z-30 backdrop-blur-md bg-[#0F0E0C]/85 border-b border-[#2E2B27]" data-testid="app-header">
      <div className="max-w-[1600px] mx-auto px-6 py-4 flex items-center gap-8">
        <div className="flex items-center gap-3">
          <motion.div
            className="orb ai-breathe"
            animate={{ boxShadow: pulse % 2 === 0 ? "0 0 24px rgba(245,158,11,0.55)" : "0 0 32px rgba(245,158,11,0.75)" }}
            transition={{ duration: 1.2 }}
            data-testid="mother-hen-orb"
          />
          <div>
            <div className="heading text-xl font-bold tracking-tight">COOP OVERWATCH</div>
            <div className="overline flex items-center gap-2">
              <Eye size={12} weight="fill" className="text-amber-500" />
              <span>Mother Hen watching {totalSheds} sheds</span>
            </div>
          </div>
        </div>

        <nav className="flex items-center gap-1 ml-4" data-testid="main-nav">
          {items.map((it) => (
            <button
              key={it.key}
              data-testid={`nav-${it.key}`}
              onClick={() => onNav && onNav(it.key)}
              className={`overline px-3 py-2 rounded-sm border ${
                current === it.key
                  ? "border-amber-500 text-amber-400 bg-amber-500/5"
                  : "border-transparent text-stone-400 hover:text-stone-100 hover:border-[#3E3A34]"
              }`}
            >
              {it.label}
            </button>
          ))}
        </nav>

        <div className="ml-auto flex items-center gap-3" data-testid="global-status">
          <StatChip label="HEALTHY" value={counts.healthy || 0} dotCls="status-healthy" />
          <StatChip label="WARNING" value={counts.warning || 0} dotCls="status-warning" />
          <StatChip label="CRITICAL" value={counts.critical || 0} dotCls="status-critical" />
          <StatChip label="ALERTS" value={alerts} dotCls="status-warning" testId="chip-alerts" />
        </div>
      </div>
    </header>
  );
};

const StatChip = ({ label, value, dotCls, testId }) => (
  <div className="flex items-center gap-2 border border-[#2E2B27] px-3 py-1.5 rounded-sm bg-[#14120F]" data-testid={testId || `chip-${label.toLowerCase()}`}>
    <span className={`status-dot ${dotCls}`} />
    <span className="overline">{label}</span>
    <span className="mono text-sm font-semibold text-stone-100">{value}</span>
  </div>
);
