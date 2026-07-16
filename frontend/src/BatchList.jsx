import { useEffect, useState } from "react";
import { fetchBatches, fetchDashboard } from "./lib/api";
import { BreedBadge } from "./Dashboard";
import { fmtInt, fmtDay, fmtNum } from "./lib/format";
import { NewBatchDialog } from "./dialogs/NewBatchDialog";
import { Plus } from "@phosphor-icons/react";

export const BatchList = ({ onOpenBatch }) => {
  const [batches, setBatches] = useState([]);
  const [tiles, setTiles] = useState([]);
  const [showNew, setShowNew] = useState(false);
  const [filter, setFilter] = useState("active");

  const load = async () => {
    const [b, d] = await Promise.all([fetchBatches(), fetchDashboard()]);
    setBatches(b);
    setTiles(d.tiles || []);
  };
  useEffect(() => { load(); }, []);

  const visible = batches.filter((b) => filter === "all" || b.status === filter);

  return (
    <div className="max-w-[1600px] mx-auto px-6 py-8" data-testid="batches-view">
      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 className="heading text-4xl font-bold">Batches</h1>
          <div className="overline mt-1 text-stone-500">Every flock, from placement to harvest.</div>
        </div>
        <button onClick={() => setShowNew(true)} className="px-4 py-2 text-xs rounded-sm bg-amber-500 text-stone-950 font-semibold hover:bg-amber-400 flex items-center gap-2" data-testid="new-batch-btn">
          <Plus size={14} weight="bold" /> New batch
        </button>
      </div>

      <div className="flex gap-2 mb-4" data-testid="batches-filter">
        {[["active", "Active"], ["closed", "Closed"], ["all", "All"]].map(([k, l]) => (
          <button
            key={k}
            onClick={() => setFilter(k)}
            data-testid={`batches-filter-${k}`}
            className={`px-3 py-1.5 text-xs rounded-sm border ${
              filter === k
                ? "border-amber-500 text-amber-400 bg-amber-500/5"
                : "border-[#2E2B27] text-stone-400 hover:border-[#3E3A34]"
            }`}
          >
            {l}
          </button>
        ))}
      </div>

      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-3" data-testid="batch-grid">
        {visible.map((b) => {
          const shed = tiles.find((t) => t.shed.id === b.shed_id)?.shed;
          const mortPct = b.bird_count_start ? (b.mortality_total / b.bird_count_start) * 100 : 0;
          return (
            <button key={b.id} onClick={() => onOpenBatch(b.id)} className="text-left border border-[#2E2B27] rounded-sm bg-[#14120F] p-5 hover:border-amber-500 transition-colors" data-testid={`batch-row-${b.id}`}>
              <div className="flex items-center justify-between">
                <BreedBadge breed={b.breed} />
                <span className={`text-[10px] mono ${b.status === "active" ? "text-emerald-400" : "text-stone-500"}`}>
                  {b.status.toUpperCase()}
                </span>
              </div>
              <div className="mt-3 heading text-lg font-semibold">{shed?.name || "—"}</div>
              <div className="overline text-stone-500 mt-0.5">Started {fmtDay(b.start_date)}</div>
              <div className="mt-4 grid grid-cols-3 gap-2 text-xs">
                <div><div className="overline">BIRDS</div><div className="mono text-stone-100">{fmtInt(b.bird_count_current)}</div></div>
                <div><div className="overline">MORT</div><div className="mono text-stone-100">{fmtNum(mortPct)}%</div></div>
                <div><div className="overline">DAY</div><div className="mono text-stone-100">{daysSince(b.start_date)}</div></div>
              </div>
            </button>
          );
        })}
        {visible.length === 0 && (
          <div className="col-span-full text-stone-500 text-center py-16 border border-dashed border-[#2E2B27] rounded-sm">
            No batches yet. Start one from this button ↑
          </div>
        )}
      </div>

      {showNew && <NewBatchDialog onClose={() => setShowNew(false)} onCreated={load} />}
    </div>
  );
};

function daysSince(iso) {
  try {
    const d = new Date(iso);
    return Math.max(0, Math.floor((Date.now() - d.getTime()) / 86400000));
  } catch { return 0; }
}
