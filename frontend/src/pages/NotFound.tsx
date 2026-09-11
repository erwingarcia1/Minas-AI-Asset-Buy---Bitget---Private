import { Link } from "react-router-dom";

export default function NotFound() {
  return (
    <div className="flex min-h-[60vh] flex-col items-center justify-center text-center">
      <p className="text-6xl font-semibold text-slate-300">404</p>
      <p className="mt-2 text-slate-600">That page doesn't exist.</p>
      <Link to="/dashboard" className="btn-primary mt-6">Back to dashboard</Link>
    </div>
  );
}
