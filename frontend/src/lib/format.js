export const STATUS = {
  healthy: { label: "HEALTHY", color: "#10B981", cls: "status-healthy" },
  warning: { label: "WARNING", color: "#F59E0B", cls: "status-warning" },
  critical: { label: "CRITICAL", color: "#EF4444", cls: "status-critical" },
  offline: { label: "OFFLINE", color: "#57534E", cls: "status-offline" },
};

export const fmtNum = (v, digits = 1) => {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  return Number(v).toLocaleString(undefined, {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
};

export const fmtInt = (v) => {
  if (v === null || v === undefined) return "—";
  return Number(v).toLocaleString();
};

export const fmtDate = (iso) => {
  if (!iso) return "—";
  try {
    const d = new Date(iso);
    return d.toLocaleString(undefined, {
      month: "short",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
    });
  } catch {
    return iso;
  }
};

export const fmtDay = (iso) => {
  if (!iso) return "—";
  try {
    return new Date(iso).toLocaleDateString(undefined, { month: "short", day: "2-digit", year: "numeric" });
  } catch {
    return iso;
  }
};
