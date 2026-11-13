/** Pure geometry: image aspect ratios are width / height; all units are CSS px. */

function nonnegative(value, name) {
  if (typeof value !== "number" || !Number.isFinite(value) || value < 0) {
    throw new RangeError(`${name} must be a finite non-negative number`);
  }
  return value;
}

function nativeAspect(value) {
  if (typeof value !== "number" || !Number.isFinite(value) || value <= 0) {
    throw new RangeError("Camera aspect must be a finite positive number");
  }
  return value;
}

/** The centered native-ratio IMAGE rectangle, without a letterbox container. */
export function fitRect(width, height, aspect) {
  nonnegative(width, "width");
  nonnegative(height, "height");
  nativeAspect(aspect);
  if (width === 0 || height === 0) {
    return { x: width / 2, y: height / 2, width: 0, height: 0 };
  }
  let imageWidth;
  let imageHeight;
  if (width / height <= aspect) {
    imageWidth = width;
    imageHeight = width / aspect;
  } else {
    imageHeight = height;
    imageWidth = height * aspect;
  }
  return {
    x: (width - imageWidth) / 2,
    y: (height - imageHeight) / 2,
    width: imageWidth,
    height: imageHeight,
  };
}

/** Enumerate each unordered set partition once (Bell(6) = 203).
 * Row/column permutations do not affect available scale, so enumerating them
 * again would change only presentation order, not the maximum image area.
 */
function eachPartition(count, visit) {
  const groups = [];
  function next(index) {
    if (index === count) {
      visit(groups);
      return;
    }
    for (const group of groups) {
      group.push(index);
      next(index + 1);
      group.pop();
    }
    groups.push([index]);
    next(index + 1);
    groups.pop();
  }
  next(0);
}

function balancedGroups(order, count) {
  return Array.from({ length: count }, (_, i) =>
    order.slice(
      Math.floor((i * order.length) / count),
      Math.floor(((i + 1) * order.length) / count),
    ),
  );
}

function balancedExtentGroups(order, count, extent) {
  const groups = Array.from({ length: count }, () => []);
  const totals = new Array(count).fill(0);
  order.forEach((index, position) => {
    let target = position < count ? position : 0;
    if (position >= count) {
      for (let i = 1; i < count; i += 1) {
        if (totals[i] < totals[target]) target = i;
      }
    }
    groups[target].push(index);
    totals[target] += extent[index];
  });
  return groups;
}

/** Pack native-ratio images with the same IMAGE area, plus separate captions.
 *
 * Exact within the row/column-partition layout family for <= 6 cameras.
 * For larger sets, evaluate <= 256 deterministic candidate partitions, including
 * one-row/one-column baselines, sorted rows and balanced-extents arrangements.
 * This is not a claim of globally optimal arbitrary rectangle packing.
 *
 * Results retain input order. height includes captionHeight; imageHeight does
 * not. No integer rounding is applied, preserving equal area/native ratios.
 * Empty input returns []; a zero/infeasible region returns zero-sized centered
 * entries so callers can defer rendering until a usable region is available.
 */
export function packEqualCameras({
  width,
  height,
  aspects,
  gap = 10,
  captionHeight = 26,
}) {
  nonnegative(width, "width");
  nonnegative(height, "height");
  nonnegative(gap, "gap");
  nonnegative(captionHeight, "captionHeight");
  if (!Array.isArray(aspects)) throw new TypeError("aspects must be an array");
  aspects.forEach(nativeAspect);
  const count = aspects.length;
  if (!count) return [];
  const empty = () =>
    aspects.map(() => ({
      x: width / 2,
      y: height / 2,
      width: 0,
      height: 0,
      imageHeight: 0,
    }));
  if (width === 0 || height === 0 || captionHeight >= height) return empty();

  const roots = aspects.map(Math.sqrt);
  const inverseRoots = roots.map((value) => 1 / value);
  let best = null;

  function consider(groups, orientation) {
    let scale = Infinity;
    if (orientation === "rows") {
      let verticalCoefficient = 0;
      for (const group of groups) {
        const available = width - gap * (group.length - 1);
        if (available <= 0) return;
        const coefficient = group.reduce((sum, index) => sum + roots[index], 0);
        scale = Math.min(scale, available / coefficient);
        verticalCoefficient += Math.max(
          ...group.map((index) => inverseRoots[index]),
        );
      }
      const available =
        height - groups.length * captionHeight - gap * (groups.length - 1);
      if (available <= 0) return;
      scale = Math.min(scale, available / verticalCoefficient);
    } else {
      let horizontalCoefficient = 0;
      for (const group of groups) {
        const available =
          height - group.length * captionHeight - gap * (group.length - 1);
        if (available <= 0) return;
        const coefficient = group.reduce(
          (sum, index) => sum + inverseRoots[index],
          0,
        );
        scale = Math.min(scale, available / coefficient);
        horizontalCoefficient += Math.max(
          ...group.map((index) => roots[index]),
        );
      }
      const available = width - gap * (groups.length - 1);
      if (available <= 0) return;
      scale = Math.min(scale, available / horizontalCoefficient);
    }
    if (!Number.isFinite(scale) || scale <= 0) return;
    if (!best || scale > best.scale * (1 + 1e-12)) {
      best = { scale, orientation, groups: groups.map((group) => [...group]) };
    }
  }

  const all = aspects.map((_, index) => index);
  const visit = (groups) => {
    consider(groups, "rows");
    consider(groups, "columns");
  };
  if (count <= 6) {
    eachPartition(count, visit);
  } else {
    const seen = new Set();
    function boundedVisit(groups) {
      if (seen.size >= 256) return;
      // Preserve deterministic output order without evaluating duplicate sets.
      const normalized = groups
        .map((group) => [...group].sort((a, b) => a - b))
        .sort((a, b) => a[0] - b[0]);
      const key = normalized.map((group) => group.join(",")).join(";");
      if (seen.has(key)) return;
      seen.add(key);
      visit(normalized);
    }
    boundedVisit([all]);
    boundedVisit(all.map((index) => [index]));
    const sorted = [...all].sort((a, b) => aspects[a] - aspects[b] || a - b);
    const alternating = [];
    for (let low = 0, high = count - 1; low <= high; low += 1, high -= 1) {
      alternating.push(sorted[low]);
      if (low !== high) alternating.push(sorted[high]);
    }
    const groupCounts = new Set(
      Array.from({ length: Math.min(count, 12) }, (_, i) => i + 1),
    );
    const medianAspect = aspects[sorted[Math.floor(count / 2)]];
    const expectedRows =
      Math.sqrt(count) * Math.sqrt(height / width) * Math.sqrt(medianAspect);
    for (const center of [Math.sqrt(count), expectedRows]) {
      for (let delta = -3; delta <= 3; delta += 1) {
        const value = Math.max(1, Math.min(count, Math.round(center) + delta));
        if (Number.isFinite(value)) groupCounts.add(value);
      }
    }
    for (const order of [all, sorted, [...sorted].reverse(), alternating]) {
      for (const groups of groupCounts) {
        boundedVisit(balancedGroups(order, groups));
        boundedVisit(balancedExtentGroups(order, groups, roots));
        boundedVisit(balancedExtentGroups(order, groups, inverseRoots));
      }
    }
  }
  if (!best) return empty();

  // Tiny numerical inset avoids floating-point overflow of a boundary, without
  // snapping windows to pixel sizes or changing equal-area/aspect relationships.
  const scale = best.scale * (1 - 1e-12);
  const imageWidths = roots.map((value) => scale * value);
  const imageHeights = inverseRoots.map((value) => scale * value);
  const rects = new Array(count);
  if (best.orientation === "rows") {
    const rowHeights = best.groups.map(
      (group) => Math.max(...group.map((i) => imageHeights[i])) + captionHeight,
    );
    const totalHeight =
      rowHeights.reduce((sum, value) => sum + value, 0) +
      gap * (best.groups.length - 1);
    let y = (height - totalHeight) / 2;
    best.groups.forEach((group, row) => {
      const rowWidth =
        group.reduce((sum, i) => sum + imageWidths[i], 0) +
        gap * (group.length - 1);
      let x = (width - rowWidth) / 2;
      for (const i of group) {
        const windowHeight = imageHeights[i] + captionHeight;
        rects[i] = {
          x,
          y: y + (rowHeights[row] - windowHeight) / 2,
          width: imageWidths[i],
          height: windowHeight,
          imageHeight: imageHeights[i],
        };
        x += imageWidths[i] + gap;
      }
      y += rowHeights[row] + gap;
    });
  } else {
    const columnWidths = best.groups.map((group) =>
      Math.max(...group.map((i) => imageWidths[i])),
    );
    const totalWidth =
      columnWidths.reduce((sum, value) => sum + value, 0) +
      gap * (best.groups.length - 1);
    let x = (width - totalWidth) / 2;
    best.groups.forEach((group, column) => {
      const columnHeight =
        group.reduce((sum, i) => sum + imageHeights[i] + captionHeight, 0) +
        gap * (group.length - 1);
      let y = (height - columnHeight) / 2;
      for (const i of group) {
        rects[i] = {
          x: x + (columnWidths[column] - imageWidths[i]) / 2,
          y,
          width: imageWidths[i],
          height: imageHeights[i] + captionHeight,
          imageHeight: imageHeights[i],
        };
        y += imageHeights[i] + captionHeight + gap;
      }
      x += columnWidths[column] + gap;
    });
  }
  return rects;
}
