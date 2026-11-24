// Compatibility build shim for the (incorrect) Render dashboard build command.
// The real app serves static/index.html from the repository root. This script
// only satisfies `npm run build` + the subsequent `cp -r dist/*` step.
const fs = require('fs');
fs.mkdirSync('dist', { recursive: true });
fs.writeFileSync(
  'dist/index.html',
  '<!doctype html><html><head><meta charset="utf-8"><title>RaTrade</title></head>' +
  '<body><p>The RaTrade UI is served from /static/index.html at the repository root.</p></body></html>\n'
);
console.log('frontend shim build: dist/index.html written');
