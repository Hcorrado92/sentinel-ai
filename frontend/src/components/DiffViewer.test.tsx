import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { DiffViewer, classifyDiffLine } from "./DiffViewer";

const sampleDiff = [
  "diff --git a/shop/discount.py b/shop/discount.py",
  "--- a/shop/discount.py",
  "+++ b/shop/discount.py",
  "@@ -1,3 +1,3 @@",
  " def apply_discount(total, percent):",
  "-    return total / percent",
  "+    return total * (1 - percent / 100)",
  "",
].join("\n");

describe("DiffViewer", () => {
  it("renders added lines with the added style and removed lines with the removed style", () => {
    render(<DiffViewer diff={sampleDiff} />);

    const lines = screen.getAllByTestId("diff-line");
    const added = lines.find((line) => line.textContent === "+    return total * (1 - percent / 100)");
    const removed = lines.find((line) => line.textContent === "-    return total / percent");

    expect(added).toHaveAttribute("data-line-type", "added");
    expect(added).toHaveClass("bg-emerald-50");
    expect(removed).toHaveAttribute("data-line-type", "removed");
    expect(removed).toHaveClass("bg-red-50");

    const context = lines.find((line) => line.textContent?.startsWith(" def apply_discount"));
    expect(context).toHaveAttribute("data-line-type", "context");
    expect(context).not.toHaveClass("bg-emerald-50");
    expect(context).not.toHaveClass("bg-red-50");
  });

  it("does not treat file headers as changes", () => {
    expect(classifyDiffLine("--- a/shop/discount.py")).toBe("header");
    expect(classifyDiffLine("+++ b/shop/discount.py")).toBe("header");
    expect(classifyDiffLine("@@ -1,3 +1,3 @@")).toBe("hunk");
    expect(classifyDiffLine("+added")).toBe("added");
    expect(classifyDiffLine("-removed")).toBe("removed");
  });

  it("shows a message for an empty diff", () => {
    render(<DiffViewer diff="" />);
    expect(screen.getByText("The diff is empty.")).toBeInTheDocument();
  });
});
