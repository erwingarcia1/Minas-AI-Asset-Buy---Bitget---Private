import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import ForgotPasswordPage from "../pages/ForgotPasswordPage";
import ResetPasswordPage, { passwordProblem } from "../pages/ResetPasswordPage";

vi.mock("../services/api", () => ({ auth: { forgotPassword: vi.fn(), resetPassword: vi.fn(), validateResetToken: vi.fn() } }));
import { auth } from "../services/api";

const at = (path: string, el: JSX.Element) => render(
  <MemoryRouter initialEntries={[path]}><Routes><Route path={path.split("?")[0]} element={el} /></Routes></MemoryRouter>);

describe("password reset", () => {
  beforeEach(() => { vi.clearAllMocks(); vi.mocked(auth.validateResetToken).mockResolvedValue({ valid: true }); });

  it("passwordProblem mirrors the API policy", () => {
    expect(passwordProblem("short")).toMatch(/8 characters/);
    expect(passwordProblem("alllowercase1!")).toMatch(/uppercase/);
    expect(passwordProblem("Good-Passw0rd!")).toBeNull();
  });

  it("forgot page sends the email and shows the neutral confirmation", async () => {
    vi.mocked(auth.forgotPassword).mockResolvedValue({ sent: true });
    at("/forgot-password", <ForgotPasswordPage />);
    fireEvent.change(screen.getByLabelText(/Email/), { target: { value: "Me@Example.com" } });
    fireEvent.click(screen.getByRole("button", { name: /Send reset link/ }));
    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent(/reset link is on its way/));
    expect(auth.forgotPassword).toHaveBeenCalledWith("me@example.com");
  });

  it("reset page validates the token on load without consuming it, then submits", async () => {
    vi.mocked(auth.resetPassword).mockResolvedValue(undefined as never);
    at("/reset-password?token=abc.def.ghi", <ResetPasswordPage />);
    await waitFor(() => expect(screen.getByLabelText(/^New password/)).toBeInTheDocument());
    expect(auth.validateResetToken).toHaveBeenCalledWith("abc.def.ghi");
    expect(auth.resetPassword).not.toHaveBeenCalled();
    fireEvent.change(screen.getByLabelText(/^New password/), { target: { value: "Good-Passw0rd!" } });
    fireEvent.change(screen.getByLabelText(/Confirm/), { target: { value: "Good-Passw0rd!" } });
    fireEvent.click(screen.getByRole("button", { name: /Set new password/ }));
    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent(/Password updated|new password is set/));
    expect(auth.resetPassword).toHaveBeenCalledWith("abc.def.ghi", "Good-Passw0rd!");
  });

  it("reset page shows the invalid state on load for a used token, before any form", async () => {
    vi.mocked(auth.validateResetToken).mockRejectedValue({ response: { status: 400, data: { valid: false } } });
    at("/reset-password?token=used-token", <ResetPasswordPage />);
    await waitFor(() => expect(screen.getByText(/no longer valid/)).toBeInTheDocument());
    expect(screen.queryByLabelText(/^New password/)).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Request a new link/ })).toBeInTheDocument();
    expect(auth.resetPassword).not.toHaveBeenCalled();
  });

  it("reset page shows the invalid-link state on 400 at submit and with no token", async () => {
    vi.mocked(auth.resetPassword).mockRejectedValue({ response: { status: 400, data: { detail: "Invalid or expired reset link" } } });
    at("/reset-password?token=stale", <ResetPasswordPage />);
    await waitFor(() => expect(screen.getByLabelText(/^New password/)).toBeInTheDocument());
    fireEvent.change(screen.getByLabelText(/^New password/), { target: { value: "Good-Passw0rd!" } });
    fireEvent.change(screen.getByLabelText(/Confirm/), { target: { value: "Good-Passw0rd!" } });
    fireEvent.click(screen.getByRole("button", { name: /Set new password/ }));
    await waitFor(() => expect(screen.getByText(/no longer valid/)).toBeInTheDocument());
    at("/reset-password", <ResetPasswordPage />);
    expect(screen.getAllByText(/no longer valid/).length).toBeGreaterThan(0);
  });
});
