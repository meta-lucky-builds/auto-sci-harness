// The only component allowed to use the certified colour (a static test holds this).
import type { Badge, Case } from "../api/types.gen";

export const CERTIFYING = new Set(["VALIDATED", "RE-VALIDATED"]);

/** Green only when the API says the Court certified this case and the badge is a fresh-test badge, on a real store. */
export function mayShowCertified(badge: Badge, verdict: Case["verdict"], scripted: boolean): boolean {
  return !scripted && verdict === "CERTIFIED" && CERTIFYING.has(badge.name);
}

export function BadgeChip({ badge, verdict, scripted }: { badge: Badge; verdict: Case["verdict"]; scripted: boolean }) {
  if (mayShowCertified(badge, verdict, scripted)) {
    return <span className="badge badge-certified" data-t={`badge:${badge.name}`} title={badge.meaning}><span className="ring" aria-hidden="true" />{badge.form}</span>;
  }
  if (scripted && CERTIFYING.has(badge.name)) {
    return <span className="badge badge-example" data-t={`badge:${badge.name}`} title={badge.meaning}>{badge.form} (example)</span>;
  }
  return <span className="badge badge-support" data-t={`badge:${badge.name}`} title={badge.meaning}>{badge.form}</span>;
}

/** The one line of light under an earned verdict, under the same rule as the badge. In an example it is a dashed grey. */
export function VerdictLine({ c, scripted }: { c: Pick<Case, "badges" | "verdict">; scripted: boolean }) {
  if (c.badges.some((b) => mayShowCertified(b, c.verdict, scripted))) return <div className="verdict-line earned" aria-hidden="true" />;
  if (scripted && c.verdict === "CERTIFIED" && c.badges.some((b) => CERTIFYING.has(b.name))) return <div className="verdict-line example" aria-hidden="true" />;
  return <div className="verdict-line" aria-hidden="true" />;
}
