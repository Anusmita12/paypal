import { expect, test } from "@playwright/test";

// Seller side, offline: pay → ship (tracking pushed) → buyer dispute → cited draft → owner approval → submitted.
test("dispute defense: cited draft is approved and sent", async ({ page }) => {
  await page.goto("/");
  await page.getByRole("button", { name: "Draft requirements" }).click();
  await page.getByRole("button", { name: "Confirm requirements" }).click();
  await page.getByRole("button", { name: "Run buyer agent" }).click();
  await page.getByRole("button", { name: /Approve this purchase/ }).click();
  await page.getByRole("button", { name: "Create checkout order" }).click();
  await page.getByRole("button", { name: "Simulate buyer approval and capture" }).click();
  await expect(page.getByText("Paid. 96 units added to")).toBeVisible();

  const fulfil = page.getByRole("region", { name: "Fulfilment" });
  await fulfil.getByRole("button", { name: "Ship order" }).click();
  await expect(fulfil.getByText("pushed")).toBeVisible();
  await fulfil.getByRole("button", { name: "Mark delivered" }).click();
  await fulfil.getByRole("button", { name: "Simulate buyer dispute" }).click();

  await expect(page.getByRole("heading", { name: /^Dispute PP-D-FX/ })).toBeVisible();
  await page.getByRole("button", { name: "Draft evidence with AI" }).click();
  await expect(page.getByText("recommend: contest")).toBeVisible();
  await expect(page.getByText("Proof of fulfillment")).toBeVisible();
  await page.getByRole("button", { name: "shipment" }).first().click(); // citation chip highlights its record
  await page.getByRole("button", { name: "Approve and send this evidence to PayPal" }).click();
  await expect(page.getByText(/^Evidence submitted/)).toBeVisible();
  await expect(page.getByText("PayPal status: under review")).toBeVisible();
});
