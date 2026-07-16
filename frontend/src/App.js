import { useState } from "react";
import "./App.css";
import { AppHeader } from "./AppHeader";
import { Dashboard } from "./Dashboard";
import { ShedDetail } from "./ShedDetail";
import { BatchDetail } from "./BatchDetail";
import { BatchList } from "./BatchList";
import { MotherHenPanel } from "./MotherHen";
import { AlertsPanel } from "./AlertsPanel";
import { Settings } from "./Settings";
import { Toaster } from "./components/ui/sonner";

function App() {
  const [view, setView] = useState("dashboard");
  const [shedId, setShedId] = useState(null);
  const [batchId, setBatchId] = useState(null);

  const goDashboard = () => { setShedId(null); setBatchId(null); setView("dashboard"); };
  const openShed = (id) => { setShedId(id); setBatchId(null); setView("shed"); };
  const openBatch = (id) => { setBatchId(id); setView("batch"); };

  return (
    <div className="min-h-screen bg-[#0F0E0C] text-stone-100 dark">
      <AppHeader current={view.startsWith("shed") || view === "dashboard" ? "dashboard" : view} onNav={(k) => {
        setShedId(null); setBatchId(null); setView(k);
      }} />

      {view === "dashboard" && <Dashboard onOpenShed={openShed} />}
      {view === "shed" && shedId && <ShedDetail shedId={shedId} onBack={goDashboard} onOpenBatch={openBatch} />}
      {view === "batch" && batchId && <BatchDetail batchId={batchId} onBack={() => setView("batches")} onOpenShed={openShed} />}
      {view === "batches" && <BatchList onOpenBatch={openBatch} />}
      {view === "mother-hen" && <MotherHenPanel />}
      {view === "alerts" && <AlertsPanel />}
      {view === "settings" && <Settings />}

      <Toaster theme="dark" position="bottom-right" richColors />
    </div>
  );
}

export default App;
