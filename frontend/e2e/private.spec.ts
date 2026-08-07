import AxeBuilder from '@axe-core/playwright'
import { expect, test } from '@playwright/test'

test('real fixture console supports incident response and every flagship route', async ({ page }) => {
  const pageErrors: string[] = []
  page.on('pageerror', (error) => pageErrors.push(error.message))

  await page.goto('/')
  await expect(page.getByRole('heading', { name: 'Operations overview' })).toBeVisible()
  await expect(page.getByText('Read-only telemetry')).toBeVisible()
  await expect(page.getByText(/No shell · no service controls · no automated remediation/i)).toBeVisible()
  const initialAccessibility = await new AxeBuilder({ page }).analyze()
  expect(
    initialAccessibility.violations.filter((item) =>
      ['serious', 'critical'].includes(item.impact ?? ''),
    ),
  ).toEqual([])

  await page.getByRole('link', { name: 'Topology' }).click()
  await expect(page.getByRole('heading', { name: 'Topology' })).toBeVisible()
  await page.getByLabel(/Search 7 assets/i).fill('Atlas')
  await expect(page.getByRole('button', { name: /Atlas Node/i })).toBeVisible()

  await page.getByRole('link', { name: 'Incidents' }).click()
  await expect(page.getByRole('heading', { name: 'Incident inbox' })).toBeVisible()
  await page.getByRole('link', { name: /Atlas Node requires attention/i }).click()
  await expect(page.getByRole('heading', { name: 'Atlas Node requires attention' })).toBeVisible()
  const acknowledge = page.getByRole('button', { name: 'Acknowledge incident' })
  if (await acknowledge.isVisible()) {
    await acknowledge.click()
    await expect(page.getByText('Incident acknowledged.')).toBeVisible()
  }
  await page.getByLabel('Private responder note').fill('Browser fixture evidence checked')
  await page.getByRole('button', { name: 'Add note' }).click()
  await expect(page.getByText('Responder note added.')).toBeVisible()

  await page.getByRole('link', { name: 'Maintenance' }).click()
  await expect(page.getByRole('heading', { name: 'Maintenance' })).toBeVisible()
  await page.getByLabel('Atlas Node').check()
  await page.getByLabel('Reason').fill('Browser release test')
  await page.getByRole('button', { name: 'Create maintenance window' }).click()
  await expect(page.getByText(/Maintenance window created/i)).toBeVisible()

  await page.getByRole('link', { name: 'Diagnostics' }).click()
  await expect(page.getByRole('heading', { name: 'Diagnostics' })).toBeVisible()
  await expect(page.getByText(/Signal Room 1\.0\.0/)).toBeVisible()

  await page.setViewportSize({ width: 320, height: 760 })
  await expect(page.getByRole('navigation', { name: 'Primary navigation' })).toBeVisible()
  await page.setViewportSize({ width: 1440, height: 900 })
  await expect(page.getByRole('heading', { name: 'Diagnostics' })).toBeVisible()
  expect(pageErrors).toEqual([])
})

test('private API navigation exposes no unexpected accessibility violations', async ({ page }) => {
  for (const path of ['/', '/history', '/maintenance', '/diagnostics']) {
    await page.goto(path)
    await expect(page.locator('main')).toBeVisible()
    const result = await new AxeBuilder({ page }).analyze()
    expect(
      result.violations.filter((item) => ['serious', 'critical'].includes(item.impact ?? '')),
    ).toEqual([])
  }
})

test('operations overview keeps topology and queue usable at every supported width', async ({
  page,
}) => {
  const widths = [1440, 1180, 1024, 760, 390, 320]
  for (const width of widths) {
    await page.setViewportSize({ width, height: 900 })
    await page.goto('/')
    await expect(page.getByRole('heading', { name: 'Operations overview' })).toBeVisible()
    await expect(page.getByRole('navigation', { name: 'Primary navigation' })).toBeVisible()

    const topology = page.locator('.overview-layout > .topology')
    const queue = page.locator('.overview-layout > .queue-card')
    const topologyBox = await topology.boundingBox()
    const queueBox = await queue.boundingBox()
    expect(topologyBox).not.toBeNull()
    expect(queueBox).not.toBeNull()
    if (!topologyBox || !queueBox) continue

    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(width)
    expect(queueBox.width).toBeGreaterThanOrEqual(Math.min(280, width - 24))
    if (width > 1180) {
      expect(topologyBox.x).toBeLessThan(queueBox.x)
      expect(Math.abs(topologyBox.y - queueBox.y)).toBeLessThanOrEqual(2)
    } else {
      expect(queueBox.y).toBeGreaterThan(topologyBox.y + topologyBox.height)
      expect(Math.abs(topologyBox.x - queueBox.x)).toBeLessThanOrEqual(2)
      expect(Math.abs(topologyBox.width - queueBox.width)).toBeLessThanOrEqual(2)
    }

    const map = page.getByRole('group', { name: 'Infrastructure dependency map' })
    const list = topology.locator('.topology__list')
    if (width <= 760) {
      await expect(map).toBeHidden()
      await expect(list).toBeVisible()
    } else {
      await expect(map).toBeVisible()
      await expect(list).toBeHidden()
    }
  }
})

test('stale, offline, recovery, and mutation-error states stay explicit and accessible', async ({
  page,
}) => {
  const bootstrapResponse = await page.request.get(
    'http://127.0.0.1:8081/api/v1/bootstrap',
  )
  const bootstrapPayload = await bootstrapResponse.json() as Record<string, unknown>
  await page.route('**/api/v1/bootstrap', async (route) => {
    await route.fulfill({ json: { ...bootstrapPayload, stale: true } })
  })
  await page.goto('/')
  await expect(page.getByText(/Telemetry is stale/i)).toBeVisible()
  let result = await new AxeBuilder({ page }).analyze()
  expect(
    result.violations.filter((item) => ['serious', 'critical'].includes(item.impact ?? '')),
  ).toEqual([])

  await page.evaluate(() => window.dispatchEvent(new Event('offline')))
  await expect(page.getByText(/Network offline/i)).toBeVisible()
  result = await new AxeBuilder({ page }).analyze()
  expect(
    result.violations.filter((item) => ['serious', 'critical'].includes(item.impact ?? '')),
  ).toEqual([])
  await page.evaluate(() => window.dispatchEvent(new Event('online')))

  const operationKeys: string[] = []
  let rejectOnce = true
  await page.route('**/api/v1/incidents/*/notes', async (route) => {
    operationKeys.push(route.request().headers()['idempotency-key'] ?? '')
    if (rejectOnce) {
      rejectOnce = false
      await route.fulfill({
        status: 503,
        contentType: 'application/problem+json',
        json: {
          type: 'about:blank',
          title: 'Service Unavailable',
          status: 503,
          detail: 'Simulated response loss',
          instance: route.request().url(),
          request_id: 'e2e-response-loss',
        },
      })
      return
    }
    await route.continue()
  })
  await page.goto('/incidents')
  await page.getByRole('link', { name: /requires attention/i }).first().click()
  await page.getByLabel('Private responder note').fill('Retry-safe browser evidence')
  await page.getByRole('button', { name: 'Add note' }).click()
  await expect(page.getByText('Simulated response loss')).toBeVisible()
  result = await new AxeBuilder({ page }).analyze()
  expect(
    result.violations.filter((item) => ['serious', 'critical'].includes(item.impact ?? '')),
  ).toEqual([])
  await page.getByRole('button', { name: 'Add note' }).click()
  await expect(page.getByText('Responder note added.')).toBeVisible()
  expect(operationKeys).toHaveLength(2)
  expect(operationKeys[1]).toBe(operationKeys[0])
})

test('topology remains searchable and keyboard-usable with 50 assets', async ({ page }) => {
  const bootstrapResponse = await page.request.get(
    'http://127.0.0.1:8081/api/v1/bootstrap',
  )
  const payload = await bootstrapResponse.json() as {
    assets: Array<Record<string, unknown>>
    states: Array<Record<string, unknown>>
    incidents: Array<Record<string, unknown>>
  }
  const assetTemplate = payload.assets[0]
  const stateTemplate = payload.states[0]
  const assets = Array.from({ length: 50 }, (_, index) => ({
    ...assetTemplate,
    id: `asset-${index}`,
    label: `Asset ${index}`,
    depends_on: index < 10 ? [] : [`asset-${index % 10}`],
    parent_id: index < 10 ? null : `asset-${index % 10}`,
    sort_order: index,
  }))
  const states = assets.slice(0, 49).map((asset, index) => ({
    ...stateTemplate,
    asset_id: asset.id,
    health: index % 11 === 0 ? 'degraded' : 'healthy',
    message: index % 11 === 0 ? 'Capacity warning' : 'Healthy',
  }))
  await page.route('**/api/v1/bootstrap', async (route) => {
    await route.fulfill({ json: { ...payload, assets, states, incidents: [] } })
  })

  await page.goto('/topology')
  await expect(page.getByLabel('Search 50 assets')).toBeVisible()
  // Assert the graph by what it owes the user - one reachable control per asset on
  // whichever surface is actually showing - rather than by the element that draws
  // it, so a change of rendering library cannot pass or fail this on its own.
  // Below 760px the canvas is hidden and the list is the interface, exactly as the
  // width sweep above requires.
  const narrow = (page.viewportSize()?.width ?? 0) <= 760
  const surface = narrow
    ? page.locator('.topology__list')
    : page.getByRole('group', { name: 'Infrastructure dependency map' })
  await expect(surface).toBeVisible()
  await expect(surface.getByRole('button')).toHaveCount(assets.length)
  await page.getByLabel('Search 50 assets').fill('Asset 49')
  const matches = page.getByRole('button', { name: /Asset 49/i })
  await expect(matches).toHaveCount(1)
  await matches.first().focus()
  await expect(matches.first()).toBeFocused()
  await expect(page.getByLabel('Status: Unknown')).toHaveCount(2)
  const result = await new AxeBuilder({ page }).analyze()
  expect(
    result.violations.filter((item) => ['serious', 'critical'].includes(item.impact ?? '')),
  ).toEqual([])
})
