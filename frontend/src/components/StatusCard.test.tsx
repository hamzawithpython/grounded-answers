import { render, screen, waitFor } from "@testing-library/react";
import { describe, it, expect, beforeEach, vi } from "vitest";
import { StatusCard } from "./StatusCard";

describe("StatusCard", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("shows online status when API returns 200", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() =>
        Promise.resolve(
          new Response(JSON.stringify({ status: "ok" }), { status: 200 })
        )
      )
    );

    render(<StatusCard />);

    await waitFor(() => {
      expect(screen.getByText("Online")).toBeInTheDocument();
    });
  });

  it("shows offline status when fetch fails", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.reject(new Error("Network error")))
    );

    render(<StatusCard />);

    await waitFor(() => {
      expect(screen.getByText("Offline")).toBeInTheDocument();
      expect(screen.getByText("Network error")).toBeInTheDocument();
    });
  });

  it("shows offline status when API returns error", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() =>
        Promise.resolve(
          new Response(JSON.stringify({ error: "Internal Server Error" }), {
            status: 500,
          })
        )
      )
    );

    render(<StatusCard />);

    await waitFor(() => {
      expect(screen.getByText("Offline")).toBeInTheDocument();
      expect(screen.getByText("HTTP 500")).toBeInTheDocument();
    });
  });

  it("shows loading state initially", () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => new Promise(() => {}) as Promise<Response>)
    );

    render(<StatusCard />);

    expect(screen.getByText("Checking...")).toBeInTheDocument();
  });
});
