import AxeBuilder from '@axe-core/playwright'
import { expect, test } from '@playwright/test'

test('static drill is accessible, responsive, and makes no API request', async ({ page }) => {
  const apiRequests: string[] = []
  const offOriginRequests: string[] = []
  page.on('request', (request) => {
    if (['fetch', 'xhr', 'websocket', 'eventsource'].includes(request.resourceType())) {
      apiRequests.push(request.url())
    }
    if (new URL(request.url()).origin !== 'http://127.0.0.1:4173') {
      offOriginRequests.push(request.url())
    }
  })
  await page.goto('/')
  await expect(page.getByRole('heading', { name: 'Pressure Drop' })).toBeVisible()
  const initialAccessibility = await new AxeBuilder({ page }).analyze()
  expect(initialAccessibility.violations.filter((item) => ['serious', 'critical'].includes(item.impact ?? ''))).toEqual([])

  await page.getByRole('button', { name: /Start incident drill/i }).click()
  await page.getByRole('button', { name: /Skip to incident/i }).click()
  await expect(page.getByText('0s ago')).toBeVisible()
  await expect(page.getByRole('heading', { name: /Make the call/i })).toBeVisible()
  await page.getByLabel('Orchid Guest').check()
  await page.getByLabel("Inspect the guest's memory trend and recent changes").check()
  await page.getByRole('button', { name: /Submit assessment/i }).click()
  await expect(page.getByText('Incident understood')).toBeVisible()
  await expect(page.getByText(/2\/2 decisions/)).toBeVisible()
  expect(apiRequests).toEqual([])
  expect(offOriginRequests).toEqual([])
  expect(await page.context().cookies()).toEqual([])
  expect(
    await page.evaluate(() => ({ local: localStorage.length, session: sessionStorage.length })),
  ).toEqual({ local: 0, session: 0 })
  expect(await page.locator('form[action], [formaction], [ping]').count()).toBe(0)
})

// Unit tests stub the graph library out (importing it hangs vitest), so they cannot prove it
// actually mounts. This is the only check that the dependency graph renders in a real build.
test('dependency graph renders, or falls back to the list on narrow viewports', async ({ page }) => {
  await page.goto('/')
  await page.getByRole('button', { name: /Start incident drill/i }).click()

  // The list and the graph are alternatives, not companions: the list is revealed only
  // below the 760px breakpoint, where the graph is hidden.
  const fallbackList = page.locator('.topology__list')
  await expect(fallbackList.locator('button').first()).toBeAttached()
  const assetCount = await fallbackList.locator('button').count()
  expect(assetCount).toBeGreaterThan(0)

  const viewportWidth = page.viewportSize()?.width ?? 0
  if (viewportWidth <= 760) {
    await expect(page.locator('.topology__viewport')).toBeHidden()
    await expect(fallbackList.locator('button').first()).toBeVisible()
    return
  }

  const nodes = page.locator('.react-flow__node')
  await expect(nodes.first()).toBeVisible()
  expect(await nodes.count()).toBe(assetCount)
  await expect(page.locator('.react-flow__edge').first()).toBeAttached()

  // Read-only console: the graph must never offer editing affordances.
  await expect(nodes.first()).not.toHaveClass(/draggable/)
  expect(await page.locator('.react-flow__handle.connectable').count()).toBe(0)

  const firstNodeButton = nodes.first().getByRole('button')
  await firstNodeButton.click()
  await expect(firstNodeButton).toHaveAttribute('aria-pressed', 'true')
})

test('reduced motion preference keeps the drill usable', async ({ page }) => {
  await page.emulateMedia({ reducedMotion: 'reduce' })
  await page.goto('/')
  await page.getByRole('button', { name: /Start incident drill/i }).click()
  await expect(page.getByText(/Simulation · no live systems/i)).toBeVisible()
})
