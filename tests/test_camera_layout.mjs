import assert from "node:assert/strict";
import test from "node:test";
import { fitRect, packEqualCameras } from "../web/camera-layout.js";

const close = (actual, expected, label = "value") => {
  assert.ok(
    Math.abs(actual - expected) <= Math.max(1e-7, Math.abs(expected) * 1e-9),
    `${label}: ${actual} != ${expected}`,
  );
};

function inspect(options) {
  const { width, height, aspects, gap = 10, captionHeight = 26 } = options;
  const rects = packEqualCameras(options);
  assert.equal(rects.length, aspects.length);
  let area = null;
  for (const [i, rect] of rects.entries()) {
    assert.ok(Object.values(rect).every(Number.isFinite));
    assert.ok(rect.width > 0 && rect.imageHeight > 0, `image ${i} has no area`);
    close(rect.width / rect.imageHeight, aspects[i], `native ratio ${i}`);
    close(rect.height - rect.imageHeight, captionHeight, `caption ${i}`);
    if (area === null) area = rect.width * rect.imageHeight;
    else close(rect.width * rect.imageHeight, area, `equal image area ${i}`);
    assert.ok(rect.x >= -1e-7 && rect.y >= -1e-7);
    assert.ok(rect.x + rect.width <= width + 1e-7);
    assert.ok(rect.y + rect.height <= height + 1e-7);
  }
  for (let i = 0; i < rects.length; i += 1) {
    for (let j = i + 1; j < rects.length; j += 1) {
      const a = rects[i],
        b = rects[j];
      assert.ok(
        a.x + a.width + gap <= b.x + 1e-7 ||
          b.x + b.width + gap <= a.x + 1e-7 ||
          a.y + a.height + gap <= b.y + 1e-7 ||
          b.y + b.height + gap <= a.y + 1e-7,
        `windows ${i},${j} overlap or violate gap`,
      );
    }
  }
  return { rects, area };
}

test("fitRect fits the actual image and centers native 16:9,16:10,portrait ratios", () => {
  for (const aspect of [16 / 9, 16 / 10, 9 / 16, 4 / 3, 1]) {
    const r = fitRect(960, 600, aspect);
    close(r.width / r.height, aspect);
    close(r.x * 2 + r.width, 960);
    close(r.y * 2 + r.height, 600);
    assert.ok(r.width <= 960 && r.height <= 600);
    assert.ok(
      Math.abs(r.width - 960) < 1e-7 || Math.abs(r.height - 600) < 1e-7,
    );
  }
  assert.deepEqual(fitRect(0, 400, 4 / 3), {
    x: 0,
    y: 200,
    width: 0,
    height: 0,
  });
});

test("same native ratios produce identical image/window dimensions", () => {
  for (const aspect of [4 / 3, 16 / 9, 16 / 10, 9 / 16]) {
    const { rects } = inspect({
      width: 1100,
      height: 620,
      aspects: [aspect, aspect, aspect],
    });
    for (const r of rects) {
      close(r.width, rects[0].width);
      close(r.height, rects[0].height);
    }
  }
});

test("mixed native ratios preserve original index, equal image area and non-overlap", () => {
  const cases = [
    [16 / 9, 4 / 3, 9 / 16],
    [16 / 10, 3 / 2, 1, 2 / 3],
    [0.2, 5, 1.2, 2.1, 0.8, 1.7],
  ];
  for (const aspects of cases) {
    for (const [width, height] of [
      [1200, 650],
      [650, 1100],
      [800, 800],
    ]) {
      inspect({ width, height, aspects });
    }
  }
});

test("all layouts beat or match the one-row equal-image-area baseline", () => {
  for (let n = 1; n <= 20; n += 1) {
    const aspects = Array.from(
      { length: n },
      (_, i) => [16 / 9, 4 / 3, 9 / 16, 16 / 10][i % 4],
    );
    const width = 1400,
      height = 900,
      gap = 10,
      captionHeight = 26;
    const oldRowScale = Math.max(
      0,
      Math.min(
        (width - gap * (n - 1)) / aspects.reduce((s, a) => s + Math.sqrt(a), 0),
        (height - captionHeight) /
          Math.max(...aspects.map((a) => 1 / Math.sqrt(a))),
      ),
    );
    const { area } = inspect({ width, height, aspects, gap, captionHeight });
    assert.ok(
      area >= oldRowScale ** 2 * (1 - 1e-9),
      `regressed single-row baseline at n=${n}`,
    );
  }
});

test("row/column enumeration matches independent permutation/split brute force for small sets", () => {
  function permutations(values) {
    if (!values.length) return [[]];
    return values.flatMap((v, i) =>
      permutations(values.filter((_, j) => j !== i)).map((tail) => [
        v,
        ...tail,
      ]),
    );
  }
  const width = 920,
    height = 530,
    gap = 13,
    captionHeight = 26;
  const aspects = [16 / 9, 9 / 16, 4 / 3, 1];
  let optimum = 0;
  for (const order of permutations(aspects)) {
    for (let mask = 0; mask < 2 ** (aspects.length - 1); mask += 1) {
      const groups = [[order[0]]];
      for (let i = 1; i < order.length; i += 1) {
        if (mask & (1 << (i - 1))) groups.push([]);
        groups.at(-1).push(order[i]);
      }
      const rows = Math.min(
        ...groups.map(
          (g) =>
            (width - gap * (g.length - 1)) /
            g.reduce((s, a) => s + Math.sqrt(a), 0),
        ),
        (height - groups.length * captionHeight - gap * (groups.length - 1)) /
          groups.reduce(
            (s, g) => s + Math.max(...g.map((a) => 1 / Math.sqrt(a))),
            0,
          ),
      );
      const columns = Math.min(
        ...groups.map(
          (g) =>
            (height - g.length * captionHeight - gap * (g.length - 1)) /
            g.reduce((s, a) => s + 1 / Math.sqrt(a), 0),
        ),
        (width - gap * (groups.length - 1)) /
          groups.reduce((s, g) => s + Math.max(...g.map(Math.sqrt)), 0),
      );
      optimum = Math.max(optimum, rows, columns);
    }
  }
  const { area } = inspect({ width, height, aspects, gap, captionHeight });
  close(area, optimum ** 2, "maximum image area");
});

test("larger camera sets stay bounded, deterministic and native-ratio correct", () => {
  for (const n of [7, 12, 24, 64]) {
    const options = {
      width: 1800,
      height: 1200,
      aspects: Array.from({ length: n }, (_, i) => 0.45 + (i % 9) * 0.24),
      gap: 4,
      captionHeight: 20,
    };
    const { rects } = inspect(options);
    assert.deepEqual(packEqualCameras(options), rects);
  }
});

test("extreme valid aspects, zero captions/gaps, and degenerate regions are explicit", () => {
  inspect({
    width: 1000,
    height: 700,
    aspects: [1e-6, 1e6, 1],
    gap: 0,
    captionHeight: 0,
  });
  inspect({
    width: 1000,
    height: 700,
    aspects: [16 / 9],
    gap: 0,
    captionHeight: 0,
  });
  assert.deepEqual(
    packEqualCameras({ width: 10, height: 10, aspects: [] }),
    [],
  );
  for (const options of [
    { width: 0, height: 500 },
    { width: 500, height: 0 },
    { width: 500, height: 20 },
  ]) {
    const result = packEqualCameras({ ...options, aspects: [4 / 3, 16 / 9] });
    assert.equal(result.length, 2);
    assert.ok(
      result.every(
        (r) => r.width === 0 && r.height === 0 && r.imageHeight === 0,
      ),
    );
  }
});

test("invalid inputs are rejected instead of silently distorting images", () => {
  for (const aspect of [0, -1, NaN, Infinity, "1.5"]) {
    assert.throws(() => fitRect(100, 100, aspect), RangeError);
    assert.throws(
      () => packEqualCameras({ width: 100, height: 100, aspects: [aspect] }),
      RangeError,
    );
  }
  assert.throws(() => fitRect(-1, 100, 1), RangeError);
  assert.throws(
    () => packEqualCameras({ width: 100, height: 100, aspects: [1], gap: -1 }),
    RangeError,
  );
  assert.throws(
    () =>
      packEqualCameras({
        width: 100,
        height: 100,
        aspects: [1],
        captionHeight: NaN,
      }),
    RangeError,
  );
});
