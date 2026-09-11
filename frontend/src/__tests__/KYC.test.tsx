import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import KYC from "@/pages/KYC";

// The SDK is replaced by a stub that exposes its props so tests can drive expiration and messages.
const sdkProps: { current: Record<string, unknown> | null } = { current: null };
vi.mock("@sumsub/websdk-react", () => ({
  default: (props: Record<string, unknown>) => { sdkProps.current = props; return <div data-testid="sumsub-sdk">sdk</div>; },
}));
vi.mock("@/api/endpoints", () => ({ kycAPI: { status: vi.fn(), initiate: vi.fn(), createApplicant: vi.fn() } }));
vi.mock("@/hooks/useAuth", () => ({ useAuth: () => ({ user: { id: "u1", email: "me@example.com", kyc_status: "pending", created_at: "" }, refreshProfile: vi.fn().mockResolvedValue(undefined) }) }));
import { kycAPI } from "@/api/endpoints";

const TOKEN = "_act-sbx-SECRET-TOKEN-VALUE-1234567890";
const pendingStatus = { kyc_status: "pending", applicant_id: "app-1", review_status: "init", reject_labels: [], rejection_reason: null, retryable: true, synced: false };

describe("KYC page with the Sumsub WebSDK", () => {
  beforeEach(() => { vi.clearAllMocks(); sdkProps.current = null; vi.mocked(kycAPI.status).mockResolvedValue(pendingStatus as never); });

  it("mounts the SDK with the token and never renders the token in the DOM", async () => {
    vi.mocked(kycAPI.initiate).mockResolvedValue({ access_token: TOKEN, applicant_id: "app-1" });
    const { container } = render(<MemoryRouter><KYC /></MemoryRouter>);
    fireEvent.click(await screen.findByRole("button", { name: "Start verification" }));
    await screen.findByTestId("sumsub-sdk");
    expect(sdkProps.current?.accessToken).toBe(TOKEN);
    expect(container.innerHTML).not.toContain(TOKEN);
    expect(container.innerHTML).not.toContain("WebSDK token");
    expect(container.innerHTML).not.toContain("Embed the Sumsub");
  });

  it("disables the start button while the initiate request is in flight (no double sessions)", async () => {
    let resolve!: (v: { access_token: string; applicant_id: string }) => void;
    vi.mocked(kycAPI.initiate).mockImplementation(() => new Promise((r) => { resolve = r; }));
    render(<MemoryRouter><KYC /></MemoryRouter>);
    const btn = await screen.findByRole("button", { name: "Start verification" });
    fireEvent.click(btn);
    fireEvent.click(btn);
    fireEvent.click(btn);
    await waitFor(() => expect(screen.getByRole("button", { name: "Starting…" })).toBeDisabled());
    expect(kycAPI.initiate).toHaveBeenCalledTimes(1);
    resolve({ access_token: TOKEN, applicant_id: "app-1" });
    await screen.findByTestId("sumsub-sdk");
  });

  it("expiration handler fetches a fresh token from /api/kyc/initiate", async () => {
    vi.mocked(kycAPI.initiate)
      .mockResolvedValueOnce({ access_token: TOKEN, applicant_id: "app-1" })
      .mockResolvedValueOnce({ access_token: "_act-sbx-NEW-TOKEN", applicant_id: "app-1" });
    render(<MemoryRouter><KYC /></MemoryRouter>);
    fireEvent.click(await screen.findByRole("button", { name: "Start verification" }));
    await screen.findByTestId("sumsub-sdk");
    const handler = sdkProps.current?.expirationHandler as () => Promise<string>;
    await expect(handler()).resolves.toBe("_act-sbx-NEW-TOKEN");
    expect(kycAPI.initiate).toHaveBeenCalledTimes(2);
  });

  it("applicant submitted -> re-syncs status and shows 'under review'; status changed -> verified badge", async () => {
    vi.mocked(kycAPI.initiate).mockResolvedValue({ access_token: TOKEN, applicant_id: "app-1" });
    render(<MemoryRouter><KYC /></MemoryRouter>);
    fireEvent.click(await screen.findByRole("button", { name: "Start verification" }));
    await screen.findByTestId("sumsub-sdk");
    const onMessage = sdkProps.current?.onMessage as (type: string, payload: unknown) => void;
    vi.mocked(kycAPI.status).mockResolvedValueOnce({ ...pendingStatus, review_status: "pending", synced: true } as never);
    onMessage("idCheck.onApplicantSubmitted", {});
    await screen.findByText(/Submitted, under review/);
    expect(kycAPI.status).toHaveBeenLastCalledWith(true);
    // a status-changed event that resolves to verified
    vi.mocked(kycAPI.status).mockResolvedValueOnce({ ...pendingStatus, kyc_status: "verified", review_status: "completed", synced: true } as never);
    fireEvent.click(screen.getByRole("button", { name: "Refresh status" }));
    await screen.findByText(/Verification status: Verified/);
  });

  it("rejected state shows the reason and a retry button; page load does not force a Sumsub sync", async () => {
    vi.mocked(kycAPI.status).mockResolvedValue({ ...pendingStatus, kyc_status: "rejected", rejection_reason: "Photo is blurry", retryable: true } as never);
    render(<MemoryRouter><KYC /></MemoryRouter>);
    await screen.findByText(/Verification status: Rejected/);
    expect(screen.getByText("Photo is blurry")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Retry verification" })).toBeInTheDocument();
    expect(kycAPI.status).toHaveBeenCalledWith(false);
  });
});
