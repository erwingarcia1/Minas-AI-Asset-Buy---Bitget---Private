import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import SecretInput from "@/components/Common/SecretInput";

function Harness() {
  return (
    <div>
      <label htmlFor="exchange_credential_private">API secret</label>
      <SecretInput id="exchange_credential_private" name="exchange_credential_private" label="API secret" value="s3cr3t" onChange={vi.fn()} />
    </div>
  );
}

describe("SecretInput", () => {
  it("is a masked text input, never a password field, with the anti-autofill attributes", () => {
    render(<Harness />);
    const input = screen.getByLabelText("API secret");
    expect(input).toHaveAttribute("type", "text");
    expect(input).toHaveClass("secret-masked");
    expect(input).toHaveAttribute("autocomplete", "one-time-code");
    expect(input).toHaveAttribute("autocorrect", "off");
    expect(input).toHaveAttribute("autocapitalize", "off");
    expect(input).toHaveAttribute("spellcheck", "false");
    expect(input).toHaveAttribute("data-1p-ignore");
    expect(input).toHaveAttribute("data-lpignore", "true");
    expect(input).toHaveAttribute("data-form-type", "other");
    expect(input).toHaveAccessibleDescription("API secret is hidden while you type.");
  });

  it("shows and hides with a labelled, keyboard-reachable button", () => {
    render(<Harness />);
    const toggle = screen.getByRole("button", { name: "Show API secret" });
    expect(toggle).toHaveAttribute("type", "button");
    expect(toggle).toHaveAttribute("aria-pressed", "false");
    fireEvent.click(toggle);
    expect(screen.getByLabelText("API secret")).not.toHaveClass("secret-masked");
    expect(screen.getByRole("button", { name: "Hide API secret" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByLabelText("API secret")).toHaveAccessibleDescription("API secret is shown as typed.");
    toggle.focus();
    fireEvent.keyDown(toggle, { key: "Enter" });
    fireEvent.click(toggle);   // a native button activates on Enter/Space; jsdom needs the click
    expect(screen.getByRole("button", { name: "Show API secret" })).toBeInTheDocument();
  });
});
