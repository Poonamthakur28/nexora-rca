import { countByStatus, formatLatency, statusStyle } from "./status";

describe("statusStyle", () => {
  it("maps each status to a label", () => {
    expect(statusStyle("success").label).toBe("Success");
    expect(statusStyle("failed").label).toBe("Failed");
    expect(statusStyle("warning").label).toBe("Warning");
    expect(statusStyle("skipped").label).toBe("Skipped");
  });

  it("falls back to skipped for unknown values", () => {
    expect(statusStyle("weird").label).toBe("Skipped");
  });
});

describe("formatLatency", () => {
  it("formats milliseconds and seconds", () => {
    expect(formatLatency(42)).toBe("42 ms");
    expect(formatLatency(2500)).toBe("2.5 s");
    expect(formatLatency(null)).toBe("—");
  });
});

describe("countByStatus", () => {
  it("counts statuses", () => {
    expect(countByStatus(["success", "success", "failed", "warning"])).toEqual({
      success: 2,
      failed: 1,
      warning: 1,
      skipped: 0,
    });
  });
});
