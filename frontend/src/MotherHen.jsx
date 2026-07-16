import { useEffect, useState } from "react";
import { fetchDecisions, approveDecision, fetchPolicies, updatePolicy, motherHenAnalyze, fetchDashboard } from "./lib/api";
import { fmtDate } from "./lib/format";
import { Sparkle, CheckCircle, XCircle, Sliders, Robot, Eye } from "@phosphor-icons/react";
import { toast } from "sonner";
import { motion } from "framer-motion";

const POLICY_ACTIONS = [
  { key: "temp_setpoint", label: "Temperature setpoint" },
  { key: "fan_speed", label: "Fan speed" },
  { key: "damper", label: "Damper position" },
  { key: "cool_flap", label: "Cool flap" },
  { key: "lighting", label: "Lighting" },
  { key: "ventilation", label: "Ventilation" },
];

export const MotherHenPanel = () => {
  const [decisions, setDecisions] = useState([]);
  const [policies, setPolicies] = useState([]);
  const [dashboard, setDashboard] = useState(null);
  const [tab, setTab] = useState("pending");

  const load = async () => {
    const [d, p, dash] = await Promise.all([fetchDecisions({ limit: 100 }), fetchPolicies(), fetchDashboard()]);
    setDecisions(d);
    setPolicies(p);
    setDashboard(dash);
  };

  useEffect(() => {
    load();
    const t = setInterval(load, 12000);
    return () => clearInterval(t);
  }, []);

  const pending = decisions.filter((d) => d.status === "pending_approval");
  const auto = decisions.filter((d) => d.status === "auto_applied");
  const done = decisions.filter((d) => d.status === "approved" || d.status === "rejected");
  const recs = decisions.filter((d) => d.status === "recommended");

  const setMap = { pending, recommended: recs, auto, history: done };
  const list = setMap[tab] || [];

  const approve = async (id, ok) => {
    await approveDecision(id, ok);
    toast.success(ok ? "Approved" : "Rejected");
    await load();
  };

  const changePolicy = async (action, mode) => {
    await updatePolicy(action, { mode });
    toast.success(`${action} policy → ${mode}`);
    await load();
  };

  const runAnalysisAll = async () => {
    if (!dashboard) return;
    toast.info("Mother Hen is thinking about your top 3 sheds…");
    const targets = dashboard.tiles
      .filter((t) => t.status === "critical" || t.status === "warning")
      .slice(0, 3);
    for (const t of targets) {
      try { await motherHenAnalyze(t.shed.id); } catch { /* ignore */ }
    }
    toast.success("Analysis complete. New entries in Recommended tab.");
    await load();
  };

  return (
    <div className="max-w-[1600px] mx-auto px-6 py-8" data-testid="mother-hen-panel">
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
        <div className="lg:col-span-2 border border-amber-800/60 bg-amber-950/10 rounded-sm p-6 relative overflow-hidden" data-testid="motherhen-hero">
          <div className="flex items-start gap-6">
            <div className="orb ai-breathe" />
            <div>
              <div className="overline text-amber-300">Mother Hen · self-learning overwatch</div>
              <h1 className="heading text-3xl font-bold mt-1">Every reading, every shed, every minute.</h1>
              <p className="text-stone-300 mt-2 text-sm max-w-2xl">
                A rules engine runs locally to react instantly. Claude Sonnet 4.5 provides deep reasoning
                on demand. Policies decide when she acts alone and when she asks first.
              </p>
              <div className="flex gap-6 mt-6">
                <Stat label="PENDING" v={pending.length} c="#F59E0B" />
                <Stat label="AUTO-APPLIED" v={auto.length} c="#10B981" />
                <Stat label="RECOMMENDED" v={recs.length} c="#60A5FA" />
                <Stat label="ARCHIVED" v={done.length} c="#A8A29E" />
              </div>
            </div>
          </div>
          <button onClick={runAnalysisAll} className="mt-6 px-4 py-2 text-xs rounded-sm border border-amber-500 bg-amber-500/20 text-amber-200 hover:bg-amber-500/30 flex items-center gap-2" data-testid="run-deep-analysis-btn">
            <Sparkle size={14} weight="fill" /> Run deep analysis on flagged sheds
          </button>
        </div>

        <div className="border border-[#2E2B27] rounded-sm bg-[#14120F] p-6" data-testid="policy-panel">
          <div className="flex items-center gap-2 mb-4">
            <Sliders size={14} className="text-amber-400" />
            <div className="overline">Per-action policy</div>
          </div>
          <div className="space-y-3">
            {POLICY_ACTIONS.map((a) => {
              const p = policies.find((x) => x.action_type === a.key);
              const mode = p?.mode || "recommend";
              return (
                <div key={a.key} className="flex items-center justify-between gap-3" data-testid={`policy-row-${a.key}`}>
                  <div className="text-sm text-stone-200">{a.label}</div>
                  <div className="flex items-center rounded-sm overflow-hidden border border-[#2E2B27]">
                    {["auto", "recommend", "manual"].map((m) => (
                      <button
                        key={m}
                        onClick={() => changePolicy(a.key, m)}
                        data-testid={`policy-${a.key}-${m}`}
                        className={`px-2.5 py-1 text-[10px] uppercase tracking-wider ${
                          mode === m
                            ? m === "auto" ? "bg-emerald-500/20 text-emerald-300"
                            : m === "recommend" ? "bg-amber-500/20 text-amber-300"
                            : "bg-stone-500/20 text-stone-300"
                            : "text-stone-500 hover:text-stone-200"
                        }`}
                      >
                        {m}
                      </button>
                    ))}
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      </div>

      <div className="flex items-center gap-1 mt-8 border-b border-[#2E2B27]" data-testid="decision-tabs">
        {[
          ["pending", "Pending approval", pending.length],
          ["recommended", "Recommended", recs.length],
          ["auto", "Auto-applied", auto.length],
          ["history", "History", done.length],
        ].map(([k, l, c]) => (
          <button
            key={k}
            onClick={() => setTab(k)}
            data-testid={`tab-${k}`}
            className={`px-4 py-3 text-xs uppercase tracking-wider border-b-2 ${
              tab === k ? "border-amber-500 text-amber-400" : "border-transparent text-stone-500 hover:text-stone-200"
            }`}
          >
            {l} <span className="ml-1 mono text-stone-500">({c})</span>
          </button>
        ))}
      </div>

      <div className="mt-4 space-y-2" data-testid="decision-list">
        {list.map((d) => (
          <DecisionRow key={d.id} d={d} onApprove={approve} dashboard={dashboard} />
        ))}
        {list.length === 0 && (
          <div className="border border-dashed border-[#2E2B27] rounded-sm p-8 text-center text-stone-500">
            Nothing here. Mother Hen sleeps well when everything is nominal.
          </div>
        )}
      </div>
    </div>
  );
};

const Stat = ({ label, v, c }) => (
  <div>
    <div className="overline" style={{ color: c }}>{label}</div>
    <div className="mono text-3xl font-bold" style={{ color: c }}>{v}</div>
  </div>
);

const DecisionRow = ({ d, onApprove, dashboard }) => {
  const shedName = dashboard?.tiles.find((t) => t.shed.id === d.shed_id)?.shed.name || d.shed_id.slice(0, 6);
  const urgencyCls = {
    high: "text-rose-400 border-rose-700/60 bg-rose-950/20",
    medium: "text-amber-300 border-amber-700/60 bg-amber-950/20",
    low: "text-stone-300 border-[#2E2B27] bg-[#14120F]",
  }[d.urgency] || "text-stone-300 border-[#2E2B27] bg-[#14120F]";
  const isLLM = d.ai_source === "llm";

  return (
    <motion.div initial={{ opacity: 0, y: 4 }} animate={{ opacity: 1, y: 0 }} className={`border rounded-sm p-4 ${urgencyCls}`} data-testid={`decision-${d.id}`}>
      <div className="flex items-start gap-3">
        <div className="pt-0.5">
          {isLLM ? <div className="orb-sm ai-pulse" /> : <Robot size={22} className="text-amber-500" />}
        </div>
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 flex-wrap">
            <span className="overline">{d.urgency}</span>
            <span className="mono text-[10px] text-stone-500">{d.ai_source.toUpperCase()}</span>
            <span className="mono text-[10px] text-stone-500">·</span>
            <span className="mono text-[10px] text-stone-500">{shedName}</span>
            <span className="mono text-[10px] text-stone-500">·</span>
            <span className="mono text-[10px] text-stone-500">{d.action_type}</span>
            <span className="ml-auto mono text-[10px] text-stone-500">{fmtDate(d.created_at)}</span>
          </div>
          <div className="mt-1.5 text-stone-100 font-semibold">{d.reasoning}</div>
          <div className="text-sm text-stone-300 mt-1 whitespace-pre-line">{d.recommendation}</div>
          {d.data_snapshot && Object.keys(d.data_snapshot).length > 0 && (
            <div className="mt-2 flex gap-3 text-[11px] mono text-stone-400">
              {Object.entries(d.data_snapshot).map(([k, v]) => (
                <span key={k}>{k}: <span className="text-stone-200">{String(v)}</span></span>
              ))}
              <span>confidence: <span className="text-stone-200">{Math.round(d.confidence * 100)}%</span></span>
            </div>
          )}
        </div>
        {d.status === "pending_approval" && (
          <div className="flex flex-col gap-2">
            <button onClick={() => onApprove(d.id, true)} className="px-3 py-1.5 text-xs rounded-sm bg-emerald-500/20 border border-emerald-700/60 text-emerald-300 hover:bg-emerald-500/30 flex items-center gap-1" data-testid={`approve-${d.id}`}>
              <CheckCircle size={12} weight="fill" /> Approve
            </button>
            <button onClick={() => onApprove(d.id, false)} className="px-3 py-1.5 text-xs rounded-sm bg-stone-500/10 border border-[#3E3A34] text-stone-400 hover:text-rose-300 flex items-center gap-1" data-testid={`reject-${d.id}`}>
              <XCircle size={12} /> Reject
            </button>
          </div>
        )}
        {d.status === "auto_applied" && (
          <div className="text-emerald-400 text-xs flex items-center gap-1"><Eye size={12} /> auto</div>
        )}
      </div>
    </motion.div>
  );
};
