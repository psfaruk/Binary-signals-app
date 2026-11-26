/**
 * routes.tsx — hand-rolled hash router.
 *
 * Trade-off note (directive: explain architectural choices): react-router
 * would add a dependency for six static pages. Hash routing works behind
 * the FastAPI static mount, inside the XTransformPort preview gateway,
 * and with zero server-side fallback config. ~70 tested lines instead.
 */

import { useEffect, useState } from "react";

export type RouteId =
  | "overview" | "terminal" | "history" | "analytics" | "models" | "settings";

export const ROUTES: RouteId[] = [
  "overview", "terminal", "history", "analytics", "models", "settings",
];

export function parseHash(hash: string): RouteId {
  const clean = (hash || "").replace(/^#\/?/, "").split("?")[0] as RouteId;
  return ROUTES.includes(clean) ? clean : "overview";
}

export function navigate(route: RouteId): void {
  location.hash = `#/${route}`;
}

export function useRoute(): [RouteId, (r: RouteId) => void] {
  const [route, setRoute] = useState<RouteId>(() => parseHash(location.hash));
  useEffect(() => {
    const onChange = () => setRoute(parseHash(location.hash));
    window.addEventListener("hashchange", onChange);
    return () => window.removeEventListener("hashchange", onChange);
  }, []);
  return [route, navigate];
}
