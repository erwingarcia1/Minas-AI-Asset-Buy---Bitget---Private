import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import Signup from "@/pages/Signup";

const signup = vi.fn();
vi.mock("@/hooks/useAuth", () => ({ useAuth: () => ({ signup }) }));

describe("Signup (private beta)", () => {
  beforeEach(() => { signup.mockReset(); signup.mockResolvedValue(undefined); });

  it("requires an invite code and sends it with the registration", async () => {
    render(<MemoryRouter><Signup /></MemoryRouter>);
    fireEvent.change(screen.getByLabelText("Email"), { target: { value: "new@example.com" } });
    fireEvent.change(screen.getByLabelText("Password"), { target: { value: "Str0ng-Passw0rd!!" } });
    fireEvent.change(screen.getByLabelText("Confirm password"), { target: { value: "Str0ng-Passw0rd!!" } });
    fireEvent.click(screen.getByRole("button", { name: "Create account" }));
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("Enter your invite code"));
    expect(signup).not.toHaveBeenCalled();
    fireEvent.change(screen.getByLabelText("Invite code"), { target: { value: " beta-2026-XYZ " } });
    fireEvent.click(screen.getByRole("button", { name: "Create account" }));
    await waitFor(() => expect(signup).toHaveBeenCalledWith("new@example.com", "Str0ng-Passw0rd!!", "beta-2026-XYZ"));
    expect(screen.getByText(/private beta\. No code\? Email support/)).toBeInTheDocument();
  });

  it("shows the server's vague refusal as-is", async () => {
    signup.mockRejectedValue(Object.assign(new Error("Signup is by invitation. Contact support@minastech.ca."), { isAxiosError: false }));
    render(<MemoryRouter><Signup /></MemoryRouter>);
    fireEvent.change(screen.getByLabelText("Email"), { target: { value: "new@example.com" } });
    fireEvent.change(screen.getByLabelText("Password"), { target: { value: "Str0ng-Passw0rd!!" } });
    fireEvent.change(screen.getByLabelText("Confirm password"), { target: { value: "Str0ng-Passw0rd!!" } });
    fireEvent.change(screen.getByLabelText("Invite code"), { target: { value: "wrong" } });
    fireEvent.click(screen.getByRole("button", { name: "Create account" }));
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("Signup is by invitation. Contact support@minastech.ca."));
  });
});
