import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import ForgotPasswordPage from "../pages/ForgotPasswordPage";
import ResetPasswordPage, { passwordProblem } from "../pages/ResetPasswordPage";

vi.mock("../services/api", () => ({ auth: { forgotPassword: vi.fn(), resetPassword: vi.fn() } }));
import { auth } from "../services/api";

const at = (path: string, el: JSX.Element) => render(
  <MemoryRouter initialEntries={[path]}><Routes><Route path={path.split("?")[0]} element={el} /></Routes></MemoryRouter>);

describe("password reset", () => {
  beforeEach(() => { vi.clearAllMocks(); });

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

  it("reset page submits the token from the URL and confirms", async () => {
    vi.mocked(auth.resetPassword).mockResolvedValue(undefined as never);
    at("/reset-password?token=abc.def.ghi", <ResetPasswordPage />);
    fireEvent.change(screen.getByLabelText(/^New password/), { target: { value: "Good-Passw0rd!" } });
    fireEvent.change(screen.getByLabelText(/Confirm/), { target: { value: "Good-Passw0rd!" } });
    fireEvent.click(screen.getByRole("button", { name: /Set new password/ }));
    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent(/Password updated|new password is set/));
    expect(auth.resetPassword).toHaveBeenCalledWith("abc.def.ghi", "Good-Passw0rd!");
  });

  it("reset page shows the invalid-link state on 400 and with no token", async () => {
    vi.mocked(auth.resetPassword).mockRejectedValue({ response: { status: 400, data: { detail: "Invalid or expired reset link" } } });
    at("/reset-password?token=stale", <ResetPasswordPage />);
    fireEvent.change(screen.getByLabelText(/^New password/), { target: { value: "Good-Passw0rd!" } });
    fireEvent.change(screen.getByLabelText(/Confirm/), { target: { value: "Good-Passw0rd!" } });
    fireEvent.click(screen.getByRole("button", { name: /Set new password/ }));
    await waitFor(() => expect(screen.getByText(/no longer valid/)).toBeInTheDocument());
    at("/reset-password", <ResetPasswordPage />);
    expect(screen.getAllByText(/no longer valid/).length).toBeGreaterThan(0);
  });
});
