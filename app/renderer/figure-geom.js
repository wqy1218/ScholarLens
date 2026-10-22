/* 论文插图坐标：PyMuPDF 与 pdf.js 均为左上原点，y 向下增长。 */
(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.SLFigureGeom = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';

  function num(value) {
    const n = Number(value);
    return Number.isFinite(n) ? n : 0;
  }

  function rectOf(fig) {
    const x0 = num(fig && fig.x0);
    const y0 = num(fig && fig.y0);
    const x1 = num(fig && fig.x1);
    const y1 = num(fig && fig.y1);
    return { x0: x0, y0: y0, x1: x1, y1: y1,
             width: Math.max(1, x1 - x0), height: Math.max(1, y1 - y0) };
  }

  function canvasBox(fig, scale) {
    const r = rectOf(fig);
    const s = Number(scale) > 0 ? Number(scale) : 1;
    return {
      width: Math.max(1, Math.round(r.width * s)),
      height: Math.max(1, Math.round(r.height * s)),
      offsetX: -r.x0 * s,
      offsetY: -r.y0 * s,
    };
  }

  function overlayBox(fig, scale) {
    const r = rectOf(fig);
    const s = Number(scale) > 0 ? Number(scale) : 1;
    return {
      left: r.x0 * s,
      top: r.y0 * s,
      width: Math.max(8, r.width * s),
      height: Math.max(8, r.height * s),
    };
  }

  return { rectOf: rectOf, canvasBox: canvasBox, overlayBox: overlayBox };
});
