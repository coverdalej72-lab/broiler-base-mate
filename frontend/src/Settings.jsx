import { useEffect, useState } from "react";
import { fetchFarms, fetchSheds, createFarm, createShed, deleteFarm, deleteShed, ingestReading, fetchBreeds } from "./lib/api";
import { Button } from "./components/ui/button";
import { Input } from "./components/ui/input";
import { Label } from "./components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "./components/ui/select";
import { Buildings, Barn, Plus, Trash, Broadcast } from "@phosphor-icons/react";
import { toast } from "sonner";

export const Settings = () => {
  const [farms, setFarms] = useState([]);
  const [sheds, setSheds] = useState([]);
  const [breeds, setBreeds] = useState({ breeds: [], targets: {} });

  // farm form
  const [fName, setFName] = useState("");
  const [fLoc, setFLoc] = useState("");
  const [fVendor, setFVendor] = useState("");

  // shed form
  const [sFarm, setSFarm] = useState("");
  const [sName, setSName] = useState("");
  const [sCap, setSCap] = useState(20000);
  const [sVendor, setSVendor] = useState("");

  // ingest form
  const [ingestShed, setIngestShed] = useState("");
  const [temp, setTemp] = useState("");
  const [rh, setRh] = useState("");
  const [nh3, setNh3] = useState("");
  const [water, setWater] = useState("");
  const [mort, setMort] = useState("");

  const load = async () => {
    const [f, s, b] = await Promise.all([fetchFarms(), fetchSheds(), fetchBreeds()]);
    setFarms(f); setSheds(s); setBreeds(b);
  };
  useEffect(() => { load(); }, []);

  const addFarm = async () => {
    if (!fName) return toast.error("Farm name required");
    await createFarm({ name: fName, location: fLoc, vendor_system: fVendor });
    toast.success("Farm added");
    setFName(""); setFLoc(""); setFVendor("");
    await load();
  };

  const addShed = async () => {
    if (!sFarm || !sName) return toast.error("Farm and name required");
    await createShed({ farm_id: sFarm, name: sName, capacity: Number(sCap), vendor_system: sVendor });
    toast.success("Shed added");
    setSName(""); setSVendor(""); setSCap(20000);
    await load();
  };

  const rmFarm = async (id) => {
    if (!window.confirm("Delete this farm and all its sheds?")) return;
    await deleteFarm(id);
    toast.success("Farm deleted");
    await load();
  };
  const rmShed = async (id) => {
    if (!window.confirm("Delete this shed?")) return;
    await deleteShed(id);
    toast.success("Shed deleted");
    await load();
  };

  const submitReading = async () => {
    if (!ingestShed) return toast.error("Choose a shed");
    try {
      await ingestReading({
        shed_id: ingestShed,
        temp_c: temp === "" ? null : Number(temp),
        humidity_pct: rh === "" ? null : Number(rh),
        ammonia_ppm: nh3 === "" ? null : Number(nh3),
        water_liters: water === "" ? null : Number(water),
        mortality_today: mort === "" ? null : Number(mort),
      });
      toast.success("Reading ingested");
      setTemp(""); setRh(""); setNh3(""); setWater(""); setMort("");
    } catch { toast.error("Ingest failed"); }
  };

  return (
    <div className="max-w-[1600px] mx-auto px-6 py-8" data-testid="settings-view">
      <h1 className="heading text-4xl font-bold">Settings</h1>
      <div className="overline mt-1 text-stone-500">Farms, sheds, and ingestion.</div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4 mt-8">
        {/* Farms */}
        <Card title="Farms" icon={<Buildings size={16} className="text-amber-400" />} testId="farms-card">
          <div className="space-y-2 mb-4">
            {farms.map((f) => (
              <div key={f.id} className="flex items-center justify-between p-2 border border-[#2E2B27] rounded-sm bg-[#0F0E0C]" data-testid={`farm-row-${f.id}`}>
                <div>
                  <div className="font-semibold text-stone-100">{f.name}</div>
                  <div className="text-xs text-stone-500 mono">{f.location} · {f.vendor_system || "—"}</div>
                </div>
                <button onClick={() => rmFarm(f.id)} className="text-stone-500 hover:text-rose-400" data-testid={`delete-farm-${f.id}`}>
                  <Trash size={14} />
                </button>
              </div>
            ))}
          </div>
          <div className="grid grid-cols-2 gap-2">
            <Field label="Name"><Input value={fName} onChange={(e) => setFName(e.target.value)} className="bg-[#0F0E0C] border-[#2E2B27]" data-testid="farm-name-input" /></Field>
            <Field label="Location"><Input value={fLoc} onChange={(e) => setFLoc(e.target.value)} className="bg-[#0F0E0C] border-[#2E2B27]" data-testid="farm-loc-input" /></Field>
            <Field label="Vendor system"><Input value={fVendor} onChange={(e) => setFVendor(e.target.value)} className="bg-[#0F0E0C] border-[#2E2B27]" data-testid="farm-vendor-input" /></Field>
            <div className="flex items-end">
              <Button onClick={addFarm} className="w-full bg-amber-500 text-stone-950 hover:bg-amber-400" data-testid="add-farm-btn">
                <Plus size={14} className="mr-1" /> Add farm
              </Button>
            </div>
          </div>
        </Card>

        {/* Sheds */}
        <Card title="Sheds" icon={<Barn size={16} className="text-amber-400" />} testId="sheds-card">
          <div className="space-y-2 mb-4 max-h-64 overflow-auto pr-1">
            {sheds.map((s) => (
              <div key={s.id} className="flex items-center justify-between p-2 border border-[#2E2B27] rounded-sm bg-[#0F0E0C]" data-testid={`shed-row-${s.id}`}>
                <div>
                  <div className="font-semibold text-stone-100">{s.name}</div>
                  <div className="text-xs text-stone-500 mono">{farms.find((f) => f.id === s.farm_id)?.name} · cap {s.capacity}</div>
                </div>
                <button onClick={() => rmShed(s.id)} className="text-stone-500 hover:text-rose-400" data-testid={`delete-shed-${s.id}`}>
                  <Trash size={14} />
                </button>
              </div>
            ))}
          </div>
          <div className="grid grid-cols-2 gap-2">
            <Field label="Farm">
              <Select value={sFarm} onValueChange={setSFarm}>
                <SelectTrigger className="bg-[#0F0E0C] border-[#2E2B27]" data-testid="shed-farm-select"><SelectValue placeholder="Choose farm" /></SelectTrigger>
                <SelectContent className="bg-[#14120F] border-[#2E2B27]">
                  {farms.map((f) => <SelectItem key={f.id} value={f.id}>{f.name}</SelectItem>)}
                </SelectContent>
              </Select>
            </Field>
            <Field label="Shed name"><Input value={sName} onChange={(e) => setSName(e.target.value)} className="bg-[#0F0E0C] border-[#2E2B27]" data-testid="shed-name-input" /></Field>
            <Field label="Capacity"><Input type="number" value={sCap} onChange={(e) => setSCap(e.target.value)} className="bg-[#0F0E0C] border-[#2E2B27] mono" data-testid="shed-cap-input" /></Field>
            <Field label="Vendor system"><Input value={sVendor} onChange={(e) => setSVendor(e.target.value)} className="bg-[#0F0E0C] border-[#2E2B27]" data-testid="shed-vendor-input" /></Field>
            <div className="col-span-2">
              <Button onClick={addShed} className="w-full bg-amber-500 text-stone-950 hover:bg-amber-400" data-testid="add-shed-btn">
                <Plus size={14} className="mr-1" /> Add shed
              </Button>
            </div>
          </div>
        </Card>

        {/* Manual ingest */}
        <Card title="Manual reading" icon={<Broadcast size={16} className="text-amber-400" />} testId="ingest-card">
          <p className="text-xs text-stone-500 mb-3">
            Vendor systems POST to <code className="text-amber-300">/api/readings</code>.
            Use this form for manual capture or a quick test.
          </p>
          <div className="grid grid-cols-2 gap-2">
            <Field label="Shed">
              <Select value={ingestShed} onValueChange={setIngestShed}>
                <SelectTrigger className="bg-[#0F0E0C] border-[#2E2B27]" data-testid="ingest-shed"><SelectValue placeholder="Choose shed" /></SelectTrigger>
                <SelectContent className="bg-[#14120F] border-[#2E2B27]">
                  {sheds.map((s) => <SelectItem key={s.id} value={s.id}>{s.name}</SelectItem>)}
                </SelectContent>
              </Select>
            </Field>
            <Field label="Temp °C"><Input type="number" value={temp} onChange={(e) => setTemp(e.target.value)} className="bg-[#0F0E0C] border-[#2E2B27] mono" data-testid="ingest-temp" /></Field>
            <Field label="Humidity %"><Input type="number" value={rh} onChange={(e) => setRh(e.target.value)} className="bg-[#0F0E0C] border-[#2E2B27] mono" data-testid="ingest-rh" /></Field>
            <Field label="Ammonia ppm"><Input type="number" value={nh3} onChange={(e) => setNh3(e.target.value)} className="bg-[#0F0E0C] border-[#2E2B27] mono" data-testid="ingest-nh3" /></Field>
            <Field label="Water L (24h)"><Input type="number" value={water} onChange={(e) => setWater(e.target.value)} className="bg-[#0F0E0C] border-[#2E2B27] mono" data-testid="ingest-water" /></Field>
            <Field label="Mortality today"><Input type="number" value={mort} onChange={(e) => setMort(e.target.value)} className="bg-[#0F0E0C] border-[#2E2B27] mono" data-testid="ingest-mort" /></Field>
            <div className="col-span-2">
              <Button onClick={submitReading} className="w-full bg-amber-500 text-stone-950 hover:bg-amber-400" data-testid="ingest-submit">
                Submit reading
              </Button>
            </div>
          </div>
        </Card>

        {/* Breed profiles */}
        <Card title="Breed profiles" icon={<Barn size={16} className="text-amber-400" />} testId="breeds-card">
          <div className="space-y-3">
            {(breeds.breeds || []).map((b) => {
              const t = breeds.targets?.[b] || {};
              return (
                <div key={b} className="p-3 border border-[#2E2B27] rounded-sm bg-[#0F0E0C]" data-testid={`breed-${b}`}>
                  <div className="flex items-center justify-between">
                    <div className="heading text-lg font-semibold">{b}</div>
                    <span className="overline">Seeded curve</span>
                  </div>
                  <div className="grid grid-cols-2 gap-2 mt-2 text-xs">
                    <div><div className="overline">TARGET FCR (42d)</div><div className="mono text-stone-100">{t.target_fcr_at_42d}</div></div>
                    <div><div className="overline">TARGET MORT.</div><div className="mono text-stone-100">{t.target_mortality_pct}%</div></div>
                  </div>
                </div>
              );
            })}
          </div>
        </Card>
      </div>
    </div>
  );
};

const Card = ({ title, icon, children, testId }) => (
  <section className="border border-[#2E2B27] rounded-sm bg-[#14120F] p-6" data-testid={testId}>
    <div className="flex items-center gap-2 mb-4">
      {icon}
      <div className="overline">{title}</div>
    </div>
    {children}
  </section>
);

const Field = ({ label, children }) => (
  <div>
    <Label className="overline">{label}</Label>
    <div className="mt-1">{children}</div>
  </div>
);
