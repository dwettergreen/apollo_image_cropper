"""
Crop support for download_apollo.py: find the exposed frame in an ASU Apollo scan and
write just that part of the raw 16-bit TIFF, downloading only the rows that are needed.

Needs numpy and Pillow (pip install numpy pillow). Everything else is the standard library.
"""
import os
import struct

import numpy as np
from PIL import Image, ImageDraw

# Exposed-frame size as a fraction of the scan width (width, height), measured in October 2026
# on 60 ASU scans whose four frame edges are all visible. The 500 mm lens vignettes the edges,
# so its usable frame is smaller.
FRAME_PRIOR = {
    ('bw', 60): (0.761, 0.742),
    ('bw', 500): (0.741, 0.731),
    ('color', 60): (0.765, 0.752),
    ('color', 500): (0.745, 0.737),
}
# The frame's left edge sits between these fractions of the scan width on every scan measured;
# the search stays inside this window so it cannot lock onto sprocket holes or the scanner edge.
X0_WINDOW = (0.07, 0.155)
# In black-and-white scans the frame's top edge lies in this band (fraction of scan width).
BW_TOP_WINDOW = (0.015, 0.09)


def prior_for(film, lens_mm):
    return FRAME_PRIOR[(film if film in ('bw', 'color') else 'bw', 500 if str(lens_mm) == '500' else 60)]

# ----------------------------------------------------------------------------------------
# Frame detection on the small preview image
# ----------------------------------------------------------------------------------------

def _box_blur(a, r=1):
    k = 2 * r + 1
    p = np.pad(a, r, mode='edge')
    c = np.cumsum(np.cumsum(p, 0), 1)
    c = np.pad(c, ((1, 0), (1, 0)))
    return (c[k:, k:] - c[:-k, k:] - c[k:, :-k] + c[:-k, :-k]) / (k * k)


def _edges(a, k=2):
    g = _box_blur(_box_blur(a))
    dx = np.zeros_like(g); dy = np.zeros_like(g)
    dx[:, k:-k] = g[:, 2 * k:] - g[:, :-2 * k]
    dy[k:-k, :] = g[2 * k:, :] - g[:-2 * k, :]
    return (np.clip(dx, 0, None), np.clip(-dx, 0, None), np.clip(dy, 0, None), np.clip(-dy, 0, None))


def _to_gray(img):
    # 16-bit previews are scaled like 8-bit ones (full range = 1.0), not stretched to their own
    # maximum, so the fixed edge thresholds mean the same thing for every preview
    if img.mode in ('I;16', 'I;16B', 'I;16L', 'I'):
        a = np.asarray(img, dtype=np.float64)
        return a / max(65535.0, float(a.max()))
    return np.asarray(img.convert('L'), dtype=np.float64) / 255.0


def detect_frame(img, film, lens_mm, weak=0.04):
    """Find the exposed frame in a scan preview (any size; ASU small.png is 1,100 px wide).

    Horizontal edges: the left edge is searched only where frame edges actually occur
    (X0_WINDOW), which keeps it off the sprocket holes and the scanner edge.
    Vertical edges: lunar surface photos nearly always have ground along the bottom, while
    the top is often black sky that looks exactly like the black film border. So the bottom
    is anchored where the frame's left and right edges stop, and the top is placed from the
    calibrated frame height, accepting a detected top edge only where that height puts it.
    Returns the box in preview pixels plus per-side evidence and a confidence label."""
    prior = prior_for(film, lens_mm)
    full = _to_gray(img)
    H, W = full.shape
    px, py = int(round(prior[0] * W)), int(round(prior[1] * W))
    L, R, T, B = _edges(full)
    xa, xb = int(X0_WINDOW[0] * W), int(X0_WINDOW[1] * W)
    tol = max(2, int(round(0.012 * px)))

    # 1) left and right edges: best pair (column sums of edge evidence) with width near prior
    colL = L.sum(axis=0) / H
    colR = R.sum(axis=0) / H
    best = None
    for x0 in range(xa, xb + 1):
        for w in range(px - tol, px + tol + 1):
            x1 = x0 + w
            if x1 >= W - 2:
                continue
            v = colL[x0] + colR[x1]
            if best is None or v > best[0]:
                best = (v, x0, x1)
    _, xl, xr = best
    el, er = colL[xl], colR[xr]
    # column evidence is diluted by sky rows, so judge strength on the rows that have content
    rows_content = (np.maximum(L[:, max(0, xl - 2):xl + 3].max(axis=1), 0)
                    + np.maximum(R[:, max(0, xr - 2):xr + 3].max(axis=1), 0))
    k = 9
    sm = np.convolve(rows_content, np.ones(k) / k, mode='same')
    thr = max(0.03, 0.25 * np.percentile(sm, 95))
    on = np.where(sm > thr)[0]
    if len(on) == 0:
        return _fallback(W, H, px, py, xl, xr, 'no frame edges found')
    # keep the longest run of rows with side-edge evidence (small gaps allowed); shorter runs
    # are pieces of the neighboring frames, which share the same left and right edges
    gap = max(3, int(0.02 * W))
    runs, start, prev = [], on[0], on[0]
    for y in on[1:]:
        if y - prev > gap:
            runs.append((start, prev)); start = y
        prev = y
    runs.append((start, prev))
    content_top, content_bot = (int(v) for v in max(runs, key=lambda r: r[1] - r[0]))
    el = float(L[content_top:content_bot + 1, xl].mean())
    er = float(R[content_top:content_bot + 1, xr].mean())

    def row_peak(evid, center, radius):
        a0, a1 = xl + (xr - xl) // 20, xr - (xr - xl) // 20
        best, pos = -1.0, center
        for y in range(max(2, center - radius), min(H - 2, center + radius) + 1):
            v = evid[y, a0:a1].mean()
            if v > best:
                best, pos = v, y
        return pos, best

    r_far, r_near = max(4, int(0.03 * W)), max(3, int(0.02 * W))
    # 2) two hypotheses for the vertical position.
    #    A: anchor on the bottom (where the side edges stop); top placed from the frame height.
    #    B: anchor on the top (where the side edges start); bottom placed from the frame height.
    #    A is the usual case (ground along the bottom, often black sky at the top). B covers
    #    frames with a deep shadow along the bottom. Keep whichever fits inside the scan and
    #    has the stronger horizontal edges, with a mild preference for A.
    ybA, ebA = row_peak(B, content_bot, r_far)
    if ebA < weak:
        ybA, ebA = content_bot, 0.0
    ytA, etA = row_peak(T, ybA - py, r_near)
    if etA < weak:
        ytA, etA = ybA - py, 0.0
    ytB, etB = row_peak(T, content_top, r_far)
    if etB < weak:
        ytB, etB = content_top, 0.0
    ybB, ebB = row_peak(B, ytB + py, r_near)
    if ebB < weak:
        ybB, ebB = ytB + py, 0.0
    slack = int(0.01 * W)
    validA = ytA >= -slack and ybA <= H - 1 + slack
    validB = ytB >= -slack and ybB <= H - 1 + slack
    scoreA = 1.25 * (etA + ebA) if validA else -1
    scoreB = (etB + ebB) if validB else -1
    if scoreA >= scoreB:
        yt, et, yb, eb = ytA, etA, ybA, ebA
    else:
        yt, et, yb, eb = ytB, etB, ybB, ebB
    # black-and-white scans were cut per frame, so the frame top always sits in a narrow band
    if film == 'bw':
        lo, hi = int(BW_TOP_WINDOW[0] * W), int(BW_TOP_WINDOW[1] * W)
        if not (lo <= yt <= hi):
            c = (lo + hi) // 2
            yt2, et2 = row_peak(T, c, (hi - lo) // 2)
            yt, et = (yt2, et2) if et2 >= weak else (c, 0.0)
            yb2, eb2 = row_peak(B, yt + py, max(3, int(0.02 * W)))
            yb, eb = (yb2, eb2) if eb2 >= weak else (yt + py, 0.0)
    if yt < 0:
        yt, yb = 0, py
    if yb > H - 1:
        yb, yt = H - 1, H - 1 - py
    ok_size = abs((xr - xl) - px) <= 0.03 * px and abs((yb - yt) - py) <= 0.03 * py
    if not ok_size:
        yt = yb - py
    strong = [e >= weak for e in (el, er, et, eb)]
    n = sum(strong)
    conf = 'high' if (n >= 3 or (strong[0] and strong[1] and strong[3])) else ('medium' if n >= 2 else 'low')
    box = {'x0': int(xl), 'y0': int(yt), 'x1': int(xr), 'y1': int(yb), 'W': W, 'H': H,
           'evidence': [round(float(e), 3) for e in (el, er, et, eb)], 'confidence': conf}
    box.update(_inscribed(box, (L, R, T, B), strong))
    box['prior_w'], box['prior_h'] = prior
    return box


def _fit_side(evid, pos, lo, hi, vertical, radius=6, thr=0.06, slope=0.0, step=1):
    """Fit a straight line to one frame edge near the predicted line pos + slope*(t - mid).
    Returns (slope, intercept) giving position = slope * t + intercept along the side, or
    None if too few edge points were found."""
    ts, ps = [], []
    mid = (lo + hi) / 2
    lo, hi = int(lo), int(hi)
    for t in range(lo + (hi - lo) // 12, hi - (hi - lo) // 12, step):
        c = int(round(pos + slope * (t - mid)))
        a, b = c - radius, c + radius + 1
        if a < 0 or b > (evid.shape[1] if vertical else evid.shape[0]) or t < 0 or t >= (evid.shape[0] if vertical else evid.shape[1]):
            continue
        seg = evid[t, a:b] if vertical else evid[a:b, t]
        if seg.size == b - a and seg.max() > thr:
            i = int(seg.argmax())
            # sub-pixel peak by parabola through the three points around the maximum
            off = 0.0
            if 0 < i < seg.size - 1:
                d = seg[i - 1] - 2 * seg[i] + seg[i + 1]
                if d < 0:
                    off = 0.5 * (seg[i - 1] - seg[i + 1]) / d
            ts.append(t); ps.append(a + i + off)
    need = 0.3 * (hi - lo) / step
    if len(ts) < need:
        return None
    ts, ps = np.array(ts, float), np.array(ps, float)
    k = np.polyfit(ts, ps, 1)
    for _ in range(2):                                  # drop outliers (texture near the edge)
        r = ps - np.polyval(k, ts)
        keep = np.abs(r) < max(1.0, 2.5 * np.std(r))
        if keep.sum() < need:
            break
        ts, ps = ts[keep], ps[keep]
        k = np.polyfit(ts, ps, 1)
    return float(k[0]), float(k[1])


def _inscribed(box, edges, strong):
    """Largest axis-aligned rectangle inside the four fitted edge lines. The scans are rotated
    by a fraction of a degree, so this trims the thin wedges of border an axis-aligned box
    would otherwise include, without resampling the image."""
    L, R, T, B = edges
    x0, y0, x1, y1 = box['x0'], box['y0'], box['x1'], box['y1']
    fits = [_fit_side(L, x0, y0, y1, True) if strong[0] else None,
            _fit_side(R, x1, y0, y1, True) if strong[1] else None,
            _fit_side(T, y0, x0, x1, False) if strong[2] else None,
            _fit_side(B, y1, x0, x1, False) if strong[3] else None]
    lines = _complete_lines(fits, (x0, y0, x1, y1))
    r = _rect_inside(lines, (x0, y0, x1, y1))
    r['lines'] = lines
    r['fitted'] = [f is not None for f in fits]
    return r


def _complete_lines(fits, box):
    """Sides without a fitted edge are taken parallel to the opposite side at the box position."""
    fl, fr, ft, fb = fits
    x0, y0, x1, y1 = box
    fl = fl or ((fr[0], x0 - fr[0] * (y0 + y1) / 2) if fr else (0.0, float(x0)))
    fr = fr or ((fl[0], x1 - fl[0] * (y0 + y1) / 2) if fl else (0.0, float(x1)))
    ft = ft or ((fb[0], y0 - fb[0] * (x0 + x1) / 2) if fb else (0.0, float(y0)))
    fb = fb or ((ft[0], y1 - ft[0] * (x0 + x1) / 2) if ft else (0.0, float(y1)))
    return [fl, fr, ft, fb]


def _rect_inside(lines, box):
    fl, fr, ft, fb = lines
    cx0, cy0, cx1, cy1 = (float(v) for v in box)
    for _ in range(4):
        cx0 = max(fl[0] * cy0 + fl[1], fl[0] * cy1 + fl[1])
        cx1 = min(fr[0] * cy0 + fr[1], fr[0] * cy1 + fr[1])
        cy0 = max(ft[0] * cx0 + ft[1], ft[0] * cx1 + ft[1])
        cy1 = min(fb[0] * cx0 + fb[1], fb[0] * cx1 + fb[1])
    tilt = float(np.degrees(np.arctan(np.median([fl[0], fr[0], -ft[0], -fb[0]]))))
    return {'cx0': cx0, 'cy0': cy0, 'cx1': cx1, 'cy1': cy1, 'tilt_deg': round(tilt, 2)}


def _gate_edge_points(g, line, lo, hi, vertical, inward, r, step=4, band=2):
    """For many positions along one side, walk outward from inside the picture across the
    predicted edge line and return the first sharp drop in brightness: the edge of the scene.
    Walking from the inside (rather than from the black border inward) matters because color
    scans often show a band of stray light, without scene detail, just outside the gate."""
    sl, ic = line
    H, W = g.shape
    ts, ps = [], []
    k = 2
    for t in range(int(lo + 0.06 * (hi - lo)), int(hi - 0.06 * (hi - lo)), step):
        c = int(round(sl * t + ic))
        a0, a1 = c - r, c + r + 1
        if vertical:
            if a0 < 0 or a1 > W or t - band < 0 or t + band + 1 > H: continue
            prof = g[t - band:t + band + 1, a0:a1].mean(axis=0)
        else:
            if a0 < 0 or a1 > H or t - band < 0 or t + band + 1 > W: continue
            prof = g[a0:a1, t - band:t + band + 1].mean(axis=1)
        if inward > 0:
            prof = prof[::-1]                       # index 0 = inside the picture, walking outward
        prof = np.convolve(prof, np.ones(3) / 3, mode='same')
        scene = float(np.median(prof[3:15]))
        border = float(np.median(prof[-12:]))
        if scene - border < 0.04:
            continue                                # black sky against black border: no edge here
        need = 0.35 * (scene - border)
        hit = None
        for i in range(8, len(prof) - k - 1):
            if prof[i - k] - prof[i + k] >= need:
                j = i - k + int(np.argmax(prof[i - k:i + k + 1][:-1] - prof[i - k + 1:i + k + 1]))
                hit = j + 0.5
                break
        if hit is None:
            continue
        ps.append(a1 - 1 - hit if inward > 0 else a0 + hit)
        ts.append(t)
    return np.array(ts, float), np.array(ps, float)


def _robust_line(ts, ps, min_pts, tol=2.0, max_slope=0.025):
    """Straight line supported by the most points (Hough-style vote over small tilts), then a
    least-squares fit to those points. Scattered interior features get few votes."""
    if len(ts) < min_pts:
        return None
    best = (0, 0.0, 0.0)
    tm = ts.mean()
    for sl in np.arange(-max_slope, max_slope + 1e-9, 0.0005):
        bi = np.sort(ps - sl * (ts - tm))
        # densest window of width 2*tol
        j = np.searchsorted(bi, bi + 2 * tol, side='right')
        cnt = j - np.arange(len(bi))
        m = int(np.argmax(cnt))
        if cnt[m] > best[0]:
            best = (int(cnt[m]), float(sl), float(bi[m] + tol))
    n, sl, b0 = best
    if n < min_pts:
        return None
    inl = np.abs(ps - (sl * (ts - tm) + b0)) <= 2 * tol
    k = np.polyfit(ts[inl], ps[inl], 1)
    return float(k[0]), float(k[1]), int(inl.sum())


# Size of the scene area (inside the gate edge's thin dark line) as a fraction of scan width,
# measured on full-resolution scans where both opposite edges are visible. Used only to place a
# side whose edge cannot be seen (black sky, deep shadow).
SCENE_PRIOR = {'bw': (0.739, 0.745), 'color': (0.745, 0.751)}
# Width of the stray-light band between the scene edge and the outer edge seen on previews.
BAND_FRAC = 0.013


def refine_on_image(img, box, film, inset_frac=0.002, placed_inset_frac=0.010, window_frac=0.02):
    """Second pass on a larger copy of the same scan (ASU's medium image, exactly a quarter of the
    raw resolution). Each side's scene edge is located by walking outward from inside the
    picture; a side whose edge is not visible is placed from the opposite side and the measured
    frame size, with a larger safety inset. Adds the crop rectangle in this image's pixels
    ('mx0'...'my1', 'MW', 'MH') and per-side status ('sides': detected / placed)."""
    g = _to_gray(img).astype(np.float32)
    Hm, Wm = g.shape
    sx, sy = Wm / box['W'], Hm / box['H']
    fl, fr, ft, fb = box['lines']
    mapped = [((sx / sy) * fl[0], sx * fl[1]), ((sx / sy) * fr[0], sx * fr[1]),
              ((sy / sx) * ft[0], sy * ft[1]), ((sy / sx) * fb[0], sy * fb[1])]
    X0, Y0, X1, Y1 = box['cx0'] * sx, box['cy0'] * sy, box['cx1'] * sx, box['cy1'] * sy
    r = max(8, int(window_frac * Wm))
    spw, sph = SCENE_PRIOR.get(film, SCENE_PRIOR['bw'])
    full = [spw * Wm, spw * Wm, sph * Wm, sph * Wm]
    lines, ok = [None] * 4, [False] * 4
    for i, (vertical, inward) in enumerate(((True, 1), (True, -1), (False, 1), (False, -1))):
        lo, hi = (Y0, Y1) if vertical else (X0, X1)
        ts, ps = _gate_edge_points(g, mapped[i], lo, hi, vertical, inward, r)
        f = _robust_line(ts, ps, min_pts=max(8, int(0.25 * (hi - lo) / 4)))
        if f is None:
            continue
        mid = (lo + hi) / 2
        # the scene edge lies inside the first-pass line by roughly the band width
        expect = mapped[i][0] * mid + mapped[i][1] + inward * BAND_FRAC * Wm
        if abs(f[0] * mid + f[1] - expect) <= 0.02 * Wm:
            lines[i], ok[i] = (f[0], f[1]), True
    # opposite sides both found: their separation must match the frame size
    for i in (0, 2):
        if ok[i] and ok[i + 1]:
            m = ((Y0 + Y1) / 2) if i == 0 else ((X0 + X1) / 2)
            sep = (lines[i + 1][0] * m + lines[i + 1][1]) - (lines[i][0] * m + lines[i][1])
            if abs(sep - full[i]) > 0.02 * full[i]:
                # keep the side with more support in the first pass, re-place the other
                weaker = i if box['evidence'][i] < box['evidence'][i + 1] else i + 1
                ok[weaker], lines[weaker] = False, None
    status = []
    for i in range(4):
        j = i ^ 1
        inward = 1 if i in (0, 2) else -1
        if ok[i]:
            status.append('detected'); continue
        if ok[j]:
            lines[i] = (lines[j][0], lines[j][1] + inward * -1 * full[i])
        else:
            lines[i] = (mapped[i][0], mapped[i][1] + inward * BAND_FRAC * Wm)
        status.append('placed')
    # move every line inward by its safety inset, then take the largest upright rectangle inside
    fw = full[0]
    shifted = []
    for i, (sl, ic) in enumerate(lines):
        inward = 1 if i in (0, 2) else -1
        ins = (inset_frac if status[i] == 'detected' else placed_inset_frac) * fw
        shifted.append((sl, ic + inward * ins))
    rect = _rect_inside(shifted, (X0, Y0, X1, Y1))
    box.update({'mx0': rect['cx0'], 'my0': rect['cy0'], 'mx1': rect['cx1'], 'my1': rect['cy1'],
                'MW': Wm, 'MH': Hm, 'tilt_deg': rect['tilt_deg'], 'sides': status})
    n_det = status.count('detected')
    box['confidence'] = 'high' if n_det >= 3 else ('medium' if n_det == 2 else 'low')
    return box


def raw_crop_box(box, raw_w, raw_h, extra_inset_frac=0.0):
    """Map the crop rectangle to raw-scan pixels. Insets are applied in refine_on_image; an
    extra inset (fraction of the frame width, per side) can be added here."""
    if 'mx0' in box:
        x0f, y0f, x1f, y1f, W, H = box['mx0'], box['my0'], box['mx1'], box['my1'], box['MW'], box['MH']
    else:   # preview only: allow for the stray-light band and a margin
        x0f, y0f, x1f, y1f, W, H = box['cx0'], box['cy0'], box['cx1'], box['cy1'], box['W'], box['H']
        extra_inset_frac += BAND_FRAC + 0.006
    sx, sy = raw_w / W, raw_h / H
    ins = extra_inset_frac * raw_w
    x0 = int(np.ceil(x0f * sx + ins)); x1 = int(np.floor(x1f * sx - ins))
    y0 = int(np.ceil(y0f * sy + ins)); y1 = int(np.floor(y1f * sy - ins))
    return max(0, x0), max(0, y0), min(raw_w, x1), min(raw_h, y1)


def _fallback(W, H, px, py, xl, xr, why):
    yt = max(0, (H - py) // 2)
    return {'x0': int(xl), 'y0': int(yt), 'x1': int(xr), 'y1': int(yt + py), 'W': W, 'H': H,
            'evidence': [0, 0, 0, 0], 'confidence': 'low', 'note': why}


def check_image(img, crop_raw, raw_size, path, label='', max_w=900, frame_raw=None):
    """Save a small JPEG of the preview with the final crop drawn in green (and the detected
    frame edge in thin yellow, when given), for quick review."""
    im = img
    if im.mode not in ('RGB', 'L'):
        a = np.asarray(im, dtype=np.float64)
        im = Image.fromarray((255 * a / max(1.0, a.max())).astype(np.uint8))
    im = im.convert('RGB')
    sx, sy = im.width / raw_size[0], im.height / raw_size[1]
    x0, y0, x1, y1 = crop_raw
    d = ImageDraw.Draw(im)
    if frame_raw:
        fx0, fy0, fx1, fy1 = frame_raw
        d.rectangle([fx0 * sx, fy0 * sy, fx1 * sx, fy1 * sy], outline=(240, 200, 40), width=1)
    d.rectangle([x0 * sx, y0 * sy, x1 * sx, y1 * sy], outline=(60, 230, 60), width=3)
    if label:
        d.rectangle([0, 0, 8 + 7 * len(label), 16], fill=(0, 0, 0))
        d.text((4, 2), label, fill=(255, 230, 80))
    s = min(1.0, max_w / im.width)
    im.resize((int(im.width * s), int(im.height * s))).save(path, quality=85)


# Where the detected frame starts in black-and-white scans (fraction of scan width), measured on
# 53 frames drawn from all 25 black-and-white candidate magazines (October 2026). Observed
# ranges: left 0.108 to 0.148, top 0.045 to 0.114. A frame well outside this is flagged.
BW_TYPICAL = {'x0': 0.132, 'y0': 0.072}
BW_POSITION_TOL = {'x0': 0.035, 'y0': 0.055}


def apply_inset(frame, inset_pct):
    """Trim inset_pct percent of the frame's width (left and right) and height (top and bottom)."""
    x0, y0, x1, y1 = frame
    ix, iy = inset_pct / 100.0 * (x1 - x0), inset_pct / 100.0 * (y1 - y0)
    return (int(np.ceil(x0 + ix)), int(np.ceil(y0 + iy)), int(np.floor(x1 - ix)), int(np.floor(y1 - iy)))


def find_crop(small_img, med_img, film, lens_mm, raw_w, raw_h, inset_pct=5.0):
    """Full detection: first pass on the small preview, precise pass on the medium image, then
    the extra safety trim. Returns (final crop in raw pixels, info dict). info['frame_crop'] is
    the crop before the extra trim, so a different trim can be applied later without redoing
    the detection."""
    box = detect_frame(small_img, film, lens_mm)
    first = raw_crop_box(box, raw_w, raw_h)        # preview-only crop, already inside the edges
    frame, sides = first, ['preview'] * 4
    if med_img is not None:
        box = refine_on_image(med_img, box, film)
        refined = raw_crop_box(box, raw_w, raw_h)
        # the precise pass may move an edge outward by at most 1% of the frame; anything more
        # would mean it latched onto something else, and the first pass is kept for that side
        tx, ty = 0.01 * (first[2] - first[0]), 0.01 * (first[3] - first[1])
        frame = (max(refined[0], int(first[0] - tx)), max(refined[1], int(first[1] - ty)),
                 min(refined[2], int(first[2] + tx)), min(refined[3], int(first[3] + ty)))
        sides = box['sides']
    crop = apply_inset(frame, inset_pct)
    info = {'confidence': box['confidence'], 'sides': sides, 'tilt_deg': box.get('tilt_deg', 0.0),
            'frame_crop': [int(v) for v in frame], 'inset_pct': inset_pct, 'notes': []}
    w, h = frame[2] - frame[0], frame[3] - frame[1]
    spw, sph = SCENE_PRIOR.get(film, SCENE_PRIOR['bw'])
    # a frame much smaller than a full frame means something went wrong
    if w < 0.85 * spw * raw_w or h < 0.85 * sph * raw_w:
        info['confidence'] = 'low'
        info['notes'].append('frame smaller than expected')
    if film == 'bw':
        dx = abs(frame[0] / raw_w - BW_TYPICAL['x0'])
        dy = abs(frame[1] / raw_w - BW_TYPICAL['y0'])
        if dx > BW_POSITION_TOL['x0'] or dy > BW_POSITION_TOL['y0']:
            info['confidence'] = 'low'
            info['notes'].append('frame position unusual for a black-and-white scan')
    return crop, info


# ----------------------------------------------------------------------------------------
# TIFF reading (header only, via HTTP Range) and writing
# ----------------------------------------------------------------------------------------
TAG_TYPES = {1: 'B', 2: 's', 3: 'H', 4: 'I', 5: 'II', 16: 'Q'}
TYPE_SIZE = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 7: 1, 16: 8}


def parse_tiff_header(buf):
    """Parse the first IFD of a little- or big-endian classic TIFF held in buf (bytes)."""
    bo = buf[:2]
    if bo == b'II': e = '<'
    elif bo == b'MM': e = '>'
    else: raise ValueError('not a TIFF')
    if struct.unpack(e + 'H', buf[2:4])[0] != 42:
        raise ValueError('BigTIFF or unknown TIFF variant')
    ifd = struct.unpack(e + 'I', buf[4:8])[0]
    n = struct.unpack(e + 'H', buf[ifd:ifd + 2])[0]
    tags = {}
    for k in range(n):
        o = ifd + 2 + 12 * k
        tag, typ, cnt = struct.unpack(e + 'HHI', buf[o:o + 8])
        size = TYPE_SIZE.get(typ, 1) * cnt
        voff = o + 8 if size <= 4 else struct.unpack(e + 'I', buf[o + 8:o + 12])[0]
        if voff + size > len(buf):
            tags[tag] = ('need', voff + size)
            continue
        raw = buf[voff:voff + size]
        if typ == 3: val = list(struct.unpack(e + 'H' * cnt, raw))
        elif typ == 4: val = list(struct.unpack(e + 'I' * cnt, raw))
        elif typ == 5: val = [struct.unpack(e + 'II', raw[8 * j:8 * j + 8]) for j in range(cnt)]
        elif typ == 2: val = raw.rstrip(b'\0').decode('latin-1')
        else: val = raw
        tags[tag] = val
    need = max([v[1] for v in tags.values() if isinstance(v, tuple) and len(v) == 2 and v[0] == 'need'] or [0])
    return e, tags, need


def describe_tiff(tags):
    g = lambda t, d=None: (tags.get(t, [d]) if isinstance(tags.get(t, [d]), list) else [tags.get(t)])
    W, H = g(256)[0], g(257)[0]
    spp = g(277, 1)[0]
    bps = g(258, 8)
    comp = g(259, 1)[0]
    planar = g(284, 1)[0]
    offs, counts = tags.get(273), tags.get(279)
    return {'W': W, 'H': H, 'spp': spp, 'bps': bps, 'comp': comp, 'planar': planar,
            'photometric': g(262, 1)[0], 'offsets': offs, 'counts': counts,
            'rps': g(278, H)[0], 'xres': tags.get(282), 'yres': tags.get(283), 'resunit': g(296, 2)[0]}


def write_tiff_header(f, W, H, spp, bits, photometric, xres, yres, resunit, description, rows_per_strip=64):
    """Write a classic little-endian uncompressed TIFF header + IFD, leaving room for the pixel
    data that follows. Returns the file offset where pixel data must start."""
    bpp = spp * bits // 8
    row_bytes = W * bpp
    nstrips = (H + rows_per_strip - 1) // rows_per_strip
    desc = (description + '\0').encode('latin-1', 'replace')
    software = b'download_apollo.py crop\0'
    entries = []   # (tag, type, count, payload bytes or int)
    def add(tag, typ, count, payload): entries.append((tag, typ, count, payload))
    add(254, 4, 1, 0)
    add(256, 4, 1, W)
    add(257, 4, 1, H)
    add(258, 3, spp, struct.pack('<' + 'H' * spp, *([bits] * spp)))
    add(259, 3, 1, 1)
    add(262, 3, 1, photometric)
    add(270, 2, len(desc), desc)
    add(273, 4, nstrips, None)                    # strip offsets, filled below
    add(277, 3, 1, spp)
    add(278, 4, 1, rows_per_strip)
    add(279, 4, nstrips, None)                    # strip byte counts
    add(282, 5, 1, struct.pack('<II', *xres))
    add(283, 5, 1, struct.pack('<II', *yres))
    add(284, 3, 1, 1)
    add(296, 3, 1, resunit)
    add(305, 2, len(software), software)
    add(339, 3, spp, struct.pack('<' + 'H' * spp, *([1] * spp)))
    entries.sort(key=lambda t: t[0])
    ifd_off = 8
    ifd_size = 2 + 12 * len(entries) + 4
    extra_off = ifd_off + ifd_size
    # lay out out-of-line values
    blobs = []
    cursor = extra_off
    sizes = {}
    for tag, typ, cnt, payload in entries:
        size = TYPE_SIZE[typ] * cnt
        if size > 4:
            sizes[tag] = cursor
            cursor += size + (size & 1)
    data_off = cursor + (-cursor % 16)
    strip_offsets = [data_off + s * rows_per_strip * row_bytes for s in range(nstrips)]
    strip_counts = [min(rows_per_strip, H - s * rows_per_strip) * row_bytes for s in range(nstrips)]
    f.write(b'II' + struct.pack('<HI', 42, ifd_off))
    f.write(struct.pack('<H', len(entries)))
    for tag, typ, cnt, payload in entries:
        size = TYPE_SIZE[typ] * cnt
        if tag == 273: payload = struct.pack('<' + 'I' * cnt, *strip_offsets)
        if tag == 279: payload = struct.pack('<' + 'I' * cnt, *strip_counts)
        if size > 4:
            f.write(struct.pack('<HHII', tag, typ, cnt, sizes[tag]))
            blobs.append((sizes[tag], payload))
        elif isinstance(payload, int):
            fmt = {3: '<HHIHH', 4: '<HHII'}[typ]
            f.write(struct.pack(fmt, tag, typ, cnt, payload, 0) if typ == 3 else struct.pack(fmt, tag, typ, cnt, payload))
        else:
            f.write(struct.pack('<HHI', tag, typ, cnt) + payload.ljust(4, b'\0'))
    f.write(struct.pack('<I', 0))
    for off, payload in blobs:
        assert f.tell() <= off
        f.write(b'\0' * (off - f.tell()))
        f.write(payload)
    f.write(b'\0' * (data_off - f.tell()))
    if data_off + H * row_bytes >= 2 ** 32:
        raise ValueError('cropped image too large for classic TIFF')
    return data_off


def crop_from_file(tif_path, src, box, out_path, description, bigendian=False):
    """Crop from a complete local copy of an uncompressed TIFF with any strip layout."""
    bits = src['bps'][0]
    bpp = src['spp'] * bits // 8
    row_bytes = src['W'] * bpp
    rps = src['rps']
    x0, y0, x1, y1 = box
    tmp = out_path + '.writing'
    with open(tif_path, 'rb') as fin, open(tmp, 'wb') as fout:
        write_tiff_header(fout, x1 - x0, y1 - y0, src['spp'], bits, src['photometric'],
                          tuple(src['xres'][0]) if src['xres'] else (72, 1),
                          tuple(src['yres'][0]) if src['yres'] else (72, 1),
                          src['resunit'], description)
        for y in range(y0, y1):
            fin.seek(src['offsets'][y // rps] + (y % rps) * row_bytes + x0 * bpp)
            seg = fin.read((x1 - x0) * bpp)
            if bigendian and bits == 16:
                seg = np.frombuffer(seg, dtype='>u2').astype('<u2').tobytes()
            fout.write(seg)
    os.replace(tmp, out_path)


def crop_from_rows(rows_path, src, box, out_path, description, bigendian=False):
    """rows_path holds raw rows y0..y1 of the source TIFF (contiguous strips). Write the crop."""
    bits = src['bps'][0]
    bpp = src['spp'] * bits // 8
    row_bytes = src['W'] * bpp
    x0, y0, x1, y1 = box
    W, H = x1 - x0, y1 - y0
    tmp = out_path + '.writing'
    with open(rows_path, 'rb') as fin, open(tmp, 'wb') as fout:
        write_tiff_header(fout, W, H, src['spp'], bits, src['photometric'],
                          tuple(src['xres'][0]) if src['xres'] else (72, 1),
                          tuple(src['yres'][0]) if src['yres'] else (72, 1),
                          src['resunit'], description)
        for _ in range(H):
            row = fin.read(row_bytes)
            if len(row) != row_bytes:
                raise IOError('row data ended early')
            seg = row[x0 * bpp:x1 * bpp]
            if bigendian and bits == 16:
                seg = np.frombuffer(seg, dtype='>u2').astype('<u2').tobytes()
            fout.write(seg)
    os.replace(tmp, out_path)
