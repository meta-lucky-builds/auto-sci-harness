// The only component allowed to use the certified colour (a static test holds this).
import type { Badge, Case } from "../api/types.gen";

export const CERTIFYING = new Set(["VALIDATED", "RE-VALIDATED"]);

/** Green only when the API says the Court certified this case and the badge is a fresh-test badge, on a real store. */
export function mayShowCertified(badge: Badge, verdict: Case["verdict"], scripted: boolean): boolean {
  return !scripted && verdict === "CERTIFIED" && CERTIFYING.has(badge.name);
}

export function BadgeChip({ badge, verdict, scripted }: { badge: Badge; verdict: Case["verdict"]; scripted: boolean }) {
  if (mayShowCertified(badge, verdict, scripted)) {
    return <span className="badge badge-certified" title={badge.meaning}>{badge.form}</span>;
  }
  if (scripted && CERTIFYING.has(badge.name)) {
    return <span className="badge badge-example" title={badge.meaning}>{badge.form} (example)</span>;
  }
  return <span className="badge badge-support" title={badge.meaning}>{badge.form}</span>;
}
