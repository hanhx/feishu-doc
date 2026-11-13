// Runs in the page. Use the rendered grid geometry to select cells, then let
// Feishu's normal copy action supply their values.
export function visibleBitableRange(canvas) {
  const box = canvas.getBoundingClientRect();
  const overlay = canvas.parentElement.querySelector('.faster-dom-over-layer');
  const body = overlay && [...overlay.children].map(element => element.getBoundingClientRect())
    .find(rect => Math.abs(rect.left - box.left) < 2 && rect.width > 0);
  if (!body || !box.width || !box.height) return null;

  const scaleX = canvas.width / box.width;
  const scaleY = canvas.height / box.height;
  const frozenRight = Math.round((body.right - box.left) * scaleX);
  const top = Math.round((body.top - box.top) * scaleY);
  const width = Math.min(canvas.width, Math.floor((innerWidth - box.left) * scaleX));
  const height = Math.min(canvas.height, Math.floor((innerHeight - box.top) * scaleY));
  // The left overlay covers the row header and any frozen columns. The
  // divider around that overlay can be darker than regular gridlines.
  const rowHeader = Math.min(frozenRight, Math.round(58 * scaleX));
  if (box.left < 0 || box.top < 0 || rowHeader < 8 || top < 8 ||
      width - rowHeader < 8 || height - top < 8) return null;

  const sample = document.createElement('canvas');
  sample.width = width;
  sample.height = height;
  const ctx = sample.getContext('2d', { willReadFrequently: true });
  ctx.drawImage(canvas, 0, 0);
  const { data } = ctx.getImageData(0, 0, width, height);
  const pixel = (x, y) => {
    const index = (y * width + x) * 4;
    return data[index + 3] ? (data[index] << 16) | (data[index + 1] << 8) | data[index + 2] : -1;
  };
  const lineY = top - 1;
  const lineColor = pixel(rowHeader + 4, lineY);
  const background = pixel(rowHeader + 4, Math.floor(top / 2));
  if (lineColor < 0 || lineColor === background || pixel(rowHeader + 7, lineY) !== lineColor) return null;
  const isRowDivider = (color) => color >= 0 && [0, 8, 16].every((shift) => {
    const delta = ((lineColor >> shift) & 255) - ((color >> shift) & 255);
    return delta >= 0 && delta <= 16;
  });

  const columns = [rowHeader];
  for (let x = rowHeader + 5; x < width; x += 1) {
    const frozenDivider = x === frozenRight - 1;
    if (x - columns.at(-1) >= 5 && (frozenDivider || [0.15, 0.35, 0.65, 0.85].every(ratio =>
      pixel(x, Math.floor(top * ratio)) === lineColor))) columns.push(x);
  }
  if (width - columns.at(-1) >= 5) columns.push(width);
  const dataLeft = columns[0];
  const firstCenter = Math.floor((dataLeft + (columns[1] ?? width)) / 2);
  const firstWidth = (columns[1] ?? width) - dataLeft;

  // A full horizontal line inside the first column separates records. A
  // hovered record can darken its border without changing the row position.
  const rows = [top];
  for (let y = top + 5; y < height; y += 1) {
    if (y - rows.at(-1) >= 5 && [0.16, 0.33, 0.55, 0.75, 0.9].every(ratio =>
      isRowDivider(pixel(dataLeft + Math.floor(firstWidth * ratio), y)))) rows.push(y);
  }
  if (rows.length < 2 || columns.length < 2) return null;

  // The add-row and add-column strips have a lone plus in their header.
  // Confirm its shape, and avoid treating a normal header label as the icon.
  function hasLonePlus(x0, y0, x1, y1, limitX) {
    const step = Math.max(3, Math.round(3 * Math.min(scaleX, scaleY)));
    const marked = (x, y) => {
      const color = pixel(x, y);
      return color >= 0 && color !== background && color !== lineColor;
    };
    let minX = x1;
    let minY = y1;
    let maxX = x0;
    let maxY = y0;
    let cross = false;
    for (let y = y0; y < y1; y += 1) {
      for (let x = x0; x < x1; x += 1) {
        if (!marked(x, y)) continue;
        // The plus is painted in the leading area; clipped boundary pixels and
        // decorations farther to the right are irrelevant to its shape.
        if (x >= limitX) continue;
        minX = Math.min(minX, x);
        minY = Math.min(minY, y);
        maxX = Math.max(maxX, x);
        maxY = Math.max(maxY, y);
        if (x < limitX && x - step >= x0 && x + step < x1 && y - step >= y0 && y + step < y1 &&
            marked(x - step, y) && marked(x + step, y) && marked(x, y - step) && marked(x, y + step) &&
            !marked(x - step, y - step) && !marked(x + step, y + step)) cross = true;
      }
    }
    return cross && maxX - minX <= 20 * scaleX && maxY - minY <= 20 * scaleY;
  }

  const inset = Math.max(2, Math.ceil(2 * Math.min(scaleX, scaleY)));
  const lastTop = rows.at(-2);
  const lastBottom = rows.at(-1);
  if (hasLonePlus(inset, lastTop + inset, dataLeft - inset, lastBottom - inset, dataLeft)) rows.pop();
  const lastLeft = columns.at(-2);
  const lastRight = columns.at(-1);
  // An add-column strip also stops drawing interior record dividers.
  const lastCenter = Math.floor((lastLeft + lastRight) / 2);
  const interiorRows = rows.slice(1, -1);
  const hasRecordDividers = interiorRows.some(y => isRowDivider(pixel(lastCenter, y)));
  if (!hasRecordDividers && hasLonePlus(lastLeft + inset, inset, lastRight - inset, top - inset,
    lastLeft + 32 * scaleX)) columns.pop();

  if (rows.length < 2 || columns.length < 2) return null;
  const midpoint = (first, last, scale) => Math.floor((first + last) / (2 * scale));
  return {
    coordinates: [Math.floor(firstCenter / scaleX), midpoint(rows[0], rows[1], scaleY),
      midpoint(columns.at(-2), columns.at(-1), scaleX), midpoint(rows.at(-2), rows.at(-1), scaleY)],
    rowCount: rows.length - 1,
    columnCount: columns.length - 1,
  };
}
