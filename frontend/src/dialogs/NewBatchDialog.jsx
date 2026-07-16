import { useEffect, useState } from "react";
import { fetchSheds, createBatch } from "../lib/api";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter } from "../components/ui/dialog";
import { Input } from "../components/ui/input";
import { Label } from "../components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "../components/ui/select";
import { Button } from "../components/ui/button";
import { toast } from "sonner";

export const NewBatchDialog = ({ onClose, onCreated, defaultShedId }) => {
  const [sheds, setSheds] = useState([]);
  const [shedId, setShedId] = useState(defaultShedId || "");
  const [breed, setBreed] = useState("Ross 308");
  const [startDate, setStartDate] = useState(new Date().toISOString().slice(0, 10));
  const [birdCount, setBirdCount] = useState(20000);
  const [notes, setNotes] = useState("");
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    fetchSheds().then(setSheds);
  }, []);

  const submit = async () => {
    if (!shedId) return toast.error("Choose a shed");
    setLoading(true);
    try {
      await createBatch({ shed_id: shedId, breed, start_date: startDate, bird_count_start: Number(birdCount), notes });
      toast.success("Batch started");
      onCreated && (await onCreated());
      onClose();
    } catch {
      toast.error("Failed to start batch");
    } finally {
      setLoading(false);
    }
  };

  return (
    <Dialog open={true} onOpenChange={onClose}>
      <DialogContent className="bg-[#14120F] border-[#2E2B27]" data-testid="new-batch-dialog">
        <DialogHeader>
          <DialogTitle className="heading text-xl">Start a new batch</DialogTitle>
        </DialogHeader>
        <div className="space-y-4">
          <div>
            <Label className="overline">Shed</Label>
            <Select value={shedId} onValueChange={setShedId}>
              <SelectTrigger data-testid="new-batch-shed" className="bg-[#0F0E0C] border-[#2E2B27] mt-1">
                <SelectValue placeholder="Select a shed" />
              </SelectTrigger>
              <SelectContent className="bg-[#14120F] border-[#2E2B27]">
                {sheds.map((s) => (
                  <SelectItem key={s.id} value={s.id}>{s.name}</SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <div>
            <Label className="overline">Breed</Label>
            <Select value={breed} onValueChange={setBreed}>
              <SelectTrigger data-testid="new-batch-breed" className="bg-[#0F0E0C] border-[#2E2B27] mt-1">
                <SelectValue />
              </SelectTrigger>
              <SelectContent className="bg-[#14120F] border-[#2E2B27]">
                <SelectItem value="Ross 308">Ross 308</SelectItem>
                <SelectItem value="Cobb 500">Cobb 500</SelectItem>
              </SelectContent>
            </Select>
          </div>
          <div className="grid grid-cols-2 gap-3">
            <div>
              <Label className="overline">Start date</Label>
              <Input type="date" value={startDate} onChange={(e) => setStartDate(e.target.value)} className="bg-[#0F0E0C] border-[#2E2B27] mt-1 mono" data-testid="new-batch-date" />
            </div>
            <div>
              <Label className="overline">Bird count</Label>
              <Input type="number" value={birdCount} onChange={(e) => setBirdCount(e.target.value)} className="bg-[#0F0E0C] border-[#2E2B27] mt-1 mono" data-testid="new-batch-birds" />
            </div>
          </div>
          <div>
            <Label className="overline">Notes</Label>
            <Input value={notes} onChange={(e) => setNotes(e.target.value)} className="bg-[#0F0E0C] border-[#2E2B27] mt-1" data-testid="new-batch-notes" />
          </div>
        </div>
        <DialogFooter>
          <Button variant="ghost" onClick={onClose} data-testid="new-batch-cancel">Cancel</Button>
          <Button onClick={submit} disabled={loading} className="bg-amber-500 text-stone-950 hover:bg-amber-400" data-testid="new-batch-submit">
            {loading ? "Starting…" : "Start batch"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
};
