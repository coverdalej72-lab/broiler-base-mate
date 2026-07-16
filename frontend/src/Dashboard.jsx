import { useEffect, useMemo, useState } from "react";
import { fetchDashboard } from "./lib/api";
import { STATUS, fmtNum, fmtInt, fmtDate } from "./lib/format";
import { Thermometer, Drop, Wind, Bird, Warning, ArrowRight } from "@phosphor-icons/react";
import { motion } from "framer-motion";

export const Dashboard = ({ onOpenShed }) => {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [filter, setFilter] = useState("all");

  useEffect(() => {
    let alive = true;
    const load = async () => {
      try {
        const d = await fetchDashboard();
        if (alive) setData(d);
      } finally {
        if (alive) setLoading(false);
      }
    };
    load();
    const t = setInterval(load, 10000);
    return () => { alive = false; clearInterval(t); };
  }, []);

  const tiles = useMemo(() => {
    if (!data) return [];
    if (filter === "all") return data.tiles;
    return data.tiles.filter((t) => t.status === filter);
  }, [data, filter]);

  return (
    <div className="max-w-[1600px] mx-auto px-6 py-8" data-testid="dashboard-view">
      <SectionHero data={data} loading={loading} />

      <div className="flex items-center gap-2 mb-4 mt-8" data-testid="dashboard-filters">
        <span className="overline mr-2">Filter</span>
        {[
          ["all", "All"],
          ["critical", "Critical"],
          ["warning", "Warning"],
          ["healthy", "Healthy"],
          ["offline", "Offline"],
        ].map(([k, l]) => (
          <button
            key={k}
            data-testid={`filter-${k}`}
            onClick={() => setFilter(k)}
            className={`px-3 py-1.5 text-xs rounded-sm border ${
              filter === k
                ? "border-amber-500 text-amber-400 bg-amber-500/5"
                : "border-[#2E2B27] text-stone-400 hover:text-stone-100 hover:border-[#3E3A34]"
            }`}
          >
            {l}
          </button>
        ))}
      </div>

      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-4" data-testid="shed-grid">
        {tiles.map((t, i) => (
          <ShedTile key={t.shed.id} tile={t} onOpen={() => onOpenShed(t.shed.id)} delay={i * 0.03} />
        ))}
        {!loading && tiles.length === 0 && (
          <div className="col-span-full text-stone-500 text-center py-16 border border-dashed border-[#2E2B27] rounded-sm">
            No sheds match this filter.
          </div>
        )}
      </div>
    </div>
  );
};

const SectionHero = ({ data, loading }) => {
  const counts = data?.counts || {};
  const pending = data?.pending_decisions || 0;
  const alerts = data?.active_alerts || 0;
  const total = data?.total_sheds || 0;

  return (
    <div className="grid grid-cols-1 lg:grid-cols-3 gap-4" data-testid="dashboard-hero">
      <div className="lg:col-span-2 relative overflow-hidden border border-[#2E2B27] rounded-sm bg-[#14120F] p-6">
        <div className="flex items-start gap-6">
          <div className="orb ai-breathe" />
          <div className="flex-1">
            <div className="overline">Mother Hen</div>
            <h1 className="heading text-3xl md:text-4xl font-bold mt-1">
              {loading ? "Initialising overwatch…" :
                counts.critical > 0 ? `${counts.critical} shed${counts.critical>1?"s":""} ${counts.critical>1?"need":"needs"} attention.` :
                counts.warning > 0 ? `Watching ${total} sheds. ${counts.warning} on watch.` :
                `All ${total} sheds nominal. Flock is safe.`}
            </h1>
            <p className="text-stone-400 mt-2 text-sm max-w-2xl">
              24/7 overwatch across every farm you run. Traffic-light status per shed, breed-aware benchmarks, and instant corrective decisions.
            </p>
            <div className="flex gap-6 mt-6">
              <MiniStat label="TOTAL" value={total} testId="stat-total" />
              <MiniStat label="HEALTHY" value={counts.healthy || 0} color="#10B981" testId="stat-healthy" />
              <MiniStat label="WARNING" value={counts.warning || 0} color="#F59E0B" testId="stat-warning" />
              <MiniStat label="CRITICAL" value={counts.critical || 0} color="#EF4444" testId="stat-critical" />
            </div>
          </div>
        </div>
        <div className="absolute -right-16 -bottom-16 w-64 h-64 rounded-full bg-amber-500/5 blur-3xl pointer-events-none" />
      </div>

      <div className="border border-[#2E2B27] rounded-sm bg-[#14120F] p-6 flex flex-col justify-between" data-testid="hero-side">
        <div>
          <div className="overline">Attention Queue</div>
          <div className="mt-3 flex items-center justify-between">
            <span className="text-sm text-stone-400">Pending decisions</span>
            <span className="mono text-2xl font-bold text-amber-400" data-testid="pending-count">{pending}</span>
          </div>
          <div className="mt-2 flex items-center justify-between">
            <span className="text-sm text-stone-400">Unack alerts</span>
            <span className="mono text-2xl font-bold text-rose-400" data-testid="alerts-count">{alerts}</span>
          </div>
        </div>
        <div className="mt-4 text-xs text-stone-500 border-t border-[#2E2B27] pt-3">
          Snapshot updated every 10 seconds. Ingest data via <code className="text-amber-400">POST /api/readings</code>.
        </div>
      </div>
    </div>
  );
};

const MiniStat = ({ label, value, color, testId }) => (
  <div data-testid={testId}>
    <div className="overline" style={color ? { color } : {}}>{label}</div>
    <div className="mono text-3xl font-bold mt-1" style={color ? { color } : {}}>{value}</div>
  </div>
);

const ShedTile = ({ tile, onOpen, delay }) => {
  const st = STATUS[tile.status] || STATUS.offline;
  const latest = tile.latest || {};
  const batch = tile.batch;
  return (
    <motion.button
      initial={{ opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ delay, duration: 0.35 }}
      onClick={onOpen}
      data-testid={`shed-tile-${tile.shed.id}`}
      className="shed-tile text-left group"
    >
      <div className="flex items-center justify-between px-4 py-3 border-b border-[#24211E] bg-[#0F0E0C]">
        <div className="flex items-center gap-2 min-w-0">
          <span className={`status-dot ${st.cls}`} data-testid="shed-status-dot" />
          <div className="min-w-0">
            <div className="heading text-lg font-semibold truncate">{tile.shed.name}</div>
            <div className="overline truncate text-[9px]">{tile.farm?.name} · {tile.shed.vendor_system || "Manual"}</div>
          </div>
        </div>
        <ArrowRight size={16} className="text-stone-500 group-hover:text-amber-400 group-hover:translate-x-0.5 transition-transform" />
      </div>

      <div className="px-4 py-4 grid grid-cols-3 gap-3">
        <TileMetric icon={<Thermometer size={14} weight="fill" />} label="TEMP" value={latest.temp_c != null ? `${fmtNum(latest.temp_c)}°` : "—"} sub={tile.breed_target?.temp_c != null ? `t ${tile.breed_target.temp_c}°` : ""} />
        <TileMetric icon={<Drop size={14} weight="fill" />} label="RH" value={latest.humidity_pct != null ? `${fmtNum(latest.humidity_pct,0)}%` : "—"} sub={latest.static_pressure_pa != null ? `${fmtNum(latest.static_pressure_pa,0)} Pa` : ""} />
        <TileMetric icon={<Wind size={14} weight="fill" />} label="NH3" value={latest.ammonia_ppm != null ? `${fmtNum(latest.ammonia_ppm,0)}` : "—"} sub="ppm" />
      </div>

      <div className="px-4 pb-4 flex items-center justify-between gap-3">
        <div className="flex items-center gap-2 min-w-0">
          <BreedBadge breed={batch?.breed} />
          <span className="overline text-stone-500">Day {tile.grow_day || 0}</span>
        </div>
        <div className="flex items-center gap-2 text-xs text-stone-400">
          <Bird size={12} />
          <span className="mono">{fmtInt(batch?.bird_count_current)}</span>
        </div>
      </div>

      {tile.findings_count > 0 && (
        <div className="px-4 py-2 border-t border-[#24211E] bg-amber-500/5 flex items-center gap-2 text-xs">
          <Warning size={12} weight="fill" className="text-amber-400" />
          <span className="text-amber-300">{tile.findings_count} finding{tile.findings_count>1?"s":""} from Mother Hen</span>
        </div>
      )}
      <div className="absolute top-0 left-0 h-full w-[3px]" style={{ background: st.color, opacity: 0.9 }} />
    </motion.button>
  );
};

const TileMetric = ({ icon, label, value, sub }) => (
  <div className="data-cell" data-testid={`metric-${label.toLowerCase()}`}>
    <div className="flex items-center gap-1 overline mb-1">
      <span className="text-amber-500">{icon}</span>
      {label}
    </div>
    <div className="big-num text-xl">{value}</div>
    {sub && <div className="text-[10px] text-stone-500 mono mt-0.5">{sub}</div>}
  </div>
);

export const BreedBadge = ({ breed }) => {
  if (!breed) return <span className="overline text-stone-500">NO BATCH</span>;
  const isRoss = breed.includes("Ross");
  return (
    <span
      className={`px-2 py-0.5 text-[10px] rounded-sm border font-semibold tracking-wider ${
        isRoss ? "bg-emerald-500/10 border-emerald-600/50 text-emerald-300" : "bg-sky-500/10 border-sky-600/50 text-sky-300"
      }`}
      data-testid={`breed-badge-${breed.replace(/\s+/g,'-')}`}
    >
      {breed.toUpperCase()}
    </span>
  );
};

export const formatTime = fmtDate;
