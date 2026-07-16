import { useEffect, useState } from "react";
import { fetchBatch, fetchBatchReadings, fetchBreedCurve, closeBatch, fetchShed } from "./lib/api";
import { BreedBadge } from "./Dashboard";
import { fmtInt, fmtNum, fmtDay } from "./lib/format";
import { LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer, Legend } from "recharts";
import { ArrowLeft, ChartLine } from "@phosphor-icons/react";
import { toast } from "sonner";

export const BatchDetail = ({ batchId, onBack, onOpenShed }) => {
  const [batch, setBatch] = useState(null);
  const [readings, setReadings] = useState([]);
  const [weightCurve, setWeightCurve] = useState([]);
  const [tempCurve, setTempCurve] = useState([]);
  const [waterCurve, setWaterCurve] = useState([]);
  const [shed, setShed] = useState(null);

  useEffect(() => {
    (async () => {
      const b = await fetchBatch(batchId);
      setBatch(b);
      const s = await fetchShed(b.shed_id);
      setShed(s);
      const r = await fetchBatchReadings(batchId);
      setReadings(r);
      const [wc, tc, wtc] = await Promise.all([
        fetchBreedCurve(b.breed, "weight_g"),
        fetchBreedCurve(b.breed, "temp_c"),
        fetchBreedCurve(b.breed, "water_ml_per_bird"),
      ]);
      setWeightCurve(wc.curve);
      setTempCurve(tc.curve);
      setWaterCurve(wtc.curve);
    })();
  }, [batchId]);

  if (!batch) return <div className="p-8 text-stone-400">Loading batch…</div>;

  // Combine actual daily readings with breed target
  const dailyAgg = aggregateByDay(readings);
  const weightChart = weightCurve.map((c) => ({
    day: c.day,
    target: c.value,
    actual: dailyAgg.find((d) => d.day === c.day)?.avg_weight_g ?? null,
  }));
  const tempChart = tempCurve.map((c) => ({
    day: c.day,
    target: c.value,
    actual: dailyAgg.find((d) => d.day === c.day)?.temp_c ?? null,
  }));
  const waterChart = waterCurve.map((c) => ({
    day: c.day,
    target: c.value,
    actual: dailyAgg.find((d) => d.day === c.day)?.water_ml_per_bird ?? null,
  }));

  const doClose = async () => {
    const finalW = prompt("Final average weight (g)?");
    if (!finalW) return;
    const fcr = prompt("Final FCR?");
    try {
      await closeBatch(batchId, { final_weight_g: parseFloat(finalW), final_fcr: fcr ? parseFloat(fcr) : null });
      toast.success("Batch closed");
      const b = await fetchBatch(batchId);
      setBatch(b);
    } catch { toast.error("Close failed"); }
  };

  const growDay = daysSince(batch.start_date);
  const mortalityPct = batch.bird_count_start > 0 ? (batch.mortality_total / batch.bird_count_start) * 100 : 0;

  return (
    <div className="max-w-[1600px] mx-auto px-6 py-8" data-testid="batch-detail">
      <button onClick={onBack} className="overline flex items-center gap-2 text-stone-400 hover:text-amber-400" data-testid="back-btn">
        <ArrowLeft size={14} /> Back
      </button>

      <div className="mt-4 flex items-start justify-between flex-wrap gap-4">
        <div>
          <div className="flex items-center gap-3">
            <BreedBadge breed={batch.breed} />
            <span className={`text-xs mono ${batch.status==="active"?"text-emerald-400":"text-stone-500"}`}>{batch.status.toUpperCase()}</span>
          </div>
          <h1 className="heading text-4xl font-bold mt-1">Batch · {fmtDay(batch.start_date)}</h1>
          <div className="overline mt-1 text-stone-500">
            {shed?.name} · Day {growDay} · {fmtInt(batch.bird_count_current)}/{fmtInt(batch.bird_count_start)} birds
          </div>
        </div>
        <div className="flex items-center gap-2">
          <button onClick={() => onOpenShed(batch.shed_id)} className="px-3 py-2 text-xs rounded-sm border border-[#3E3A34] text-stone-300 hover:border-amber-500 hover:text-amber-400" data-testid="open-shed-btn">
            View shed live
          </button>
          {batch.status === "active" && (
            <button onClick={doClose} className="px-3 py-2 text-xs rounded-sm border border-rose-800/60 text-rose-300 bg-rose-950/20 hover:bg-rose-950/40" data-testid="close-batch-btn">
              Close batch
            </button>
          )}
        </div>
      </div>

      <div className="grid grid-cols-2 md:grid-cols-4 gap-3 mt-6" data-testid="batch-kpis">
        <Kpi label="MORTALITY" value={`${fmtNum(mortalityPct)}%`} sub={`${fmtInt(batch.mortality_total)} birds`} />
        <Kpi label="GROW DAY" value={growDay} sub={`start ${fmtDay(batch.start_date)}`} />
        <Kpi label="BIRDS ALIVE" value={fmtInt(batch.bird_count_current)} sub={`of ${fmtInt(batch.bird_count_start)}`} />
        <Kpi label="FINAL FCR" value={batch.final_fcr ? fmtNum(batch.final_fcr, 2) : "—"} sub={batch.final_weight_g ? `${fmtInt(batch.final_weight_g)} g avg` : "closed metrics"} />
      </div>

      <BenchmarkChart title="Body weight vs breed target" data={weightChart} unit="g" testId="chart-weight" />
      <BenchmarkChart title="Temperature vs breed target" data={tempChart} unit="°C" testId="chart-temp" />
      <BenchmarkChart title="Water per bird vs breed target" data={waterChart} unit="ml/bird" testId="chart-water" />
    </div>
  );
};

const Kpi = ({ label, value, sub }) => (
  <div className="border border-[#2E2B27] rounded-sm bg-[#14120F] p-4">
    <div className="overline">{label}</div>
    <div className="big-num text-3xl mt-1">{value}</div>
    {sub && <div className="text-xs text-stone-500 mono mt-1">{sub}</div>}
  </div>
);

const BenchmarkChart = ({ title, data, unit, testId }) => (
  <div className="mt-6 border border-[#2E2B27] rounded-sm bg-[#14120F] p-4" data-testid={testId}>
    <div className="flex items-center gap-2 mb-3">
      <ChartLine size={14} className="text-amber-400" />
      <div className="overline">{title} <span className="text-stone-500">({unit})</span></div>
    </div>
    <div className="h-64">
      <ResponsiveContainer width="100%" height="100%">
        <LineChart data={data} margin={{ top: 8, right: 16, left: -12, bottom: 0 }}>
          <CartesianGrid stroke="#24211E" strokeDasharray="3 3" />
          <XAxis dataKey="day" stroke="#57534E" tick={{ fontSize: 10 }} label={{ value: "Grow day", fill: "#57534E", fontSize: 10, position: "insideBottom", dy: 8 }} />
          <YAxis stroke="#57534E" tick={{ fontSize: 10 }} />
          <Tooltip contentStyle={{ background: "#14120F", border: "1px solid #2E2B27", fontSize: 12, borderRadius: 3 }} />
          <Legend wrapperStyle={{ fontSize: 10, color: "#A8A29E" }} />
          <Line type="monotone" dataKey="target" stroke="#10B981" strokeWidth={1.5} strokeDasharray="5 5" dot={false} name="Breed target" />
          <Line type="monotone" dataKey="actual" stroke="#F59E0B" strokeWidth={2} dot={{ r: 2 }} name="Actual" connectNulls />
        </LineChart>
      </ResponsiveContainer>
    </div>
  </div>
);

function daysSince(iso) {
  try {
    const d = new Date(iso);
    const diff = (Date.now() - d.getTime()) / 86400000;
    return Math.max(0, Math.floor(diff));
  } catch { return 0; }
}

function aggregateByDay(readings) {
  const map = new Map();
  for (const r of readings) {
    const day = r.grow_day ?? 0;
    const m = map.get(day) || { day, count: 0, temp_c: 0, avg_weight_g: 0, water_liters: 0, birds: 0 };
    m.count += 1;
    if (r.temp_c != null) m.temp_c += r.temp_c;
    if (r.avg_weight_g != null) m.avg_weight_g += r.avg_weight_g;
    if (r.water_liters != null) m.water_liters += r.water_liters;
    map.set(day, m);
  }
  return Array.from(map.values()).map((m) => ({
    day: m.day,
    temp_c: m.count ? m.temp_c / m.count : null,
    avg_weight_g: m.count ? m.avg_weight_g / m.count : null,
    // rough per-bird water (needs bird_count; we can pass it, else use raw litres)
    water_ml_per_bird: null,
  })).sort((a, b) => a.day - b.day);
}
