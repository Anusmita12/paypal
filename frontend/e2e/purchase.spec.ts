import { expect, test } from "@playwright/test";

// The README main story, offline: stale quote rejected, re-run negotiates B to $488, keyboard approval,
// simulated buyer approval, server capture, and incoming stock raised exactly once.
test("main story: price change, re-run, approve and pay by keyboard", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByText("8 shortages")).toBeVisible();
  await expect(page.getByText("AI: fixture script")).toBeVisible();
  await expect(page.getByText("PayPal: offline fixture")).toBeVisible();

  await page.getByRole("button", { name: "Draft requirements" }).click();
  await expect(page.getByRole("heading", { name: "Confirm requirements" })).toBeVisible();
  await expect(page.getByLabel("Budget (USD, delivered)")).toHaveValue("500.00");
  await page.getByRole("button", { name: "Confirm requirements" }).click();
  await expect(page.getByRole("heading", { name: "Confirmed requirements" })).toBeVisible();

  await page.getByRole("button", { name: "Run buyer agent" }).click();
  const approveA = page.getByRole("button", { name: "Approve this purchase — $470.00" });
  await expect(approveA).toBeVisible();
  await expect(page.getByText("Rejected Cobalt Direct (simulated): CBL-C2L-1M: certifications missing mfi")).toBeVisible();

  // Labeled scenario: Apex raises prices before approval. The old snapshot must be refused server-side.
  await page.getByRole("button", { name: "Trigger" }).first().click();
  await expect(page.getByText("Scenario applied: Supplier A raises prices.")).toBeVisible();
  await approveA.click();
  await expect(page.getByRole("alert")).toContainText("proposal invalidated");
  await expect(page.getByRole("alert")).toContainText("supplier terms changed");

  await page.getByRole("button", { name: "Run buyer again" }).click();
  const approveB = page.getByRole("button", { name: "Approve this purchase — $488.00" });
  await expect(approveB).toBeVisible();
  await expect(page.getByText("Saves $22.00 against the best current list-price basket")).toBeVisible();

  // Keyboard path for every money-related action.
  await approveB.focus();
  await page.keyboard.press("Enter");
  await expect(page.getByRole("heading", { name: "Restock from Brightline Distribution (simulated)" })).toBeVisible();
  const create = page.getByRole("button", { name: "Create checkout order" });
  await create.focus();
  await page.keyboard.press("Enter");
  await expect(page.getByText("Offline PayPal stand-in")).toBeVisible();
  const simulate = page.getByRole("button", { name: "Simulate buyer approval and capture" });
  await simulate.focus();
  await page.keyboard.press("Enter");
  await expect(page.getByText("Paid. 96 units added to")).toBeVisible();
  await expect(page.getByText(/^Capture FXCAP\w+ completed for 488\.00 USD\.$/)).toBeVisible();

  // Incoming rises once by the paid quantity; on-hand does not change.
  await page.getByRole("button", { name: "Inventory" }).click();
  const row = page.locator('.ag-center-cols-container [row-id="CHG-65W-PD"]');
  await expect(row.locator('[col-id="incoming"]')).toHaveText("10");
  await expect(row.locator('[col-id="on_hand"]')).toHaveText("6");
});

test("pay-per-call: metered run, PayPal top-up, provider payouts", async ({ page }) => {
  await page.goto("/");
  await page.getByRole("button", { name: "Draft requirements" }).click();
  await page.getByRole("button", { name: "Confirm requirements" }).click();
  await page.getByRole("button", { name: "Run buyer agent" }).click();
  await expect(page.getByRole("button", { name: /Approve this purchase/ })).toBeVisible();
  const ticker = page.getByLabel("Tool charges this run");
  await expect(ticker.locator("li")).toHaveCount(8); // 3 quotes + 4 rounds at 1¢, one 2¢ market check
  await expect(ticker).toContainText("$0.09");

  await page.getByRole("button", { name: "Tool wallet" }).click();
  await expect(page.getByLabel("Balance")).toHaveText("$0.41"); // 50¢ synthetic grant − 9¢
  await expect(page.locator(".ag-center-cols-container .ag-row")).toHaveCount(8);

  // Top up $5 through the same checkout path (fixture PayPal stand-in).
  await page.getByRole("button", { name: "$5.00" }).click();
  await expect(page.getByRole("heading", { name: "Tool wallet top-up" })).toBeVisible();
  await page.getByRole("button", { name: "Create checkout order" }).click();
  await page.getByRole("button", { name: "Simulate buyer approval and capture" }).click();
  await expect(page.getByText("$5.00 added to the tool wallet.")).toBeVisible();
  await page.getByRole("button", { name: "Open wallet" }).click();
  await expect(page.getByLabel("Balance")).toHaveText("$5.41");

  // Settle providers once; the button then has nothing left to pay.
  await page.getByRole("button", { name: "Settle 9¢ owed" }).click();
  const payouts = page.getByRole("region", { name: "Provider payouts" });
  await expect(payouts.locator("tbody tr")).toHaveCount(4);
  await expect(page.getByRole("button", { name: "Settle 0¢ owed" })).toBeDisabled();
  await payouts.getByRole("button", { name: "Check status" }).first().click();
  await expect(payouts.getByText("success")).toHaveCount(1);
});
