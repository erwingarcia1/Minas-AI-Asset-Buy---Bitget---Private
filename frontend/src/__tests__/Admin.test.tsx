import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import Admin from "@/pages/Admin";

vi.mock("@/api/endpoints", () => ({
  adminAPI: { stats: vi.fn(), users: vi.fn(), suspend: vi.fn(), unsuspend: vi.fn(), removeUser: vi.fn(), transactions: vi.fn(), alerts: vi.fn() },
}));
const authUser = { current: { id: "a1", email: "admin@example.com", kyc_status: "verified", role: "admin", created_at: "" } as Record<string, unknown> };
vi.mock("@/hooks/useAuth", () => ({ useAuth: () => ({ user: authUser.current }) }));
import { adminAPI } from "@/api/endpoints";

const STATS = { total_users: 12, verified_users: 9, suspended_users: 1, total_invested_usdt: 1234.5, total_transactions: 40,
  failed_transactions_24h: 2, active_purchases: 7, signups_7d: 3, volume_7d_usdt: 250 };
const ROW = { id: "u1", email: "maria@example.com", role: "user", kyc_status: "verified", suspended: false, created_at: "2026-09-01T00:00:00Z", invested_usdt: 300 };

function mount() {
  return render(
    <MemoryRouter initialEntries={["/admin"]}>
      <Routes><Route path="/admin" element={<Admin />} /><Route path="/dashboard" element={<p>dashboard page</p>} /></Routes>
    </MemoryRouter>);
}

describe("Admin console", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    authUser.current = { id: "a1", email: "admin@example.com", kyc_status: "verified", role: "admin", created_at: "" };
    vi.mocked(adminAPI.stats).mockResolvedValue(STATS as never);
    vi.mocked(adminAPI.users).mockResolvedValue({ items: [ROW], total: 1 } as never);
    vi.mocked(adminAPI.suspend).mockResolvedValue(undefined as never);
    vi.mocked(adminAPI.removeUser).mockResolvedValue(undefined as never);
  });

  it("redirects non-admins away without calling any admin endpoint (the server enforces the role anyway)", async () => {
    authUser.current = { ...authUser.current, role: "user" };
    mount();
    await screen.findByText("dashboard page");
    expect(adminAPI.stats).not.toHaveBeenCalled();
  });

  it("shows platform stats and the user list", async () => {
    mount();
    await waitFor(() => expect(screen.getByTestId("admin-overview")).toHaveTextContent("12"));
    fireEvent.click(screen.getByRole("button", { name: "Users" }));
    await waitFor(() => expect(screen.getByTestId("admin-users")).toHaveTextContent("maria@example.com"));
  });

  it("suspending requires typing the user's email; cancel does nothing", async () => {
    mount();
    fireEvent.click(await screen.findByRole("button", { name: "Users" }));
    fireEvent.click(await screen.findByRole("button", { name: "Suspend" }));
    const dialog = await screen.findByRole("dialog");
    expect(dialog).toHaveTextContent("Suspend this user?");
    const confirmBtn = screen.getByRole("button", { name: "Suspend user" });
    expect(confirmBtn).toBeDisabled();
    fireEvent.change(screen.getByLabelText(/to confirm/), { target: { value: "wrong@example.com" } });
    expect(confirmBtn).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(adminAPI.suspend).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Suspend" }));
    fireEvent.change(await screen.findByLabelText(/to confirm/), { target: { value: "maria@example.com" } });
    fireEvent.click(screen.getByRole("button", { name: "Suspend user" }));
    await waitFor(() => expect(adminAPI.suspend).toHaveBeenCalledWith("u1"));
  });

  it("deleting sends the typed email as the confirmation and surfaces a server rejection", async () => {
    vi.mocked(adminAPI.removeUser).mockRejectedValueOnce({ isAxiosError: true, response: { status: 400, data: { detail: "confirm must equal the user's email" } } });
    mount();
    fireEvent.click(await screen.findByRole("button", { name: "Users" }));
    fireEvent.click(await screen.findByRole("button", { name: "Delete" }));
    fireEvent.change(await screen.findByLabelText(/to confirm/), { target: { value: "maria@example.com" } });
    fireEvent.click(screen.getByRole("button", { name: "Delete user" }));
    await waitFor(() => expect(adminAPI.removeUser).toHaveBeenCalledWith("u1", "maria@example.com"));
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent(/confirm must equal/));
    expect(screen.getByRole("dialog")).toBeInTheDocument();
  });
});
