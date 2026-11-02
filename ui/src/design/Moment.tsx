// The Promote moment, once, after the API has said the promotion happened. The Lab's heartbeat slows and stops, the
// screen goes dark, the claim is pressed into one line, and that line opens onto the page behind it. It is the colour
// of paper, never the green: fixing a claim certifies nothing, and the words say so. A static test holds both.
import { useEffect, useRef, useState } from "react";
import { calm, slowHeart } from "./motion";

export const MOMENT_MS = 1900;
const CALM_MS = 400;

export function PromoteMoment({ sentence, onDone }: { sentence: string; onDone: () => void }) {
  const quiet = useRef(calm());
  const [over, setOver] = useState(false);
  const done = useRef(onDone);
  done.current = onDone;

  const finish = () => { slowHeart(1); setOver(true); done.current(); };
  useEffect(() => {
    let id = 0;
    if (!quiet.current) {
      const t0 = performance.now();
      id = requestAnimationFrame(function ease(t) {  // the Lab's motion runs down to a stop over the first third
        const p = Math.min(1, (t - t0) / 650);
        slowHeart((1 - p) ** 2);
        if (p < 1) id = requestAnimationFrame(ease);
      });
    }
    const end = setTimeout(finish, quiet.current ? CALM_MS : MOMENT_MS);
    const skip = () => finish();
    window.addEventListener("keydown", skip);
    return () => { cancelAnimationFrame(id); clearTimeout(end); window.removeEventListener("keydown", skip); slowHeart(1); };
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  if (over) return null;
  return (
    <div className={`moment-veil ${quiet.current ? "moment-calm" : ""}`} role="status" onClick={finish}>
      <p className="moment-claim">{sentence}</p>
      <div className="moment-line" />
      <span className="moment-point" />
      <p className="moment-words">Fixed. Nothing has been decided.</p>
      <span className="moment-skip">Press any key to skip</span>
    </div>
  );
}
