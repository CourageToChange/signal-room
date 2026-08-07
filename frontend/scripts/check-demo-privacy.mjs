import fs from 'node:fs'
import path from 'node:path'

const root = path.resolve('dist-demo')
const forbidden = [
  /192\.168\./i,
  /10\.\d{1,3}\.\d{1,3}\./i,
  /172\.(?:1[6-9]|2\d|3[01])\./i,
  /[A-Za-z0-9._%+-]+@(?!example\.invalid)[A-Za-z0-9.-]+\.[A-Za-z]{2,}/i,
  /PVEAPIToken=/i,
  /Cf-Access-Jwt-Assertion/i,
  /noorfamily\.uk/i,
  /\/api\/v1\//i,
  /EventSource\s*\(/i,
]

function files(directory) {
  return fs.readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
    const item = path.join(directory, entry.name)
    return entry.isDirectory() ? files(item) : [item]
  })
}

if (!fs.existsSync(root)) throw new Error('dist-demo does not exist')
for (const file of files(root)) {
  if (/\.(?:png|jpg|jpeg|gif|woff2?)$/i.test(file)) continue
  const content = fs.readFileSync(file, 'utf8')
  for (const pattern of forbidden) {
    if (pattern.test(content)) throw new Error(`forbidden pattern ${pattern} found in ${file}`)
  }
}
const headers = fs.readFileSync(path.join(root, '_headers'), 'utf8')
if (!headers.includes("connect-src 'none'")) throw new Error("demo CSP must set connect-src 'none'")
process.stdout.write('Demo privacy and network-isolation scan passed.\n')
