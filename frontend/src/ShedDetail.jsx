import { useEffect, useState } from "react";
import {
  fetchShed, fetchLatest, fetchShedReadings, fetchBatches, motherHenAnalyze, ingestReading,
} from "./lib/api";
import { STATUS, fmtNum, fmtInt, fmtDate } from "./lib/format";
import { BreedBadge } from "./Dashboard";
import { ArrowLeft, Bird, Drop, Fan, Fire, Lightbulb, Thermometer, Wind, ChartLine, Sparkle } from "@phosphor-icons/react";
import { LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer, ReferenceLine } from "recharts";
import { toast } from "sonner";
import { motion } from "framer-motion";

export const ShedDetail = ({ shedId, onBack, onOpenBatch }) => {
  const [shed, setShed] = useState(null);
  const [latest, setLatest] = useState(null);
  const [readings, setReadings] = useState([]);
  const [batches, setBatches] = useState([]);
  const [analysis, setAnalysis] = useState(null);
  const [analyzing, setAnalyzing] = useState(false);

  const activeBatch = batches.find((b) => b.status === "active");

  const load = async () => {
    const [s, l, r, b] = await Promise.all([
      fetchShed(shedId),
      fetchLatest(shedId),
      fetchShedReadings(shedId, 50),
      fetchBatches({ shed_id: shedId }),
    ]);
    setShed(s);
    setLatest(l);
    setReadings(r.reverse());
    setBatches(b);
  };

  useEffect(() => {
    load();
    const t = setInterval(load, 12000);
    return () => clearInterval(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [shedId]);

  const askMotherHen = async () => {
    setAnalyzing(true);
    try {
      const res = await motherHenAnalyze(shedId);
      setAnalysis(res);
    } catch (e) {
      toast.error("Analysis failed");
    } finally {
      setAnalyzing(false);
    }
  };

  const simulateReading = async () => {
    if (!latest) return;
    const jitter = (v, d) => (v ?? 0) + (Math.random() - 0.5) * d;
    try {
      await ingestReading({
        shed_id: shedId,
        temp_c: Number(jitter(latest.temp_c, 1.4).toFixed(1)),
        temp_required_c: latest.temp_required_c,
        humidity_pct: Number(jitter(latest.humidity_pct, 4).toFixed(1)),
        ammonia_ppm: Number(jitter(latest.ammonia_ppm, 5).toFixed(1)),
        static_pressure_pa: Number(jitter(latest.static_pressure_pa, 5).toFixed(1)),
        airflow_pct: latest.airflow_pct,
        damper_pct: latest.damper_pct,
        cool_flap_pct: latest.cool_flap_pct,
        curtain_pct: latest.curtain_pct,
        heaters_on: latest.heaters_on,
        fans_on: latest.fans_on,
        lighting_pct: latest.lighting_pct,
        water_liters: latest.water_liters ? Number((latest.water_liters * (0.95 + Math.random() * 0.15)).toFixed(1)) : null,
        feed_kg: latest.feed_kg,
        mortality_today: Math.random() > 0.7 ? Math.floor(Math.random() * 4) : 0,
        avg_weight_g: latest.avg_weight_g,
      });
      toast.success("Reading ingested");
      await load();
    } catch (e) {
      toast.error("Ingest failed");
    }
  };

  if (!shed) return <div className="p-8 text-stone-400">Loading shed…</div>;

  const chartData = readings.map((r) => ({
    t: r.timestamp?.slice(11, 16),
    temp: r.temp_c,
    target: r.temp_required_c,
    humidity: r.humidity_pct,
    ammonia: r.ammonia_ppm,
  }));

  return (
    <div className="max-w-[1600px] mx-auto px-6 py-8" data-testid="shed-detail">
      <button onClick={onBack} className="overline flex items-center gap-2 text-stone-400 hover:text-amber-400" data-testid="back-btn">
        <ArrowLeft size={14} /> Back to overview
      </button>

      <div className="flex items-start justify-between mt-4 gap-6 flex-wrap">
        <div>
          <div className="flex items-center gap-3">
            <h1 className="heading text-4xl font-bold">{shed.name}</h1>
            {activeBatch && <BreedBadge breed={activeBatch.breed} />}
          </div>
          <div className="overline mt-1 text-stone-500">
            {shed.vendor_system || "Manual"} · Capacity {fmtInt(shed.capacity)} birds
            {activeBatch && <> · Day {daysSince(activeBatch.start_date)} · {fmtInt(activeBatch.bird_count_current)} birds</>}
          </div>
        </div>
        <div className="flex items-center gap-2">
          <button onClick={simulateReading} className="px-3 py-2 text-xs rounded-sm border border-[#3E3A34] text-stone-300 hover:text-amber-400 hover:border-amber-500" data-testid="simulate-reading-btn">
            Simulate new reading
          </button>
          <button onClick={askMotherHen} disabled={analyzing} className="px-4 py-2 text-xs rounded-sm border border-amber-500/60 text-amber-300 bg-amber-500/10 hover:bg-amber-500/20 flex items-center gap-2" data-testid="ask-mother-hen-btn">
            <Sparkle size={14} weight="fill" />
            {analyzing ? "Mother Hen analysing…" : "Ask Mother Hen"}
          </button>
        </div>
      </div>

      {analysis && (
        <motion.div initial={{ opacity: 0, y: 8 }} animate={{ opacity: 1, y: 0 }} className="mt-4 border border-amber-800/60 bg-amber-950/20 rounded-sm p-4" data-testid="mother-hen-analysis">
          <div className="flex items-center gap-2 mb-2">
            <div className="orb-sm ai-pulse" />
            <div className="overline text-amber-300">Mother Hen · deep analysis</div>
          </div>
          <div className="text-stone-200 text-sm whitespace-pre-line leading-relaxed">{analysis.summary}</div>
        </motion.div>
      )}

      {/* Live tile grid */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-3 mt-6" data-testid="live-metrics">
        <BigMetric label="TEMPERATURE" value={fmtNum(latest?.temp_c)} unit="°C" target={latest?.temp_required_c} icon={<Thermometer size={18} weight="fill" />} />
        <BigMetric label="HUMIDITY" value={fmtNum(latest?.humidity_pct, 0)} unit="%" icon={<Drop size={18} weight="fill" />} />
        <BigMetric label="AMMONIA" value={fmtNum(latest?.ammonia_ppm, 0)} unit="ppm" warnAbove={20} icon={<Wind size={18} weight="fill" />} />
        <BigMetric label="STATIC PRESSURE" value={fmtNum(latest?.static_pressure_pa, 0)} unit="Pa" icon={<Fan size={18} />} />
      </div>

      <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-6 gap-3 mt-3">
        <SmallMetric label="AIRFLOW" value={fmtNum(latest?.airflow_pct, 0)} unit="%" />
        <SmallMetric label="DAMPER" value={fmtNum(latest?.damper_pct, 0)} unit="%" />
        <SmallMetric label="COOL FLAP" value={fmtNum(latest?.cool_flap_pct, 0)} unit="%" />
        <SmallMetric label="CURTAIN" value={fmtNum(latest?.curtain_pct, 0)} unit="%" />
        <SmallMetric label="FANS ON" value={latest?.fans_on ?? "—"} icon={<Fan size={12} />} />
        <SmallMetric label="HEATERS" value={latest?.heaters_on ?? "—"} icon={<Fire size={12} />} />
      </div>

      <div className="grid grid-cols-2 md:grid-cols-4 gap-3 mt-3">
        <SmallMetric label="LIGHTING" value={fmtNum(latest?.lighting_pct, 0)} unit="%" icon={<Lightbulb size={12} />} />
        <SmallMetric label="WATER (24H)" value={fmtNum(latest?.water_liters, 0)} unit="L" icon={<Drop size={12} />} />
        <SmallMetric label="FEED (CUM)" value={fmtNum(latest?.feed_kg, 0)} unit="kg" />
        <SmallMetric label="AVG WEIGHT" value={fmtNum(latest?.avg_weight_g, 0)} unit="g" icon={<Bird size={12} />} />
      </div>

      <div className="mt-3 text-xs text-stone-500">
        Last reading: <span className="mono text-stone-300">{fmtDate(latest?.timestamp)}</span>
      </div>

      {/* Chart */}
      <div className="mt-8 border border-[#2E2B27] rounded-sm bg-[#14120F] p-4" data-testid="temperature-chart">
        <div className="flex items-center gap-2 mb-3">
          <ChartLine size={14} className="text-amber-400" />
          <div className="overline">Environment · last {readings.length} readings</div>
        </div>
        <div className="h-72">
          <ResponsiveContainer width="100%" height="100%">
            <LineChart data={chartData} margin={{ top: 8, right: 16, left: -12, bottom: 0 }}>
              <CartesianGrid stroke="#24211E" strokeDasharray="3 3" />
              <XAxis dataKey="t" stroke="#57534E" tick={{ fontSize: 10 }} />
              <YAxis stroke="#57534E" tick={{ fontSize: 10 }} />
              <Tooltip contentStyle={{ background: "#14120F", border: "1px solid #2E2B27", fontSize: 12, borderRadius: 3 }} />
              <Line type="monotone" dataKey="temp" stroke="#F59E0B" strokeWidth={2} dot={false} name="Temp °C" />
              <Line type="monotone" dataKey="target" stroke="#10B981" strokeWidth={1.5} strokeDasharray="4 4" dot={false} name="Target °C" />
              <Line type="monotone" dataKey="humidity" stroke="#60A5FA" strokeWidth={1.5} dot={false} name="RH %" />
              <Line type="monotone" dataKey="ammonia" stroke="#EF4444" strokeWidth={1.5} dot={false} name="NH3 ppm" />
              <ReferenceLine y={25} stroke="#EF4444" strokeDasharray="2 2" opacity={0.4} />
            </LineChart>
          </ResponsiveContainer>
        </div>
      </div>

      {/* Batches */}
      <div className="mt-8">
        <div className="overline mb-3">Batches on this shed</div>
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-3">
          {batches.map((b) => (
            <button
              key={b.id}
              onClick={() => onOpenBatch(b.id)}
              className="text-left border border-[#2E2B27] rounded-sm bg-[#14120F] p-4 hover:border-amber-500 transition-colors"
              data-testid={`batch-card-${b.id}`}
            >
              <div className="flex items-center justify-between">
                <BreedBadge breed={b.breed} />
                <span className={`text-[10px] mono ${b.status==="active"?"text-emerald-400":"text-stone-500"}`}>{b.status.toUpperCase()}</span>
              </div>
              <div className="mt-2 heading text-lg font-semibold">Started {b.start_date}</div>
              <div className="mt-2 grid grid-cols-3 gap-2 text-xs">
                <Stat label="BIRDS" val={fmtInt(b.bird_count_current)} />
                <Stat label="MORT." val={fmtInt(b.mortality_total)} />
                <Stat label="DAY" val={daysSince(b.start_date)} />
              </div>
            </button>
          ))}
        </div>
      </div>
    </div>
  );
};

const BigMetric = ({ label, value, unit, target, warnAbove, icon }) => {
  const num = typeof value === "string" ? parseFloat(value) : value;
  const warn = warnAbove != null && !Number.isNaN(num) && num > warnAbove;
  return (
    <div className="border border-[#2E2B27] rounded-sm bg-[#14120F] p-5 relative" data-testid={`bigmetric-${label.replace(/\s/g,'-').toLowerCase()}`}>
      <div className="flex items-center justify-between">
        <div className="overline flex items-center gap-1"><span className="text-amber-500">{icon}</span>{label}</div>
        {target != null && <div className="overline text-stone-500">TGT {target}°</div>}
      </div>
      <div className="mt-4 flex items-baseline gap-2">
        <span className={`big-num text-5xl ${warn?"text-rose-400":"text-stone-100"}`}>{value}</span>
        <span className="text-stone-500 mono text-sm">{unit}</span>
      </div>
    </div>
  );
};

const SmallMetric = ({ label, value, unit, icon }) => (
  <div className="data-cell" data-testid={`smallmetric-${label.replace(/\s/g,'-').toLowerCase()}`}>
    <div className="overline flex items-center gap-1">{icon && <span className="text-amber-500">{icon}</span>}{label}</div>
    <div className="big-num text-xl mt-1">
      {value} <span className="text-stone-500 text-xs">{unit}</span>
    </div>
  </div>
);

const Stat = ({ label, val }) => (
  <div>
    <div className="overline">{label}</div>
    <div className="mono text-sm text-stone-100">{val}</div>
  </div>
);

function daysSince(iso) {
  try {
    const d = new Date(iso);
    const diff = (Date.now() - d.getTime()) / 86400000;
    return Math.max(0, Math.floor(diff));
  } catch { return 0; }
}
