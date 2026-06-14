import { QueryClient } from "@tanstack/react-query";

// The single app-wide query client. Lives in its own module so non-component
// code (e.g. the batch store) can invalidate caches without importing main.tsx
// (which would create an import cycle through the entry point).
export const queryClient = new QueryClient({
  defaultOptions: { queries: { staleTime: 30_000, retry: 1, refetchOnWindowFocus: false } },
});
