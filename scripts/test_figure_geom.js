/* Unit test for app/renderer/figure-geom.js.
 *
 * PyMuPDF figure rects use a top-left origin (y down). The renderer must use
 * the same origin; a bottom-left conversion puts the crop and the in-page
 * flash on the vertical mirror of the figure.
 *
 * Run: node scripts/test_figure_geom.js
 */
'use strict';
const assert = require('assert');
const path = require('path');

const geom = require(path.join(__dirname, '..', 'app', 'renderer', 'figure-geom.js'));

// A figure near the TOP of a 595x842 page: y0=60, y1=180.
const fig = { x0: 100, y0: 60, x1: 300, y1: 180, w: 595, h: 842 };
const scale = 2;

const box = geom.canvasBox(fig, scale);
assert.deepStrictEqual(box, { width: 400, height: 240, offsetX: -200, offsetY: -120 });

const overlay = geom.overlayBox(fig, scale);
assert.deepStrictEqual(overlay, { left: 200, top: 120, width: 400, height: 240 });

// Reject a bottom-left conversion: top must be y0*scale,
// not (pageHeight - y1)*scale = 1324.
assert.strictEqual(overlay.top, fig.y0 * scale);
assert.notStrictEqual(overlay.top, (fig.h - fig.y1) * scale);

// Missing / malformed coordinates must not produce NaN geometry.
const empty = geom.canvasBox({}, 1);
assert.deepStrictEqual(empty, { width: 1, height: 1, offsetX: -0, offsetY: -0 });
['left', 'top', 'width', 'height'].forEach((k) => {
  assert.ok(Number.isFinite(geom.overlayBox({ x0: 'x', y0: null }, 1)[k]), k);
});

// A figure on the BOTTOM half maps below the page middle.
const bottom = geom.overlayBox({ x0: 0, y0: 700, x1: 100, y1: 800 }, 1);
assert.strictEqual(bottom.top, 700);

console.log('test_figure_geom OK (6 assertions)');
