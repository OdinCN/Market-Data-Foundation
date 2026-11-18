import { useState } from "react";
import { uuidValid } from "./cpiTimeSeriesApi";

// Preserve the active intent across navigation/refresh until explicit terminal reopen.
export function useTdxPendingJob(asset: string) {
  const key = `mdf.${asset}.pending.v1`;
  function read() {
    const value = sessionStorage.getItem(key);
    if (value !== null && !uuidValid(value)) throw Error("invalid_pending_job");
    return value;
  }
  const [pending, setPending] = useState(() => {
    try {
      return { id: read(), blocked: false };
    } catch {
      return { id: null, blocked: true };
    }
  });
  function reserve(id: string) {
    try {
      const existing = read();
      if (existing) {
        setPending({ id: existing, blocked: false });
        return false;
      }
      sessionStorage.setItem(key, id);
      if (read() !== id) throw Error("reservation_failed");
      setPending({ id, blocked: false });
      return true;
    } catch {
      setPending({ id: null, blocked: true });
      return false;
    }
  }
  function release(id: string) {
    if (read() !== id) throw Error("pending_job_changed");
    sessionStorage.removeItem(key);
    setPending({ id: null, blocked: false });
  }
  return { ...pending, reserve, release };
}
