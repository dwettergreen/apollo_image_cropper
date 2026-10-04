#!/usr/bin/env python3
"""
Download Apollo lunar surface frames listed in apollo_surface_frames.csv.

Images come from Arizona State University's "March to the Moon" archive of the NASA Johnson
Space Center film scans (https://tothemoon.im-ldi.com), or from the Lunar and Planetary
Institute (LPI) Apollo Image Atlas for the "lpi" option.

Resolutions (--res):
  raw        Raw 16-bit TIFF film scan, the highest resolution available. Includes the film
             border and sprocket area. About 1.36 GB per color frame, 0.35 GB per B&W frame.
  processed  Processed PNG from the same scan. About 0.30 GB color, 0.17 GB B&W.
  preview    Medium PNG, about 3,600 px across, about 18 MB.
  small      Quick-look PNG, about 1,100 px across, about 1.6 MB.
  lpi        LPI print-resolution JPG (3,900 px, already cropped), about 4 to 8 MB.

Cropping (--crop, raw only):
  Finds the exposed picture area in each scan and writes just that part as an uncompressed
  16-bit TIFF at full scan resolution, downloading only the rows of the raw scan that are
  needed. By default it also trims a further 5% of the picture's width and height from every
  side (--crop-inset 5), so no film border, sprocket hole or frame number can be left along
  an edge. Reseau crosses (the small "+" marks) are part of the picture and remain. Each frame
  gets a small *.crop-check.jpg (green = what is saved), and crop_review.html in the output
  folder shows them all, with any frame that needs a look listed first. Needs numpy and Pillow.
  Add --crop-preview-only to work out and review the crops without downloading raw scans.

Typical use, with a keep list saved from review.html:
  python3 download_apollo.py --ids keep_list.txt --crop --limit 3            # quick test
  python3 download_apollo.py --ids keep_list.txt --crop --random 50          # 50 chosen at random
  python3 download_apollo.py --ids keep_list.txt --crop --crop-preview-only  # check all crops
  python3 download_apollo.py --ids keep_list.txt --crop --dry-run            # sizes and disk check
  python3 download_apollo.py --ids keep_list.txt --crop                      # cropped TIFFs
  python3 download_apollo.py --ids keep_list.txt --crop --skip-ids bad_crops.txt
  python3 download_apollo.py --res small --tier T1 T2                       # quick-look images

The script checks sizes before downloading, shows the total and your free disk space, and asks
before starting (skip the prompt with --yes). Downloads resume where they stopped if
interrupted, and finished files are skipped, so it is safe to run the same command again.
A manifest.csv in the output folder records each file.

Requires Python 3.8 or newer; --crop also needs numpy and Pillow. Installing certifi
(python3 -m pip install certifi) avoids the certificate error that Python from python.org on a
Mac shows on first use.
"""
import argparse
import csv
import io
import json
import os
import random
import shutil
import ssl
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

USER_AGENT = 'apollo-terrain-downloader/1.3 (personal research; polite, low concurrency)'
RES_COLUMN = {'raw': 'raw_tif_url', 'processed': 'processed_png_url', 'preview': 'preview_png_url',
              'small': 'small_png_url', 'lpi': 'lpi_jpg_url'}
CHUNK = 1 << 20
RETRIES = 6

_print_lock = threading.Lock()


def log(*a):
    with _print_lock:
        print(*a, flush=True)


def human(n):
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if abs(n) < 1024 or unit == 'TB':
            return f'{n:,.1f} {unit}' if unit != 'B' else f'{n} B'
        n /= 1024.0


def read_ids(path):
    ids = []
    with open(path, encoding='utf-8') as f:
        for line in f:
            line = line.split('#', 1)[0].strip().replace(',', ' ')
            ids.extend(x.strip().upper() for x in line.split() if x.strip())
    return ids


def _ssl_context():
    """System certificates plus, when installed, the certifi bundle. Python from python.org on a
    Mac starts with no certificates at all, which makes every HTTPS request fail."""
    ctx = ssl.create_default_context()
    try:
        import certifi
        ctx.load_verify_locations(certifi.where())
    except Exception:
        pass
    return ctx


SSL_CONTEXT = _ssl_context()


class CertificateError(Exception):
    pass


def _is_cert_error(e):
    r = getattr(e, 'reason', e)
    return isinstance(r, ssl.SSLCertVerificationError) or 'CERTIFICATE_VERIFY_FAILED' in str(e)


CERT_HELP = """Python could not verify the server's security certificate. This is the usual first-run
problem with Python installed from python.org on a Mac: it has no certificates yet. Either fix
works (then run the same command again):
  1. python3 -m pip install certifi
  2. or, in Finder, open Applications > Python 3.x and double-click "Install Certificates.command"."""


def request(url, method='GET', headers=None, timeout=60):
    h = {'User-Agent': USER_AGENT}
    h.update(headers or {})
    try:
        return urllib.request.urlopen(urllib.request.Request(url, method=method, headers=h),
                                      timeout=timeout, context=SSL_CONTEXT)
    except urllib.error.HTTPError:
        raise
    except Exception as e:
        if _is_cert_error(e):
            raise CertificateError(str(e)) from None   # not worth retrying
        raise


def check_connection(url):
    """One quick request before starting, so a setup problem is reported at once."""
    try:
        with request(url, method='HEAD', timeout=30):
            return True
    except CertificateError:
        log(CERT_HELP)
    except urllib.error.HTTPError as e:
        if e.code in (404, 405, 410):
            return True                                 # the server answered; that is enough
        log(f'The image server answered with HTTP {e.code}; it may be down. Try again later.')
    except Exception as e:
        log(f'Could not reach the image server ({e}). Check your internet connection and try again.')
    return False


def head_size(url):
    for attempt in range(RETRIES):
        try:
            with request(url, method='HEAD', timeout=30) as r:
                n = r.headers.get('Content-Length')
                return int(n) if n is not None else None
        except urllib.error.HTTPError as e:
            if e.code in (404, 410):
                return -1
            time.sleep(min(60, 2 ** attempt))
        except CertificateError:
            raise
        except Exception:
            time.sleep(min(60, 2 ** attempt))
    return None


def fetch_bytes(url, start=None, end=None):
    """GET a whole small file, or the byte range start..end inclusive, with retries."""
    headers = {'Range': f'bytes={start}-{end}'} if start is not None else {}
    err = None
    for attempt in range(RETRIES):
        try:
            with request(url, headers=headers, timeout=120) as r:
                data = r.read()
            if start is not None and r.status == 200:      # server ignored Range
                data = data[start:end + 1]
            return data
        except urllib.error.HTTPError as e:
            if e.code in (404, 410):
                raise
            err = e
        except CertificateError:
            raise
        except Exception as e:
            err = e
        time.sleep(min(120, 5 * 2 ** attempt))
    raise IOError(f'could not fetch {url}: {err}')


def download(url, dest, expected, start=0, length=None):
    """Download url (or bytes start..start+length-1 of it) to dest with resume.
    Returns (status, bytes)."""
    if length is not None:
        expected = length
    if os.path.exists(dest) and (expected is None or os.path.getsize(dest) == expected):
        return 'skipped (already complete)', os.path.getsize(dest)
    os.makedirs(os.path.dirname(dest) or '.', exist_ok=True)
    part = dest + '.part'
    for attempt in range(RETRIES):
        have = os.path.getsize(part) if os.path.exists(part) else 0
        if expected is not None and have > expected:
            os.remove(part)
            have = 0
        if length is not None:
            headers = {'Range': f'bytes={start + have}-{start + length - 1}'}
        else:
            headers = {'Range': f'bytes={have}-'} if have else {}
        try:
            with request(url, headers=headers, timeout=120) as r:
                if headers and r.status != 206:
                    raise IOError('server did not honor the byte range')
                with open(part, 'ab' if have else 'wb') as f:
                    while True:
                        buf = r.read(CHUNK)
                        if not buf:
                            break
                        f.write(buf)
            got = os.path.getsize(part)
            if expected is not None and got != expected:
                raise IOError(f'size mismatch: got {got}, expected {expected}')
            os.replace(part, dest)
            return 'downloaded', got
        except urllib.error.HTTPError as e:
            if e.code == 416 and expected is not None and have == expected:
                os.replace(part, dest)
                return 'downloaded', have
            if e.code in (404, 410):
                return f'missing on server (HTTP {e.code})', 0
            err = f'HTTP {e.code}'
        except CertificateError:
            raise
        except Exception as e:  # network drop, timeout, size mismatch
            err = str(e)
        wait = min(120, 5 * 2 ** attempt)
        log(f'  retry {attempt + 1}/{RETRIES} in {wait}s for {os.path.basename(dest)}: {err}')
        time.sleep(wait)
    return 'failed after retries (rerun to resume)', os.path.getsize(part) if os.path.exists(part) else 0


# ------------------------------------------------------------------------------------------
# Crop mode
# ------------------------------------------------------------------------------------------

def read_raw_header(url, ac):
    """Read and describe the raw TIFF header with Range requests."""
    n = 262144
    for _ in range(4):
        buf = fetch_bytes(url, 0, n - 1)
        e, tags, need = ac.parse_tiff_header(buf)
        if need <= len(buf):
            break
        n = need + 1024
    src = ac.describe_tiff(tags)
    src['bigendian'] = (e == '>')
    if src['comp'] != 1:
        raise ValueError('raw TIFF is compressed; crop mode needs an uncompressed scan')
    if src['spp'] > 1 and src['planar'] != 1:
        raise ValueError('raw TIFF uses separate color planes; not supported')
    bits = src['bps'][0]
    row_bytes = src['W'] * src['spp'] * bits // 8
    offs, rps = src['offsets'], src['rps']
    src['row_bytes'] = row_bytes
    src['first'] = offs[0]
    # rows are contiguous when every strip starts where the previous one ended
    src['contiguous'] = all(offs[i + 1] - offs[i] == rps * row_bytes for i in range(len(offs) - 1))
    return src


def _remove(*paths):
    for p in paths:
        if os.path.exists(p):
            os.remove(p)


def crop_frame(r, dest_dir, a, ac):
    """Detect the frame, download only its rows, write the cropped TIFF. Returns (status, bytes, meta)."""
    from PIL import Image
    fid = r['frame_id']
    raw_url = r['raw_tif_url']
    out_tif = os.path.join(dest_dir, fid + '.crop.tif')
    meta_path = os.path.join(dest_dir, fid + '.crop.json')
    check_jpg = os.path.join(dest_dir, fid + '.crop-check.jpg')
    rows_file = os.path.join(dest_dir, fid + '.rows')
    os.makedirs(dest_dir, exist_ok=True)
    meta, small, redraw = None, None, False
    if os.path.exists(meta_path):
        try:
            meta = json.load(open(meta_path))
        except Exception:
            meta = None
        if meta and 'frame_crop' not in meta.get('info', {}):
            meta = None                            # written by an older version: work it out again
            _remove(out_tif, rows_file, rows_file + '.part')
    if meta and abs(meta['info'].get('inset_pct', -1) - a.crop_inset) > 1e-9:
        # the trim was changed since the last run: re-apply it to the saved frame and discard
        # files made with the old trim (the detection itself is reused)
        meta['crop'] = list(ac.apply_inset(meta['info']['frame_crop'], a.crop_inset))
        meta['info']['inset_pct'] = a.crop_inset
        _remove(out_tif, rows_file, rows_file + '.part')
        with open(meta_path, 'w') as f:
            json.dump(meta, f)
        redraw = True
    if meta and os.path.exists(out_tif) and not a.crop_preview_only:
        return 'skipped (already complete)', os.path.getsize(out_tif), meta
    if meta is None:
        src = read_raw_header(raw_url, ac)
        small = Image.open(io.BytesIO(fetch_bytes(r['small_png_url'])))
        small.load()
        med = Image.open(io.BytesIO(fetch_bytes(r['preview_png_url'])))
        med.load()
        crop, info = ac.find_crop(small, med, r['film'], r.get('lens_mm', ''), src['W'], src['H'],
                                  a.crop_inset)
        keep = {k: src[k] for k in ('W', 'H', 'spp', 'bps', 'comp', 'planar', 'photometric', 'rps',
                                    'xres', 'yres', 'resunit', 'row_bytes', 'first', 'contiguous',
                                    'bigendian')}
        keep['offsets'] = src['offsets'] if not src['contiguous'] else None
        meta = {'frame_id': fid, 'crop': list(crop), 'info': info, 'src': keep, 'raw_url': raw_url,
                'film': r['film']}
        with open(meta_path, 'w') as f:
            json.dump(meta, f)
        redraw = True
    if (redraw or not os.path.exists(check_jpg)) and not a.no_check_images:
        if small is None:
            small = Image.open(io.BytesIO(fetch_bytes(r['small_png_url'])))
            small.load()
        c = meta['crop']
        label = f"{fid}  {meta['info']['confidence']}  {c[2] - c[0]}x{c[3] - c[1]} px  trim {meta['info']['inset_pct']:g}%"
        ac.check_image(small, c, (meta['src']['W'], meta['src']['H']), check_jpg, label,
                       frame_raw=meta['info']['frame_crop'])
    src = meta['src']
    x0, y0, x1, y1 = meta['crop']
    info = meta['info']
    if a.crop_preview_only:
        return 'crop worked out (preview only)', 0, meta
    desc = (f"{fid} cropped to the exposed frame with a {info['inset_pct']:g}% safety trim per side. "
            f"Source: NASA Johnson Space Center scan via ASU March to the Moon, {raw_url}. "
            f"Crop in source pixels: x {x0}-{x1}, y {y0}-{y1}. Scan tilt {info['tilt_deg']} deg (not corrected).")
    if src['contiguous']:
        start = src['first'] + y0 * src['row_bytes']
        length = (y1 - y0) * src['row_bytes']
        status, _ = download(raw_url, rows_file, None, start=start, length=length)
        if not status.startswith(('downloaded', 'skipped')):
            return status, 0, meta
        ac.crop_from_rows(rows_file, src, (x0, y0, x1, y1), out_tif, desc, src['bigendian'])
        os.remove(rows_file)
    else:
        full = os.path.join(dest_dir, fid + '.source.tif')
        status, _ = download(raw_url, full, None)
        if not status.startswith(('downloaded', 'skipped')):
            return status, 0, meta
        ac.crop_from_file(full, src, (x0, y0, x1, y1), out_tif, desc, src['bigendian'])
        os.remove(full)
    return 'cropped', os.path.getsize(out_tif), meta


def _crop_records(out_dir):
    recs = []
    for root, _, files in os.walk(out_dir):
        for fn in files:
            if fn.endswith('.crop.json'):
                try:
                    m = json.load(open(os.path.join(root, fn)))
                except Exception:
                    continue
                if 'frame_crop' not in m.get('info', {}):
                    continue
                m['_dir'] = root
                recs.append(m)
    recs.sort(key=lambda m: m['frame_id'])
    return recs


def write_crops_csv(out_dir):
    """crops.csv and crop_review.html, one entry per frame, rebuilt from the *.crop.json records."""
    recs = _crop_records(out_dir)
    with open(os.path.join(out_dir, 'crops.csv'), 'w', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        w.writerow(['frame_id', 'confidence', 'notes', 'width_px', 'height_px', 'x0', 'y0', 'x1', 'y1',
                    'trim_pct', 'left_edge', 'right_edge', 'top_edge', 'bottom_edge', 'tilt_deg',
                    'tiff_written', 'check_image'])
        for m in recs:
            x0, y0, x1, y1 = m['crop']
            i = m['info']
            w.writerow([m['frame_id'], i['confidence'], '; '.join(i.get('notes', [])), x1 - x0, y1 - y0,
                        x0, y0, x1, y1, i['inset_pct'], *i['sides'], i['tilt_deg'],
                        'yes' if os.path.exists(os.path.join(m['_dir'], m['frame_id'] + '.crop.tif')) else 'no',
                        os.path.join(m['_dir'], m['frame_id'] + '.crop-check.jpg')])
    order = {'low': 0, 'medium': 1, 'high': 2}
    recs.sort(key=lambda m: (order.get(m['info']['confidence'], 0), m['frame_id']))
    cards = []
    for m in recs:
        rel = os.path.relpath(os.path.join(m['_dir'], m['frame_id'] + '.crop-check.jpg'), out_dir).replace(os.sep, '/')
        conf = m['info']['confidence']
        note = '; '.join(m['info'].get('notes', []))
        cards.append(f'<figure class="{conf}" data-id="{m["frame_id"]}"><img loading="lazy" src="{rel}" alt="">'
                     f'<figcaption><b>{m["frame_id"]}</b> <span>{conf}</span>{(" · " + note) if note else ""}</figcaption></figure>')
    n_check = sum(1 for m in recs if m['info']['confidence'] != 'high')
    html = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Crop review</title>
<style>body{{margin:0;background:#111;color:#ddd;font:13px -apple-system,Segoe UI,Helvetica,Arial,sans-serif}}
header{{position:sticky;top:0;background:#111e;padding:10px 14px;border-bottom:1px solid #333}}
h1{{font-size:16px;margin:0 0 4px}} p{{margin:2px 0;color:#999}} button{{background:#222;color:#ddd;border:1px solid #444;border-radius:5px;padding:4px 9px;margin:6px 6px 0 0;cursor:pointer}}
main{{display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:10px;padding:12px 14px}}
figure{{margin:0;background:#1b1b1b;border:2px solid transparent;border-radius:6px;overflow:hidden;cursor:pointer}}
figure img{{width:100%;display:block}} figcaption{{padding:5px 7px}} .low{{border-color:#d55}} .medium{{border-color:#c93}}
figure.bad{{opacity:.35;border-color:#888}} figure span{{color:#aaa}}</style></head><body>
<header><h1>Crop review: {len(recs)} frames, {n_check} to check first (red = low, orange = medium confidence)</h1>
<p>Green box = what is saved. Yellow line = detected edge of the picture. Click a frame to mark a bad crop.</p>
<button id="dl">Download list of marked frames</button><span id="n"></span></header><main>{''.join(cards)}</main>
<script>const bad=new Set();document.querySelectorAll('figure').forEach(f=>f.onclick=()=>{{const i=f.dataset.id;
bad.has(i)?bad.delete(i):bad.add(i);f.classList.toggle('bad');document.getElementById('n').textContent=bad.size+' marked';}});
document.getElementById('dl').onclick=()=>{{const b=new Blob([[...bad].join('\\n')+'\\n'],{{type:'text/plain'}});
const a=document.createElement('a');a.href=URL.createObjectURL(b);a.download='bad_crops.txt';a.click();}};</script></body></html>"""
    with open(os.path.join(out_dir, 'crop_review.html'), 'w', encoding='utf-8') as f:
        f.write(html)


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--csv', default=os.path.join(here, 'apollo_surface_frames.csv'), help='frame list (default: next to this script)')
    p.add_argument('--tier', nargs='+', default=['T1'], choices=['T1', 'T2', 'T3', 'EXCLUDE', 'ALL'],
                   help='which tiers to download (default: T1). Ignored when --ids is given')
    p.add_argument('--ids', help='text file of frame IDs to download (one per line), e.g. the keep list from review.html')
    p.add_argument('--skip-ids', help='text file of frame IDs to leave out, e.g. the reject list from review.html')
    p.add_argument('--mission', nargs='+', type=int, choices=[11, 12, 14, 15, 16, 17], help='limit to these missions')
    p.add_argument('--film', choices=['any', 'color', 'bw'], default='any', help='color or black-and-white film only')
    p.add_argument('--res', choices=list(RES_COLUMN), default='raw', help='resolution to download (default: raw)')
    p.add_argument('--crop', action='store_true', help='write only the exposed picture area as a 16-bit TIFF (raw only)')
    p.add_argument('--crop-preview-only', action='store_true', help='with --crop: work out crops and check images, skip raw downloads')
    p.add_argument('--crop-inset', type=float, default=5.0, metavar='PCT',
                   help='with --crop: extra safety trim on every side, in percent of the picture size '
                        '(default 5; 0 gives the tightest crop). Changing it later reuses the saved detection.')
    p.add_argument('--no-check-images', action='store_true', help='with --crop: do not write *.crop-check.jpg files')
    p.add_argument('--out', default='apollo_images', help='output folder (default: ./apollo_images)')
    p.add_argument('--workers', type=int, default=2, help='parallel downloads (default 2; please keep this low)')
    pick = p.add_mutually_exclusive_group()
    pick.add_argument('--limit', type=int, metavar='N', help='only the first N matching frames (useful for a test run)')
    pick.add_argument('--random', type=int, metavar='N',
                      help='N matching frames chosen at random, with no duplicates')
    p.add_argument('--seed', type=int, help='with --random: repeat an earlier random choice (the seed is printed on each run)')
    p.add_argument('--max-gb', type=float, help='stop before starting if the total is larger than this')
    p.add_argument('--dry-run', action='store_true', help='report count and total size, download nothing')
    p.add_argument('--no-size-check', action='store_true', help='skip the HEAD size check (no total, no disk check)')
    p.add_argument('--yes', action='store_true', help='do not ask for confirmation')
    p.add_argument('--rewrite-host', metavar='OLD=NEW', help='replace a URL prefix, e.g. to use a mirror')
    a = p.parse_args()

    if a.limit is not None and a.limit < 1:
        p.error('--limit needs a number of 1 or more')
    if a.random is not None and a.random < 1:
        p.error('--random needs a number of 1 or more')
    if a.seed is not None and a.random is None:
        p.error('--seed goes with --random')

    ac = None
    if a.crop:
        if a.res != 'raw':
            p.error('--crop works on the raw scans; leave --res at raw')
        try:
            sys.path.insert(0, here)
            import apollo_crop as ac  # noqa: F811
        except ImportError as e:
            p.error(f'--crop needs numpy and Pillow (pip install numpy pillow) and apollo_crop.py next to this script: {e}')
    elif a.crop_preview_only:
        p.error('--crop-preview-only goes with --crop')

    with open(a.csv, newline='', encoding='utf-8') as f:
        rows = list(csv.DictReader(f))
    if a.ids:
        wanted = read_ids(a.ids)
        byid = {r['frame_id'].upper(): r for r in rows}
        unknown = [i for i in wanted if i not in byid]
        if unknown:
            log(f'Note: {len(unknown)} IDs not in the CSV were ignored, e.g. {", ".join(unknown[:5])}')
        sel = [byid[i] for i in dict.fromkeys(wanted) if i in byid]
    else:
        tiers = None if 'ALL' in a.tier else set(a.tier)
        sel = [r for r in rows if tiers is None or r['tier'] in tiers]
    if a.skip_ids:
        skip = set(read_ids(a.skip_ids))
        sel = [r for r in sel if r['frame_id'].upper() not in skip]
    if a.mission:
        sel = [r for r in sel if int(r['mission']) in a.mission]
    if a.film != 'any':
        sel = [r for r in sel if r['film'] == a.film]
    col = RES_COLUMN[a.res]
    needed = [col] + (['small_png_url', 'preview_png_url'] if a.crop else [])
    nourl = [r['frame_id'] for r in sel if not all(r[c] for c in needed)]
    sel = [r for r in sel if all(r[c] for c in needed)]
    if nourl:
        log(f'Note: {len(nourl)} frames have no {a.res} file listed and were left out, e.g. {", ".join(nourl[:5])}')
    if not sel:
        log('Nothing matches those filters.')
        return 0
    if a.limit:
        sel = sel[:a.limit]
    if a.random:
        pool = len(sel)
        seed = a.seed if a.seed is not None else random.SystemRandom().randrange(1, 1000000)
        if a.random >= pool:
            log(f'Note: --random {a.random} is not fewer than the {pool} matching frames, so all of them are used.')
        else:
            order = {id(r): i for i, r in enumerate(sel)}
            sel = sorted(random.Random(seed).sample(sel, a.random), key=lambda r: order[id(r)])
    if a.random and len(sel) < pool:
        os.makedirs(a.out, exist_ok=True)
        pick_file = os.path.join(a.out, f'random_{seed}.txt')
        with open(pick_file, 'w', encoding='utf-8') as f:
            f.write(f'# {len(sel)} of {pool} frames chosen at random with --seed {seed}\n')
            f.write('\n'.join(r['frame_id'] for r in sel) + '\n')
        log(f'Chose {len(sel)} of {pool} frames at random (seed {seed}). To repeat or resume this '
            f'choice, run the same command with --seed {seed}. The list is in {pick_file}.')
    if a.rewrite_host:
        old, new = a.rewrite_host.split('=', 1)
        sel = [{k: (v.replace(old, new, 1) if k.endswith('_url') and v else v) for k, v in r.items()} for r in sel]

    jobs = []
    for r in sel:
        url = r[col]
        ext = os.path.splitext(url.split('?')[0])[1] or '.bin'
        suffix = {'preview': '.preview', 'small': '.small', 'lpi': '.lpi'}.get(a.res, '')
        mag_dir = os.path.join(a.out, r['frame_id'].split('-')[0], r['frame_id'].rsplit('-', 1)[0])
        jobs.append({'id': r['frame_id'], 'url': url, 'dir': mag_dir, 'row': r,
                     'dest': os.path.join(mag_dir, r['frame_id'] + suffix + ext), 'size': None})

    mode = 'raw, cropped to the exposed frame' if a.crop else a.res
    if a.crop_preview_only:
        mode = 'crop detection only (about 20 MB of previews per frame)'
    log(f'{len(jobs)} frames selected, resolution: {mode}')
    if not check_connection(jobs[0]['url']):
        return 1
    if not a.no_size_check and not a.crop_preview_only:
        log('Checking file sizes on the server ...')
        done = 0
        try:
            with ThreadPoolExecutor(max_workers=max(1, min(4, a.workers * 2))) as ex:
                futs = {ex.submit(head_size, j['url']): j for j in jobs}
                for fu in as_completed(futs):
                    futs[fu]['size'] = fu.result()
                    done += 1
                    if done % 100 == 0:
                        log(f'  {done}/{len(jobs)}')
        except CertificateError:
            log(CERT_HELP)
            return 1
        missing = [j['id'] for j in jobs if j['size'] == -1]
        if missing:
            log(f'Note: {len(missing)} files are missing on the server and will be skipped, e.g. {", ".join(missing[:5])}')
        jobs = [j for j in jobs if j['size'] != -1]
        known = [j['size'] for j in jobs if j['size']]
        if not known:
            log('Stopping: the server did not report the size of any file, so nothing would download. '
                'Check your connection and try again, or add --no-size-check to skip this step.')
            return 1
        total = sum(known)
        os.makedirs(a.out, exist_ok=True)
        free = shutil.disk_usage(a.out).free
        if a.crop:
            # measured on full-resolution scans with no trim: the picture spans about 86% (B&W) or
            # 66% (color) of a raw scan's rows, and about 63% (B&W) or 49% (color) of its pixels
            t = max(0.0, 1 - 2 * a.crop_inset / 100.0)
            rows_f = {'bw': 0.865, 'color': 0.659}
            area_f = {'bw': 0.630, 'color': 0.487}
            film = {j['id']: (j['row']['film'] if j['row']['film'] in rows_f else 'bw') for j in jobs}
            dl = sum(rows_f[film[j['id']]] * t * (j['size'] or 0) for j in jobs) + 20e6 * len(jobs)
            out_size = sum(area_f[film[j['id']]] * t * t * (j['size'] or 0) for j in jobs)
            log(f'Raw scans total {human(total)}. With cropping and a {a.crop_inset:g}% trim, expect to '
                f'download about {human(dl)} and keep about {human(out_size)} of TIFFs (estimates).')
            need = out_size + a.workers * 1.4e9
        else:
            have = sum(os.path.getsize(j['dest']) for j in jobs if os.path.exists(j['dest']))
            log(f'Total size: {human(total)} for {len(jobs)} files'
                + (f' ({len(jobs) - len(known)} sizes unknown)' if len(known) < len(jobs) else ''))
            log(f'Already on disk: {human(have)}.')
            need = total - have
        log(f'Free space in {os.path.abspath(a.out)}: {human(free)}')
        if a.max_gb is not None and total > a.max_gb * 1e9:
            log(f'Stopping: total is larger than --max-gb {a.max_gb}.')
            return 1
        if need > free:
            log('Stopping: not enough free disk space. Choose another --out folder, a smaller --res, or fewer frames.')
            return 1
    if a.dry_run:
        for j in jobs[:10]:
            log(f'  {j["id"]}  {human(j["size"]) if j["size"] else "?"}  ->  {j["dest"] if not a.crop else os.path.join(j["dir"], j["id"] + ".crop.tif")}')
        if len(jobs) > 10:
            log(f'  ... and {len(jobs) - 10} more')
        log('Dry run only; nothing downloaded.')
        return 0
    if not a.yes:
        ans = input('Start? [y/N] ').strip().lower()
        if ans not in ('y', 'yes'):
            log('Cancelled.')
            return 0

    os.makedirs(a.out, exist_ok=True)
    manifest = os.path.join(a.out, 'manifest.csv')
    new = not os.path.exists(manifest)
    mf = open(manifest, 'a', newline='', encoding='utf-8')
    mw = csv.writer(mf)
    if new:
        mw.writerow(['frame_id', 'resolution', 'status', 'bytes', 'file', 'url', 'time',
                     'crop_x0', 'crop_y0', 'crop_x1', 'crop_y1', 'crop_confidence', 'crop_edges'])
    counts = {}
    review = []
    t0 = time.time()

    def work(j):
        try:
            if a.crop:
                return crop_frame(j['row'], j['dir'], a, ac)
            s, n = download(j['url'], j['dest'], j['size'])
            return s, n, None
        except CertificateError:
            return 'failed: security certificate could not be verified (see the note at the start)', 0, None
        except Exception as e:
            return f'failed: {e}', 0, None

    with ThreadPoolExecutor(max_workers=max(1, a.workers)) as ex:
        futs = {ex.submit(work, j): j for j in jobs}
        try:
            for k, fu in enumerate(as_completed(futs), 1):
                j = futs[fu]
                status, nbytes, meta = fu.result()
                key = status.split(' (')[0].split(':')[0]
                counts[key] = counts.get(key, 0) + 1
                extra = ''
                crop = ['', '', '', '']
                conf = edges = ''
                if meta:
                    crop = meta['crop']
                    conf = meta['info']['confidence']
                    edges = '/'.join(meta['info']['sides'])
                    extra = f", crop {crop[2] - crop[0]}x{crop[3] - crop[1]} px, {conf} confidence"
                    if conf != 'high':
                        review.append(j['id'])
                log(f'[{k}/{len(jobs)}] {j["id"]}: {status}{extra}' + (f', {human(nbytes)}' if nbytes else ''))
                with _print_lock:
                    f_out = os.path.join(j['dir'], j['id'] + '.crop.tif') if a.crop else j['dest']
                    mw.writerow([j['id'], 'raw-crop' if a.crop else a.res, status, nbytes, f_out, j['url'],
                                 time.strftime('%Y-%m-%d %H:%M:%S'), *crop, conf, edges])
                    mf.flush()
        except KeyboardInterrupt:
            log('\nInterrupted. Partial downloads are kept and will resume next time.')
            try:
                ex.shutdown(wait=False, cancel_futures=True)
            except TypeError:                      # Python 3.8 has no cancel_futures
                ex.shutdown(wait=False)
            mf.close()
            return 130
    mf.close()
    if a.crop:
        write_crops_csv(a.out)
    log(f'Finished in {time.time() - t0:,.0f} s: ' + ', '.join(f'{v} {k}' for k, v in counts.items()))
    log(f'Manifest: {manifest}')
    if a.crop:
        log(f'Crop review page: {os.path.join(a.out, "crop_review.html")} (open it in a browser).')
    if review:
        path = os.path.join(a.out, 'crops_to_check.txt')
        with open(path, 'w') as f:
            f.write('\n'.join(review) + '\n')
        log(f'{len(review)} crop{" is" if len(review) == 1 else "s are"} worth a look '
            f'(listed first on the review page and in {path}).')
    return 0 if not any(k.startswith('failed') for k in counts) else 2


if __name__ == '__main__':
    sys.exit(main())
