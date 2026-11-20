import { useCallback, useEffect, useState } from "react";
import { errorText } from "../../api/product/client";

/** Ignore responses from an unmounted account/page; keep failures visible. */
export function useProductData<T>(load: () => Promise<T>, initial: T) {
  const [data, setData] = useState(initial);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [generation] = useState(() => ({ sequence: 0 }));
  const reload = useCallback(async () => {
    const current = ++generation.sequence;
    setBusy(true);
    try {
      const value = await load();
      if (current === generation.sequence) {
        setData(value);
        setError("");
      }
    } catch (err) {
      if (current === generation.sequence) setError(errorText(err));
    } finally {
      if (current === generation.sequence) setBusy(false);
    }
  }, [load, generation]);
  useEffect(() => {
    void reload();
    return () => {
      generation.sequence++;
    };
  }, [reload, generation]);
  return { data, error, busy, reload, setError, setData };
}
