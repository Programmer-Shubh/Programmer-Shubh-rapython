// Render's existing build command expects an npm build step. The restored
// site is server-rendered from static/index.html, so just publish it to dist/.
const fs = require('fs');
const path = require('path');

const src = path.resolve(__dirname, '..', '..', 'static');
const dst = path.resolve(__dirname, 'dist');

fs.rmSync(dst, { recursive: true, force: true });
fs.mkdirSync(dst, { recursive: true });
fs.cpSync(src, dst, { recursive: true });
console.log('Published old RaTrade UI shell to dist/');
