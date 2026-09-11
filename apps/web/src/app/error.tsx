"use client"; // Error boundaries must be Client Components

import { useEffect } from "react";

export default function RootError({
  error,
  retry,
}: {
  error: Error & { digest?: string };
  retry: () => void;
}) {
  useEffect(() => {
    console.error(error);
  }, [error]);

  return (
    <div>
      <p className="error">
        Something went wrong rendering this page{error.message ? `: ${error.message}` : "."}
      </p>
      <button type="button" className="btn" onClick={() => retry()}>
        Try again
      </button>
    </div>
  );
}
