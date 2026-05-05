import type { SignalStrength } from "../types";

const LABEL: Record<SignalStrength, string> = {
  strong: "Strong",
  weak: "Weak",
  none: "—",
};

export function SignalBadge({ strength }: { strength: SignalStrength | null | undefined }) {
  const s = strength ?? "none";
  return (
    <span className={`badge ${s}`}>
      <span className="badge-dot" />
      {LABEL[s]}
    </span>
  );
}
