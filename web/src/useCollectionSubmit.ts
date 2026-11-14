import { useRef, useState } from "react";

type Attempt = {
  id: string;
  state: "submitting" | "unknown" | "accepted";
  receipt?: unknown;
  scope: string;
};

/** One confirmed intent, one POST. Recovery only reads its original identity.
 * Domain adapters retain their commands, durable outboxes and receipt validators.
 * Navigation belongs exclusively to an explicitly clicked execution brief.
 */
export function useCollectionSubmit(scope = "") {
  const attempts = useRef(new Map<string, Promise<unknown>>());
  const [attempt, setAttempt] = useState<Attempt | null>(null);
  async function recall<T>(
    id: string,
    read: () => Promise<T>,
    matches: (receipt: T) => boolean,
  ) {
    const receipt = await read();
    if (!matches(receipt)) throw Error("collection_receipt_identity_mismatch");
    setAttempt({ id, scope, state: "accepted", receipt });
    return receipt;
  }
  function submit<T>(
    id: string,
    post: () => Promise<T>,
    read: () => Promise<T>,
    matches: (receipt: T) => boolean,
    rejected: (error: unknown) => boolean = () => false,
  ): Promise<T> {
    const existing = attempts.current.get(scope + ":" + id);
    if (existing) return existing as Promise<T>;
    setAttempt({ id, scope, state: "submitting" });
    const result = (async () => {
      try {
        const receipt = await post();
        if (!matches(receipt))
          throw Error("collection_receipt_identity_mismatch");
        setAttempt({ id, scope, state: "accepted", receipt });
        return receipt;
      } catch (error) {
        if (!rejected(error)) {
          try {
            return await recall(id, read, matches);
          } catch {
            // Missing, delayed or malformed GET cannot authorize a second POST.
          }
        }
        setAttempt({ id, scope, state: "unknown" });
        throw error;
      }
    })();
    attempts.current.set(scope + ":" + id, result);
    return result;
  }
  return { attempt: attempt?.scope === scope ? attempt : null, submit, recall };
}
