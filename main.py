# -*- coding: utf-8 -*-
"""
script.dvhdr.labels.diagnostic -- on-screen overlay for verifying the Dolby
Vision and HDR metadata Kodi core publishes via
Player.Process(video.sidedata), parsed by script.module.sidedata.

Result shape and field semantics are documented in that module's README and
lib/sidedata/__init__.py; absence is signalled by emptiness (shown here as
"-"), which also covers a build without the feature. Row values come from
the parsed result via compute_row_values()/trim_rows()/hdr10plus_rows(),
pure functions that never touch xbmc, so they're host-testable;
parse_sidedata() only re-runs when the raw sidedata string changes between
polls. If the module import fails, the module row shows "missing" and every
parsed row falls back to "-" instead of taking the script down.

Read-only test tooling: no network, no settings, no filesystem writes.
"""

import os
import sys
import time
import traceback

import xbmc
import xbmcaddon
import xbmcgui

try:
    from sidedata import parse_sidedata as _parse_sidedata
    SIDEDATA_IMPORT_ERROR = None
except Exception as exc:  # never let a missing/broken module install kill the script
    _parse_sidedata = None
    SIDEDATA_IMPORT_ERROR = exc

_EMPTY_SIDEDATA = {'flags': [], 'structure': None, 'config': None, 'rpu': None,
                   'hdr10plus': None, 'mdcv': None, 'cll': None}


def parse_sidedata(json_str):
    if _parse_sidedata is None:
        return _EMPTY_SIDEDATA
    return _parse_sidedata(json_str)


ADDON_ID = "script.dvhdr.labels.diagnostic"
ADDON_VERSION = "3.0.3"  # keep in sync with addon.xml
SIDEDATA_MODULE_ID = "script.module.sidedata"
LOG_PREFIX = "[script.dvhdr.labels.diagnostic] "

# running flag on the home window: every ExecuteAddon spawns a fresh Python
# invocation, and without the guard each call stacks another WindowDialog.
# cleared in run_overlay()'s finally block so a crash still releases it
RUNNING_PROPERTY = ADDON_ID + ".running"

# a second invocation sets this instead of stacking, so RunScript acts as a
# show/hide toggle. cleared on startup so a stale request from a crashed
# instance cannot instantly close a fresh one
CLOSE_PROPERTY = ADDON_ID + ".closerequest"

POLL_INTERVAL = 0.05
HIGHLIGHT_SECS = 0.6

# ------------------------------------------------------------ label sections
# rows are (key, short name shown on screen). keys starting with
# "VideoPlayer." are read directly as infolabel expressions; every other key
# is looked up in the dict compute_row_values() returns each refresh
SECTIONS = (
    ("DV identity (static)  +  L1 nits (per-frame)", (
        ("VideoPlayer.HdrType", "HdrType"),
        ("VideoPlayer.HdrDetail", "HdrDetail"),
        ("module.version", "module.version"),
        ("dovi.profile", "dovi.profile"),
        ("dovi.level", "dovi.level"),
        ("dovi.version", "dovi.version"),
        ("dovi.el.type", "dovi.el.type"),
        ("dovi.rpu.present", "dovi.rpu.present"),
        ("dovi.bl.present", "dovi.bl.present"),
        ("dovi.el.present", "dovi.el.present"),
        ("dovi.meta.version", "dovi.meta.version"),
        ("flags", "flags"),
        ("structure", "structure"),
        ("dovi.vdr.bitdepth", "dovi.vdr.bitdepth"),
        ("dovi.l1.min.pq", "dovi.l1.min.pq"),
        ("dovi.l1.min.nits", "dovi.l1.min.nits"),
        ("dovi.l1.max.pq", "dovi.l1.max.pq"),
        ("dovi.l1.max.nits", "dovi.l1.max.nits"),
        ("dovi.l1.avg.pq", "dovi.l1.avg.pq"),
        ("dovi.l1.avg.nits", "dovi.l1.avg.nits"),
    )),
    ("DV source / L3 / L5 (per-frame)  +  L6 / HDR10 / L9 / L11 (static)", (
        # source min/max are zeroed by the bitstream on compressed frames,
        # in which case they render empty
        ("dovi.source.min.pq", "dovi.source.min.pq"),
        ("dovi.source.min.nits", "dovi.source.min.nits"),
        ("dovi.source.max.pq", "dovi.source.max.pq"),
        ("dovi.source.max.nits", "dovi.source.max.nits"),
        ("dovi.l3.mid", "dovi.l3.mid"),
        ("dovi.l5.left.offset", "dovi.l5.left.offset"),
        ("dovi.l5.right.offset", "dovi.l5.right.offset"),
        ("dovi.l5.top.offset", "dovi.l5.top.offset"),
        ("dovi.l5.bottom.offset", "dovi.l5.bottom.offset"),
        ("dovi.l6.max.cll", "dovi.l6.max.cll"),
        ("dovi.l6.max.fall", "dovi.l6.max.fall"),
        ("dovi.l6.min.lum", "dovi.l6.min.lum"),
        ("dovi.l6.max.lum", "dovi.l6.max.lum"),
        ("hdr.max.cll", "hdr.max.cll"),
        ("hdr.max.fall", "hdr.max.fall"),
        ("hdr.min.lum", "hdr.min.lum"),
        ("hdr.max.lum", "hdr.max.lum"),
        ("dovi.l9.primaries", "dovi.l9.primaries"),
        ("dovi.l11.type", "dovi.l11.type"),
        ("dovi.l11.whitepoint", "dovi.l11.whitepoint"),
        ("dovi.l11.refmode", "dovi.l11.refmode"),
    )),
)

ROWS_PER_SECTION = max(len(rows) for _, rows in SECTIONS)


# ---------------------------------------------------------- value rendering
# pure functions: parsed sidedata dict -> row strings. No xbmc calls, so
# these are host-testable directly.

def fmt_number(value):
    """Mirrors the reference AMLFormatMetadataNumber: four decimals below 1,
    whole numbers at or above 1, no unit suffix."""
    if value != 0 and abs(value) < 1.0:
        return "%.4f" % value
    return str(int(round(value)))


def _bool01(value):
    return "1" if value else "0"


def compute_row_values(parsed, module_version):
    """parsed is a sidedata.parse_sidedata() result. Returns a dict keyed by
    the same short names used in SECTIONS (minus the VideoPlayer.* rows,
    which are read live)."""
    rpu = parsed.get('rpu')
    config = parsed.get('config')
    mdcv = parsed.get('mdcv')
    cll = parsed.get('cll')
    values = {'module.version': module_version or ""}

    header = rpu['header'] if rpu else None

    # profile is container-level truth (the dvcC/dvvC config record), not
    # the RPU's guessed_profile: a profile 10 stream carries a profile
    # 8-shaped RPU, so the RPU alone can't tell profile 10 from 8. Only
    # fall back to the RPU guess, plain with no compat digit, when there's
    # no config record at all to read the carriage-level profile from.
    profile = rpu['profile'] if rpu else None
    if config:
        values['dovi.profile'] = "%d.%d" % (config['profile'], config['compat_id'])
    elif profile is not None:
        values['dovi.profile'] = str(profile)
    else:
        values['dovi.profile'] = ""
    values['dovi.el.type'] = header['el_type'] if header and header['el_type'] else ""

    if config:
        values['dovi.level'] = str(config['level']) if config['level'] > 0 else ""
        values['dovi.version'] = "%d.%d" % (config['version_major'], config['version_minor'])
        values['dovi.rpu.present'] = _bool01(config['rpu_present'])
        values['dovi.bl.present'] = _bool01(config['bl_present'])
        values['dovi.el.present'] = _bool01(config['el_present'])
    else:
        values['dovi.level'] = ""
        values['dovi.version'] = ""
        values['dovi.rpu.present'] = ""
        values['dovi.bl.present'] = ""
        values['dovi.el.present'] = ""

    values['dovi.meta.version'] = rpu['cm_version'] if rpu and rpu['cm_version'] else ""
    values['flags'] = " ".join(parsed.get('flags') or [])
    values['structure'] = parsed.get('structure') or ""

    vdr_bit_depth = header['vdr_bit_depth'] if header else None
    values['dovi.vdr.bitdepth'] = str(vdr_bit_depth) if vdr_bit_depth is not None else ""

    l1 = rpu['l1'] if rpu else None
    if l1:
        values['dovi.l1.min.pq'] = str(l1['min_pq'])
        values['dovi.l1.min.nits'] = fmt_number(l1['min_nits'])
        values['dovi.l1.max.pq'] = str(l1['max_pq'])
        values['dovi.l1.max.nits'] = fmt_number(l1['max_nits'])
        values['dovi.l1.avg.pq'] = str(l1['avg_pq'])
        values['dovi.l1.avg.nits'] = fmt_number(l1['avg_nits'])
    else:
        for key in ('dovi.l1.min.pq', 'dovi.l1.min.nits', 'dovi.l1.max.pq',
                    'dovi.l1.max.nits', 'dovi.l1.avg.pq', 'dovi.l1.avg.nits'):
            values[key] = ""

    source = rpu['source'] if rpu else None
    if source:
        values['dovi.source.min.pq'] = str(source['min_pq'])
        values['dovi.source.min.nits'] = fmt_number(source['min_nits'])
        values['dovi.source.max.pq'] = str(source['max_pq'])
        values['dovi.source.max.nits'] = fmt_number(source['max_nits'])
    else:
        for key in ('dovi.source.min.pq', 'dovi.source.min.nits',
                    'dovi.source.max.pq', 'dovi.source.max.nits'):
            values[key] = ""

    l3 = rpu['l3'] if rpu else None
    values['dovi.l3.mid'] = str(l3['avg_pq_offset']) if l3 else ""

    l5 = rpu['l5'] if rpu else None
    if l5:
        values['dovi.l5.left.offset'] = str(l5['left'])
        values['dovi.l5.right.offset'] = str(l5['right'])
        values['dovi.l5.top.offset'] = str(l5['top'])
        values['dovi.l5.bottom.offset'] = str(l5['bottom'])
    else:
        for key in ('dovi.l5.left.offset', 'dovi.l5.right.offset',
                    'dovi.l5.top.offset', 'dovi.l5.bottom.offset'):
            values[key] = ""

    l6 = rpu['l6'] if rpu else None
    if l6:
        values['dovi.l6.max.cll'] = str(l6['max_cll'])
        values['dovi.l6.max.fall'] = str(l6['max_fall'])
        values['dovi.l6.min.lum'] = fmt_number(l6['min_lum_nits'])
        values['dovi.l6.max.lum'] = fmt_number(l6['max_lum_nits'])
    else:
        for key in ('dovi.l6.max.cll', 'dovi.l6.max.fall',
                    'dovi.l6.min.lum', 'dovi.l6.max.lum'):
            values[key] = ""

    if cll:
        values['hdr.max.cll'] = str(cll['max_cll'])
        values['hdr.max.fall'] = str(cll['max_fall'])
    else:
        values['hdr.max.cll'] = ""
        values['hdr.max.fall'] = ""

    if mdcv:
        values['hdr.min.lum'] = fmt_number(mdcv['min_luminance'])
        values['hdr.max.lum'] = fmt_number(mdcv['max_luminance'])
    else:
        values['hdr.min.lum'] = ""
        values['hdr.max.lum'] = ""

    l9 = rpu['l9'] if rpu else None
    values['dovi.l9.primaries'] = l9['name'] if l9 else ""

    l11 = rpu['l11'] if rpu else None
    if l11:
        values['dovi.l11.type'] = l11['content_type_name']
        values['dovi.l11.whitepoint'] = l11['whitepoint_name']
        values['dovi.l11.refmode'] = _bool01(l11['reference_mode'])
    else:
        values['dovi.l11.type'] = ""
        values['dovi.l11.whitepoint'] = ""
        values['dovi.l11.refmode'] = ""

    return values

# ---------------------------------------------------------------- trim block
# Targets come from rpu['l2']/rpu['l8'], each already resolved to nits and
# carrying its raw codes plus the inverted 'ui' scale. One raw row and one
# .ui row per target, condensed short keys.
TRIM_SECTIONS = (("L2 trims (per-frame)", "l2"), ("L8 trims (per-frame)", "l8"))
TRIM_RAW_CONTROLS = (("s", "slope"), ("o", "offset"), ("p", "power"),
                     ("cw", "chromaweight"), ("sg", "saturation"),
                     ("td", "tonedetail"))
TRIM_RAW_CONTROLS_L8 = TRIM_RAW_CONTROLS + (("mc", "midcontrastbias"),
                                            ("hc", "highlightclipping"))
TRIM_UI_CONTROLS = (("g", "gain"), ("l", "lift"), ("gm", "gamma"),
                    ("cw", "chromaweight"), ("sg", "saturation"),
                    ("td", "tonedetail"))
# raw control names that don't match a trim dict key directly
_TRIM_RAW_KEY_ALIASES = {'midcontrastbias': 'mid_contrast',
                         'highlightclipping': 'clip_trim'}
# row budget per level: presence row + 2 rows per shown target, more targets
# collapse into a "+N more" tail row
TRIM_MAX_TARGETS = 4
TRIM_DETAIL_ROWS = TRIM_MAX_TARGETS * 2
TRIM_ROWS_TOTAL = 1 + TRIM_DETAIL_ROWS + 1


def _trim_raw_value(trim, name):
    val = trim.get(_TRIM_RAW_KEY_ALIASES.get(name, name))
    return str(val) if val is not None else ""


def _trim_ui_value(trim, name):
    val = trim['ui'].get(name)
    return "%.4f" % val if val is not None else ""


def trim_rows(level, trims, l10_targets=None):
    """Composed display rows for one level: (slot key, text) pairs. trims is
    rpu['l2'] or rpu['l8']; l10_targets is rpu['l10'] (l8 only)."""
    header = "targets: %s" % (" ".join(str(t['nits']) for t in trims) or "-")
    if level == "l8":
        l10_str = " ".join("%d (%s)" % (t['nits'], t['primary_name'])
                           for t in (l10_targets or []))
        header += "  l10: %s" % (l10_str or "-")
    rows = [("%s.trims" % level, header)]
    shown = trims[:TRIM_MAX_TARGETS]
    controls = TRIM_RAW_CONTROLS_L8 if level == "l8" else TRIM_RAW_CONTROLS
    for trim in shown:
        raw = ["%s%s" % (key, _trim_raw_value(trim, name) or "-")
              for key, name in controls]
        ui = ["%s%s" % (key, _trim_ui_value(trim, name) or "-")
             for key, name in TRIM_UI_CONTROLS]
        rows.append(("%s.%d.raw" % (level, trim['nits']),
                     "%d  %s" % (trim['nits'], " ".join(raw))))
        rows.append(("%s.%d.ui" % (level, trim['nits']),
                     "     ui  %s" % " ".join(ui)))
    if len(trims) > len(shown):
        rows.append(("%s.more" % level, "+%d more targets"
                     % (len(trims) - len(shown))))
    return rows

# ------------------------------------------------------------- hdr10+ block
# Single block, not per-level like trim; blank entirely when parsed['hdr10plus']
# is None (no HDR10+ metadata on this content or frame). Values join onto
# compact rows the same way trim_rows() composes targets, to keep the many
# percentile fields inside a small row budget.
HDR10PLUS_TITLE = "HDR10+ (per-frame)"
HDR10PLUS_PERCENTILES = (1, 5, 10, 25, 50, 75, 90, 95, 99)
HDR10PLUS_ROWS_TOTAL = 3


def hdr10plus_rows(hdr10plus):
    """Composed display rows for the HDR10+ block: (slot key, text) pairs.
    hdr10plus is parsed['hdr10plus']."""
    if not hdr10plus:
        return []
    maxscl = hdr10plus['maxscl']
    rows = [("hdr10plus.id",
             "profile %s  app %s  win %s  tgt.nits %s  maxscl %s  r %s  g %s  b %s" % (
        hdr10plus['profile'],
        hdr10plus['application_version'],
        hdr10plus['num_windows'],
        hdr10plus['targeted_system_display_maximum_luminance'],
        fmt_number(max(maxscl)),
        fmt_number(maxscl[0]),
        fmt_number(maxscl[1]),
        fmt_number(maxscl[2])))]
    is_profile_b = hdr10plus['profile'] == 'B'
    knee_x = "%.3f" % hdr10plus['knee_point_x'] if is_profile_b else "-"
    knee_y = "%.3f" % hdr10plus['knee_point_y'] if is_profile_b else "-"
    anchors = str(len(hdr10plus['bezier_anchors'])) if is_profile_b else "-"
    rows.append(("hdr10plus.tone",
                 "avg.maxrgb %s  frac.bright %s  knee.x %s  knee.y %s  bezier.anchors %s" % (
        fmt_number(hdr10plus['average_maxrgb']),
        "%.1f" % hdr10plus['fraction_bright_pixels'],
        knee_x, knee_y, anchors)))
    dist_by_pct = {d['percentage']: d['nits'] for d in hdr10plus['distribution']}
    dist = ["%d:%s" % (p, fmt_number(dist_by_pct[p]) if p in dist_by_pct else "-")
           for p in HDR10PLUS_PERCENTILES]
    rows.append(("hdr10plus.dist", "dist  %s" % " ".join(dist)))
    return rows

# action ids from xbmc/input/actions/ActionIDs.h
ACT_PREVIOUS_MENU = 10
ACT_NAV_BACK = 92

CLOSE = (ACT_PREVIOUS_MENU, ACT_NAV_BACK)

# ------------------------------------------------------- input pass-through
# WindowDialog is hard-modal, so keys translate through the <global> keymap
# context instead of <FullscreenVideo>. Forward ONLY the ids whose keymap
# translation that changes AND that nothing handles natively; transport keys,
# digits, volume etc. already reach the player through CApplication's global
# fallthrough, and forwarding them double-fires (v1.0.2 pause bug).
# Untargeted forwards echo back into this dialog; safe because no forwarded
# action's translated id is itself a forwarded key.

FORWARD_TO_APP = {
    1: "stepback",                  # MOVE_LEFT   -> left seek
    2: "stepforward",               # MOVE_RIGHT  -> right seek
    3: "chapterorbigstepforward",   # MOVE_UP     -> chapter/big step +
    4: "chapterorbigstepback",      # MOVE_DOWN   -> chapter/big step -
    5: "skipnext",                  # PAGE_UP     -> next item
    6: "skipprevious",              # PAGE_DOWN   -> previous item
}

# actions only CGUIWindowFullScreen handles; targeted sends never re-enter
# this dialog
FORWARD_TO_WINDOW = {
    7: "osd",     # SELECT_ITEM  -- OK opens the video OSD, as in fullscreen
    117: "osd",   # CONTEXT_MENU -- remote menu/title key, global context
    163: "osd",   # MENU         -- keyboard "m", global context
}

BG_IMAGE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "resources", "panel.png")


def read(expression):
    try:
        return xbmc.getInfoLabel(expression) or ""
    except Exception as exc:  # never let one bad read kill the loop
        log_error("read(%s) failed: %s" % (expression, exc))
        return "?"


def log(msg, level=xbmc.LOGINFO):
    xbmc.log(LOG_PREFIX + msg, level)


def log_error(msg):
    log(msg, xbmc.LOGERROR)


def player_state():
    if xbmc.getCondVisibility("Player.Paused"):
        return "paused"
    if xbmc.getCondVisibility("Player.Playing"):
        return "playing"
    if xbmc.getCondVisibility("Player.HasMedia"):
        return "media"
    return "idle"


def module_version():
    if SIDEDATA_IMPORT_ERROR is not None:
        log_error("sidedata module unavailable: %s" % SIDEDATA_IMPORT_ERROR)
        return "missing"
    try:
        return xbmcaddon.Addon(SIDEDATA_MODULE_ID).getAddonInfo("version")
    except Exception as exc:
        log_error("module_version() failed: %s" % exc)
        return "?"


class DoViLabelOverlay(xbmcgui.WindowDialog):
    """Borderless overlay; renders above fullscreen video without pausing it."""

    # no super().__init__() on purpose: the C++ side is built in __new__ and
    # some Kodi builds warn when a Window subclass chains __init__
    def __init__(self):
        self.tick = 0
        self.closed = False
        self.previous = {}
        self.changed_at = {}
        self.rows = []  # flat, section-major: section 0's rows, then section 1's
        self.module_version = module_version()
        self._last_raw = None  # forces a parse on the first refresh
        self._last_parsed = None
        self._build()

    def _build(self):
        try:
            width, height = self.getWidth(), self.getHeight()
        except Exception:
            width, height = 1280, 720  # pre-Estuary skin coordinate fallback

        # condensed since 2.2.0/2.4.0: trim and hdr10plus blocks add rows.
        # font10 (smallest named skin font, ~23px on Estuary) is the floor
        # this line height must clear, so 44 is as tight as the row count
        # allows without the glyphs overrunning their row again
        line_h = max(14, int(height / 44.0))
        mx, my = int(width * 0.03), int(height * 0.04)
        pad = int(width * 0.015)
        gap = int(width * 0.02)
        pw = width - 2 * mx

        title_block = int(line_h * 1.9)
        section_block = int(line_h * 1.35)
        footer_block = int(line_h * 1.8)
        # size the panel to the content: it sits on top of the video the
        # user is trying to look at
        ph = (int(line_h * 0.35) + title_block + section_block
              + ROWS_PER_SECTION * line_h + int(line_h * 0.4) + section_block
              + TRIM_ROWS_TOTAL * line_h + int(line_h * 0.4) + section_block
              + HDR10PLUS_ROWS_TOTAL * line_h + footer_block)

        col_w = (pw - 2 * pad - gap) // 2
        name_w = int(col_w * 0.62)
        val_w = col_w - name_w
        title_y = my + int(line_h * 0.35)
        section_y = title_y + title_block
        rows_y = section_y + section_block

        controls = []
        try:
            controls.append(xbmcgui.ControlImage(mx, my, pw, ph, BG_IMAGE,
                                                 colorDiffuse="E4000000"))
        except Exception as exc:  # cosmetic; labels still work
            log("panel image unavailable: %s" % exc, xbmc.LOGWARNING)

        self.title = xbmcgui.ControlLabel(mx + pad, title_y, pw - 2 * pad,
                                          line_h, "", font="font10",
                                          textColor="FFFFD060")
        controls.append(self.title)

        for index, (section_name, section_rows) in enumerate(SECTIONS):
            cx = mx + pad + index * (col_w + gap)
            controls.append(xbmcgui.ControlLabel(
                cx, section_y, col_w, line_h, section_name, font="font10",
                textColor="FFFFD060"))
            for row in range(ROWS_PER_SECTION):
                y = rows_y + row * line_h
                name = xbmcgui.ControlLabel(cx, y, name_w, line_h, "",
                                            font="font10",
                                            textColor="FFE0E8F0")
                value = xbmcgui.ControlLabel(cx + name_w, y, val_w, line_h, "",
                                             font="font10",
                                             textColor="FFFFFFFF")
                # rows past the end of a short section stay blank
                self.rows.append((name, value,
                                  section_rows[row] if row < len(section_rows)
                                  else None))
                controls.extend((name, value))

        # trim block: two columns of composed full-width rows, filled
        # dynamically from the presence labels each refresh
        trim_y = rows_y + ROWS_PER_SECTION * line_h + int(line_h * 0.4)
        self.trim_labels = []
        for index, (trim_title, _) in enumerate(TRIM_SECTIONS):
            cx = mx + pad + index * (col_w + gap)
            controls.append(xbmcgui.ControlLabel(
                cx, trim_y, col_w, line_h, trim_title, font="font10",
                textColor="FFFFD060"))
            column = []
            for row in range(TRIM_ROWS_TOTAL):
                y = trim_y + section_block + row * line_h
                label = xbmcgui.ControlLabel(cx, y, col_w, line_h, "",
                                             font="font10",
                                             textColor="FFFFFFFF")
                column.append(label)
                controls.append(label)
            self.trim_labels.append(column)

        # HDR10+ block: single full-width column of composed rows, filled
        # dynamically each refresh; title and rows stay blank together when
        # the block has nothing to show
        hdr10plus_y = trim_y + section_block + TRIM_ROWS_TOTAL * line_h + int(line_h * 0.4)
        self.hdr10plus_title = xbmcgui.ControlLabel(
            mx + pad, hdr10plus_y, pw - 2 * pad, line_h, "", font="font10",
            textColor="FFFFD060")
        controls.append(self.hdr10plus_title)
        self.hdr10plus_labels = []
        for row in range(HDR10PLUS_ROWS_TOTAL):
            y = hdr10plus_y + section_block + row * line_h
            label = xbmcgui.ControlLabel(mx + pad, y, pw - 2 * pad, line_h, "",
                                         font="font10", textColor="FFFFFFFF")
            self.hdr10plus_labels.append(label)
            controls.append(label)

        self.footer = xbmcgui.ControlLabel(
            mx + pad, my + ph - int(line_h * 1.3), pw - 2 * pad, line_h,
            "Back or re-run add-on: close   |   all other keys act on the video",
            font="font10", textColor="FF8090A0")
        controls.append(self.footer)

        self.addControls(controls)

    # ---------------------------------------------------------------- refresh
    def refresh(self):
        self.tick += 1
        self.title.setLabel("DV / HDR infolabels     tick %d     %s"
                            % (self.tick, player_state()))

        now = time.monotonic()
        raw = read("Player.Process(video.sidedata)")
        if raw != self._last_raw:
            self._last_parsed = parse_sidedata(raw)
            self._last_raw = raw
        parsed = self._last_parsed
        computed = compute_row_values(parsed, self.module_version)

        for name_ctl, value_ctl, spec in self.rows:
            if spec is None:
                name_ctl.setLabel("")
                value_ctl.setLabel("")
                continue

            row_key, shown_name = spec
            if row_key.startswith("VideoPlayer."):
                value = read(row_key)
            else:
                value = computed.get(row_key, "")
            shown = value if value != "" else "-"
            if self.previous.get(row_key, value) != value:
                self.changed_at[row_key] = now
            if now - self.changed_at.get(row_key, -HIGHLIGHT_SECS) < HIGHLIGHT_SECS:
                shown = "[COLOR FF60FF60]%s[/COLOR]" % shown
            self.previous[row_key] = value
            name_ctl.setLabel(shown_name)
            value_ctl.setLabel(shown)

        rpu = parsed.get('rpu')
        l2_trims = rpu['l2'] if rpu else []
        l8_trims = rpu['l8'] if rpu else []
        l10_targets = rpu['l10'] if rpu else []
        for (_, level), column in zip(TRIM_SECTIONS, self.trim_labels):
            trims = l8_trims if level == "l8" else l2_trims
            composed = trim_rows(level, trims, l10_targets if level == "l8" else None)
            for slot, label in enumerate(column):
                if slot >= len(composed):
                    label.setLabel("")
                    continue
                key, text = composed[slot]
                shown = text
                if self.previous.get(key, text) != text:
                    self.changed_at[key] = now
                if now - self.changed_at.get(key, -HIGHLIGHT_SECS) < HIGHLIGHT_SECS:
                    shown = "[COLOR FF60FF60]%s[/COLOR]" % shown
                self.previous[key] = text
                label.setLabel(shown)

        composed = hdr10plus_rows(parsed.get('hdr10plus'))
        self.hdr10plus_title.setLabel(HDR10PLUS_TITLE if composed else "")
        for slot, label in enumerate(self.hdr10plus_labels):
            if slot >= len(composed):
                label.setLabel("")
                continue
            key, text = composed[slot]
            shown = text
            if self.previous.get(key, text) != text:
                self.changed_at[key] = now
            if now - self.changed_at.get(key, -HIGHLIGHT_SECS) < HIGHLIGHT_SECS:
                shown = "[COLOR FF60FF60]%s[/COLOR]" % shown
            self.previous[key] = text
            label.setLabel(shown)

    # ----------------------------------------------------------------- input
    def onAction(self, action):
        try:
            action_id = action.getId()
        except Exception:
            return
        if action_id in CLOSE:
            self.closed = True
            self.close()
            return

        name = FORWARD_TO_APP.get(action_id)
        if name is not None:
            xbmc.executebuiltin("Action(%s)" % name)
            return

        name = FORWARD_TO_WINDOW.get(action_id)
        if name is not None:
            xbmc.executebuiltin("Action(%s,fullscreenvideo)" % name)
            return
        # every other id already reached its handler through CApplication's
        # global fallthrough before this callback ran; forwarding here would
        # double-fire

    def onControl(self, control):
        pass


# ------------------------------------------------------------------- overlay

def run_overlay():
    home = xbmcgui.Window(10000)
    if home.getProperty(RUNNING_PROPERTY):
        # toggle: ask the open overlay to close instead of stacking
        log("already running (property %s set) -- requesting close (toggle)"
            % RUNNING_PROPERTY)
        home.setProperty(CLOSE_PROPERTY, "1")
        return
    home.clearProperty(CLOSE_PROPERTY)
    home.setProperty(RUNNING_PROPERTY, "1")

    window = None
    try:
        window = DoViLabelOverlay()
        window.show()
        monitor = xbmc.Monitor()
        log("overlay open (%d labels in %d sections, single page)"
            % (sum(len(rows) for _, rows in SECTIONS), len(SECTIONS)))
        while not window.closed:
            if home.getProperty(CLOSE_PROPERTY):
                log("close requested by a second invocation (toggle)")
                break
            try:
                window.refresh()
            except Exception:
                log_error("refresh failed: %s" % traceback.format_exc())
            if monitor.waitForAbort(POLL_INTERVAL):  # clean shutdown on Kodi exit
                break
    except Exception:
        log_error("fatal: %s" % traceback.format_exc())
    finally:
        if window is not None:
            try:
                window.close()
            except Exception:
                pass
            del window  # release the C++ window object
        home.clearProperty(CLOSE_PROPERTY)
        home.clearProperty(RUNNING_PROPERTY)
        log("overlay closed")


def main(argv=None):
    argv = list(sys.argv) if argv is None else list(argv)
    if len(argv) > 1:
        log("ignoring unknown params %r (diag mode was removed in 2.0.0)"
            % (argv[1:],), xbmc.LOGWARNING)
    run_overlay()


if __name__ == "__main__":
    main()
