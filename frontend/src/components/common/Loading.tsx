export default function Loading({ label = "Loading…" }: { label?: string }) {
  return (
    <div className="flex items-center justify-center gap-2 p-8 text-gray-500" role="status">
      <span className="h-4 w-4 animate-spin rounded-full border-2 border-gray-300 border-t-blue-600" />
      {label}
    </div>
  );
}
