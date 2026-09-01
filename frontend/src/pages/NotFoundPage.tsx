import { Link } from "react-router-dom";

export default function NotFoundPage() {
  return (
    <main className="flex min-h-screen flex-col items-center justify-center gap-4">
      <h1 className="text-3xl font-semibold">Page not found</h1>
      <Link className="text-blue-600 underline" to="/dashboard">Back to dashboard</Link>
    </main>
  );
}
