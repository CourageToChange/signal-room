import { spawnSync } from 'node:child_process'
import fs from 'node:fs'
import path from 'node:path'

// `npm audit --audit-level=high` has one setting: everything or nothing. When an
// advisory lands that provably cannot fire in this app, the only ways to get a
// green build are to mute the whole check or to move a dependency the release
// depends on. Neither is right. This gate keeps the same bar and adds a written,
// dated exception list, so an unreachable advisory is recorded once, with the
// reasoning and a reviewer, and expires on a deadline instead of going quiet
// forever. It adds no dependency: policing the supply chain should not enlarge it.

const failingSeverities = new Set(['high', 'critical'])
const requiredFields = ['advisory', 'package', 'reason', 'reviewedBy', 'expires']
const advisoryPattern = /GHSA-[a-z0-9]+-[a-z0-9]+-[a-z0-9]+/i
const datePattern = /^\d{4}-\d{2}-\d{2}$/

const frontend = path.resolve(import.meta.dirname, '..')
const exceptionsFile = path.join(frontend, 'audit-exceptions.json')
const today = new Date().toISOString().slice(0, 10)
const failures = []

function advisoryId(url) {
  const match = advisoryPattern.exec(url ?? '')
  if (!match) throw new Error(`advisory has no GHSA identifier: ${url}`)
  return match[0].toUpperCase()
}

function runAudit() {
  // A fixed command string rather than an argument array: `npm` is a shim script
  // on Windows, which cannot be spawned without a shell, and passing an array
  // alongside `shell` is deprecated. Nothing here is interpolated.
  const audit = spawnSync('npm audit --json', {
    cwd: frontend,
    encoding: 'utf8',
    maxBuffer: 64 * 1024 * 1024,
    shell: true,
  })
  // npm audit exits non-zero whenever it finds anything, so the exit code says
  // nothing useful here; a genuine failure shows up as an `error` in the report.
  if (audit.error) throw audit.error
  if (!audit.stdout) throw new Error(`npm audit produced no output: ${audit.stderr}`)
  const report = JSON.parse(audit.stdout)
  if (report.error) throw new Error(`npm audit failed: ${report.error.summary ?? report.error.code}`)
  return report.vulnerabilities ?? {}
}

// Only the object entries in `via` carry an advisory; a string entry means this
// package is merely affected by another package's advisory and is already
// reported on that package's own row. Collapsing on the GHSA id therefore keeps
// one exception covering every package a single advisory touches.
function findAdvisories(vulnerabilities) {
  const found = new Map()
  for (const vulnerability of Object.values(vulnerabilities)) {
    for (const via of vulnerability.via) {
      if (typeof via === 'string') continue
      if (!failingSeverities.has(via.severity)) continue
      const id = advisoryId(via.url)
      const entry = found.get(id) ?? { id, severity: via.severity, title: via.title, packages: [] }
      if (!entry.packages.includes(via.name)) entry.packages.push(via.name)
      found.set(id, entry)
    }
  }
  return found
}

function readExceptions() {
  if (!fs.existsSync(exceptionsFile)) return []
  const exceptions = JSON.parse(fs.readFileSync(exceptionsFile, 'utf8'))
  if (!Array.isArray(exceptions)) throw new Error('audit-exceptions.json must contain an array')
  for (const [index, exception] of exceptions.entries()) {
    const missing = requiredFields.filter((field) => !exception[field])
    if (missing.length) {
      failures.push(`exception #${index + 1} is missing: ${missing.join(', ')}`)
      continue
    }
    if (!advisoryPattern.test(exception.advisory)) {
      failures.push(`exception ${exception.advisory} is not a GHSA identifier`)
    }
    if (!datePattern.test(exception.expires)) {
      failures.push(`exception ${exception.advisory} needs an expiry as YYYY-MM-DD`)
      continue
    }
    // An exception expires whether or not it is still suppressing anything, so a
    // decision made once cannot outlive the reasoning behind it.
    if (exception.expires < today) {
      failures.push(
        `exception ${exception.advisory} expired on ${exception.expires} — re-review it, ` +
          'then either fix the advisory or record a fresh expiry',
      )
    }
  }
  return exceptions
}

const exceptions = readExceptions()
const excused = new Set(exceptions.map((exception) => String(exception.advisory).toUpperCase()))
const advisories = findAdvisories(runAudit())

for (const advisory of advisories.values()) {
  if (excused.has(advisory.id)) continue
  failures.push(
    `unreviewed ${advisory.severity} advisory ${advisory.id} in ` +
      `${advisory.packages.join(', ')}: ${advisory.title}`,
  )
}

for (const advisory of excused) {
  if (!advisories.has(advisory)) {
    process.stdout.write(`Note: exception ${advisory} no longer matches anything — remove it.\n`)
  }
}

if (failures.length) {
  process.stderr.write(`Audit gate failed:\n${failures.map((line) => `  - ${line}\n`).join('')}`)
  process.exitCode = 1
} else {
  const excusedCount = excused.size
  process.stdout.write(
    `Audit gate passed: no unreviewed high or critical advisories ` +
      `(${excusedCount} reviewed exception${excusedCount === 1 ? '' : 's'} on file).\n`,
  )
}
