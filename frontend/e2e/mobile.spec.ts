import { expect, test } from "@playwright/test";

test("mobile shows a semantic offer summary instead of the grid", async ({ page }) => {
  await page.goto("/");
  await page.getByRole("button", { name: "Draft requirements" }).click();
  await page.getByRole("button", { name: "Confirm requirements" }).click();
  await page.getByRole("button", { name: "Run buyer agent" }).click();
  const summary = page.getByRole("list", { name: "Offer summary" });
  await expect(summary).toBeVisible();
  await expect(summary.getByText("Apex Wholesale (simulated)")).toBeVisible();
  await expect(page.locator(".offer-grid")).toBeHidden();
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
  expect(overflow).toBeLessThanOrEqual(1);
});
