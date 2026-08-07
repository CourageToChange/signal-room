import fs from 'node:fs/promises'
import path from 'node:path'
import { chromium } from '@playwright/test'

const baseURL = process.env.DEMO_BASE_URL ?? 'http://127.0.0.1:4173'
const output = path.resolve('../docs/screenshots')
await fs.mkdir(output, { recursive: true })

const browser = await chromium.launch(
  process.env.E2E_BROWSER_PATH
    ? { executablePath: process.env.E2E_BROWSER_PATH }
    : undefined,
)

try {
  for (const profile of [
    { name: 'desktop', viewport: { width: 1440, height: 1000 } },
    { name: 'mobile', viewport: { width: 412, height: 915 }, isMobile: true, hasTouch: true },
  ]) {
    const context = await browser.newContext({
      viewport: profile.viewport,
      isMobile: profile.isMobile ?? false,
      hasTouch: profile.hasTouch ?? false,
    })
    const page = await context.newPage()
    await page.goto(baseURL)
    await page.screenshot({ path: path.join(output, `pressure-drop-${profile.name}-brief.png`), fullPage: true })
    await page.getByRole('button', { name: /Start incident drill/i }).click()
    await page.getByRole('button', { name: /Skip to incident/i }).click()
    await page.screenshot({ path: path.join(output, `pressure-drop-${profile.name}-incident.png`), fullPage: true })
    await context.close()
  }
} finally {
  await browser.close()
}

process.stdout.write(`Captured demo screenshots in ${output}\n`)
