import { useEffect, useState } from "react";
import { fetchAlerts, ackAlert } from "./lib/api";
import { fmtDate } from "./lib/format";
import { toast } from "sonner";
import { BellRinging, CheckCircle, WarningOctagon } from "@phosphor-icons/react";

export const AlertsPanel = () => {
  const [alerts, setAlerts] = useState([]);
  const [showAcked, setShowAcked] = useState(false);

  const load = async () => {
    const list = await fetchAlerts(!showAcked);
    setAlerts(list);
  };
  useEffect(() => {
    load();
    const t = setInterval(load, 12000);
    return () => clearInterval(t);
    // eslint-disable-next-line
  }, [showAcked]);

  const ack = async (id) => {
    await ackAlert(id);
    toast.success("Acknowledged");
    load();
  };

  return (
    <div className="max-w-[1600px] mx-auto px-6 py-8" data-testid="alerts-view">
      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 className="heading text-4xl font-bold flex items-center gap-3">
            <BellRinging size={28} className="text-amber-400" />
            Alerts
          </h1>
          <div className="overline mt-1 text-stone-500">Critical events and Mother Hen findings</div>
        </div>
        <label className="flex items-center gap-2 text-xs text-stone-400 cursor-pointer" data-testid="show-acked-toggle">
          <input type="checkbox" checked={showAcked} onChange={(e) => setShowAcked(e.target.checked)} className="accent-amber-500" />
          Show acknowledged
        </label>
      </div>

      <div className="space-y-2" data-testid="alerts-list">
        {alerts.map((a) => (
          <div key={a.id} className={`border rounded-sm p-4 flex items-start gap-4 ${
            a.severity === "critical" ? "border-rose-700/60 bg-rose-950/15" :
            a.severity === "warning" ? "border-amber-700/60 bg-amber-950/15" :
            "border-[#2E2B27] bg-[#14120F]"
          }`} data-testid={`alert-${a.id}`}>
            <WarningOctagon size={20} weight="fill" className={
              a.severity === "critical" ? "text-rose-400" :
              a.severity === "warning" ? "text-amber-400" : "text-stone-400"
            } />
            <div className="flex-1 min-w-0">
              <div className="flex items-center gap-2 flex-wrap">
                <span className="overline">{a.severity}</span>
                <span className="mono text-[10px] text-stone-500">· {a.category}</span>
                <span className="ml-auto mono text-[10px] text-stone-500">{fmtDate(a.created_at)}</span>
              </div>
              <div className="mt-1 font-semibold text-stone-100">{a.title}</div>
              <div className="text-sm text-stone-300 mt-0.5">{a.message}</div>
            </div>
            {!a.acknowledged && (
              <button onClick={() => ack(a.id)} className="px-3 py-1.5 text-xs rounded-sm bg-emerald-500/20 border border-emerald-700/60 text-emerald-300 hover:bg-emerald-500/30 flex items-center gap-1" data-testid={`ack-${a.id}`}>
                <CheckCircle size={12} weight="fill" /> Ack
              </button>
            )}
            {a.acknowledged && <span className="text-emerald-400 text-xs">acked</span>}
          </div>
        ))}
        {alerts.length === 0 && (
          <div className="border border-dashed border-[#2E2B27] rounded-sm p-8 text-center text-stone-500">
            No active alerts. Nice work.
          </div>
        )}
      </div>
    </div>
  );
};
