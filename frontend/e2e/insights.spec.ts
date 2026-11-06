import { expect, test } from "@playwright/test";

test("AG Studio dashboard renders this session's data", async ({ page }) => {
  await page.goto("/");
  await page.getByRole("button", { name: "Draft requirements" }).click();
  await page.getByRole("button", { name: "Confirm requirements" }).click();
  await page.getByRole("button", { name: "Run buyer agent" }).click();
  await expect(page.getByRole("button", { name: /Approve this purchase/ })).toBeVisible();
  await page.getByRole("button", { name: "Insights" }).click();
  await expect(page.getByText("Best delivered quote by supplier (USD)")).toBeVisible({ timeout: 20_000 });
  await expect(page.getByText("Agent tool spend (¢)")).toBeVisible();
  await expect(page.getByText("Apex Wholesale").first()).toBeVisible();
});
