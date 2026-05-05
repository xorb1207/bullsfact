interface Props {
  label: string;
  value: string;
  sub?: string;
  tone?: "neutral" | "pos" | "neg";
}

export function StatCard({ label, value, sub, tone = "neutral" }: Props) {
  const valueClass =
    tone === "pos" ? "stat-value pos" : tone === "neg" ? "stat-value neg" : "stat-value";
  return (
    <div className="stat">
      <div className="stat-label">{label}</div>
      <div className={valueClass}>{value}</div>
      {sub && <div className="stat-sub">{sub}</div>}
    </div>
  );
}
