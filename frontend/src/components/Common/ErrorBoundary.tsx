import { Component, type ErrorInfo, type ReactNode } from "react";

interface Props { children: ReactNode }
interface State { error: Error | null }

export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error("Unhandled UI error", error, info.componentStack);
  }

  render() {
    if (!this.state.error) return this.props.children;
    return (
      <div className="flex min-h-screen items-center justify-center p-6">
        <div className="card max-w-md text-center">
          <h1 className="text-lg font-semibold text-slate-900">Something went wrong</h1>
          <p className="mt-2 text-sm text-slate-600">{this.state.error.message}</p>
          <button className="btn-primary mt-6" onClick={() => window.location.assign("/dashboard")}>
            Reload app
          </button>
        </div>
      </div>
    );
  }
}
