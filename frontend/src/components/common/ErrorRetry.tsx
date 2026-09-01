export default function ErrorRetry({ message, onRetry }: { message: string; onRetry: () => void }) {
  return (
    <div className="m-6 rounded border border-red-200 bg-red-50 p-4 text-sm" role="alert">
      <p className="text-red-700">{message}</p>
      <button onClick={onRetry} className="mt-2 rounded bg-red-600 px-3 py-1 text-white">
        Retry
      </button>
    </div>
  );
}
