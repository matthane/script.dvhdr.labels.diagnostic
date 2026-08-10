# -*- coding: utf-8 -*-
"""
script.dvhdr.labels.diagnostic -- on-screen overlay for verifying the Dolby
Vision and HDR infolabels registered by the CoreELEC label registry
(branch ce-label-registry).

Label semantics are documented in xbmc/guilib/guiinfo/CEGUIInfoLabels.dox;
absence is signalled by emptiness (shown here as "-"), which also covers a
build without the feature.

2.2.0 adds the trim block: targets enumerated live from the l2.trims /
l8.trims presence labels, then every control queried per target through the
parameterized trim labels (raw codes plus the .ui row). Spacing is condensed
to fit; density beats aesthetics in a diagnostic overlay.

Read-only test tooling: no network, no settings, no filesystem writes.
"""

import os
import sys
import time
import traceback

import xbmc
import xbmcgui

ADDON_ID = "script.dvhdr.labels.diagnostic"
ADDON_VERSION = "2.2.0"  # keep in sync with addon.xml
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
# rows are (infolabel expression, short name shown on screen)
SECTIONS = (
    ("DV identity (static)  +  source / L1 nits (per-frame)", (
        ("VideoPlayer.HdrType", "HdrType"),
        ("VideoPlayer.HdrDetail", "HdrDetail"),
        ("Player.Process(video.dovi.apiversion)", "dovi.apiversion"),
        ("Player.Process(video.dovi.profile)", "dovi.profile"),
        ("Player.Process(video.dovi.el.type)", "dovi.el.type"),
        ("Player.Process(video.dovi.meta.version)", "dovi.meta.version"),
        ("Player.Process(video.dovi.flags)", "dovi.flags"),
        # source min/max are zeroed by the bitstream on compressed frames,
        # in which case they render empty
        ("Player.Process(video.dovi.source.min.pq)", "dovi.source.min.pq"),
        ("Player.Process(video.dovi.source.min.nits)", "dovi.source.min.nits"),
        ("Player.Process(video.dovi.source.max.pq)", "dovi.source.max.pq"),
        ("Player.Process(video.dovi.source.max.nits)", "dovi.source.max.nits"),
        ("Player.Process(video.dovi.l1.min.pq)", "dovi.l1.min.pq"),
        ("Player.Process(video.dovi.l1.min.nits)", "dovi.l1.min.nits"),
        ("Player.Process(video.dovi.l1.max.pq)", "dovi.l1.max.pq"),
        ("Player.Process(video.dovi.l1.max.nits)", "dovi.l1.max.nits"),
        ("Player.Process(video.dovi.l1.avg.pq)", "dovi.l1.avg.pq"),
        ("Player.Process(video.dovi.l1.avg.nits)", "dovi.l1.avg.nits"),
        ("Player.Process(video.dovi.l3.mid)", "dovi.l3.mid"),
    )),
    ("DV L5 offsets (per-frame)  +  L6 / HDR10 (static)", (
        ("Player.Process(video.dovi.l5.left.offset)", "dovi.l5.left.offset"),
        ("Player.Process(video.dovi.l5.right.offset)", "dovi.l5.right.offset"),
        ("Player.Process(video.dovi.l5.top.offset)", "dovi.l5.top.offset"),
        ("Player.Process(video.dovi.l5.bottom.offset)", "dovi.l5.bottom.offset"),
        ("Player.Process(video.dovi.l6.max.cll)", "dovi.l6.max.cll"),
        ("Player.Process(video.dovi.l6.max.fall)", "dovi.l6.max.fall"),
        ("Player.Process(video.dovi.l6.min.lum)", "dovi.l6.min.lum"),
        ("Player.Process(video.dovi.l6.max.lum)", "dovi.l6.max.lum"),
        ("Player.Process(video.hdr.max.cll)", "hdr.max.cll"),
        ("Player.Process(video.hdr.max.fall)", "hdr.max.fall"),
        ("Player.Process(video.hdr.min.lum)", "hdr.min.lum"),
        ("Player.Process(video.hdr.max.lum)", "hdr.max.lum"),
        ("Player.Process(video.dovi.l9.primaries)", "dovi.l9.primaries"),
        ("Player.Process(video.dovi.l11.type)", "dovi.l11.type"),
        ("Player.Process(video.dovi.l11.whitepoint)", "dovi.l11.whitepoint"),
        ("Player.Process(video.dovi.l11.refmode)", "dovi.l11.refmode"),
    )),
)

ROWS_PER_SECTION = max(len(rows) for _, rows in SECTIONS)

# ---------------------------------------------------------------- trim block
# Targets come live from the l2.trims / l8.trims presence labels; every
# control is then queried per target through the parameterized labels. One
# raw row and one .ui row per target, condensed short keys.
TRIM_SECTIONS = (("L2 trims (per-frame)", "l2"), ("L8 trims (per-frame)", "l8"))
TRIM_RAW_CONTROLS = (("s", "slope"), ("o", "offset"), ("p", "power"),
                     ("cw", "chromaweight"), ("sg", "saturation"),
                     ("td", "tonedetail"))
TRIM_RAW_CONTROLS_L8 = TRIM_RAW_CONTROLS + (("mc", "midcontrastbias"),
                                            ("hc", "highlightclipping"))
TRIM_UI_CONTROLS = (("g", "gain"), ("l", "lift"), ("gm", "gamma"),
                    ("cw", "chromaweight"), ("sg", "saturation"),
                    ("td", "tonedetail"))
# row budget per level: presence row + 2 rows per shown target, more targets
# collapse into a "+N more" tail row
TRIM_MAX_TARGETS = 4
TRIM_DETAIL_ROWS = TRIM_MAX_TARGETS * 2
TRIM_ROWS_TOTAL = 1 + TRIM_DETAIL_ROWS + 1


def trim_rows(level):
    """Composed display rows for one level: (slot key, text) pairs."""
    targets = read("Player.Process(video.dovi.%s.trims)" % level).split()
    rows = [("%s.trims" % level, "targets: %s" % (" ".join(targets) or "-"))]
    shown = targets[:TRIM_MAX_TARGETS]
    controls = TRIM_RAW_CONTROLS_L8 if level == "l8" else TRIM_RAW_CONTROLS
    for target in shown:
        raw = ["%s%s" % (key, read(
            "Player.Process(video.dovi.%s.trim.%s.%s)" % (level, target, name))
            or "-") for key, name in controls]
        ui = ["%s%s" % (key, read(
            "Player.Process(video.dovi.%s.trim.%s.%s.ui)" % (level, target, name))
            or "-") for key, name in TRIM_UI_CONTROLS]
        rows.append(("%s.%s.raw" % (level, target),
                     "%s  %s" % (target, " ".join(raw))))
        rows.append(("%s.%s.ui" % (level, target), "     ui  %s" % " ".join(ui)))
    if len(targets) > len(shown):
        rows.append(("%s.more" % level, "+%d more targets"
                     % (len(targets) - len(shown))))
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
        self._build()

    def _build(self):
        try:
            width, height = self.getWidth(), self.getHeight()
        except Exception:
            width, height = 1280, 720  # pre-Estuary skin coordinate fallback

        # condensed since 2.2.0: the trim block nearly doubles the row count
        line_h = max(14, int(height / 40.0))
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
              + TRIM_ROWS_TOTAL * line_h + footer_block)

        col_w = (pw - 2 * pad - gap) // 2
        name_w = int(col_w * 0.62)
        val_w = col_w - name_w
        title_y = my + int(line_h * 0.35)
        section_y = title_y + title_block
        rows_y = section_y + section_block

        controls = []
        try:
            controls.append(xbmcgui.ControlImage(mx, my, pw, ph, BG_IMAGE,
                                                 colorDiffuse="D0000000"))
        except Exception as exc:  # cosmetic; labels still work
            log("panel image unavailable: %s" % exc, xbmc.LOGWARNING)

        self.title = xbmcgui.ControlLabel(mx + pad, title_y, pw - 2 * pad,
                                          line_h, "", font="font13",
                                          textColor="FFFFD060")
        controls.append(self.title)

        for index, (section_name, section_rows) in enumerate(SECTIONS):
            cx = mx + pad + index * (col_w + gap)
            controls.append(xbmcgui.ControlLabel(
                cx, section_y, col_w, line_h, section_name, font="font12",
                textColor="FFFFD060"))
            for row in range(ROWS_PER_SECTION):
                y = rows_y + row * line_h
                name = xbmcgui.ControlLabel(cx, y, name_w, line_h, "",
                                            font="font12",
                                            textColor="FFA8B4C0")
                value = xbmcgui.ControlLabel(cx + name_w, y, val_w, line_h, "",
                                             font="font13",
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
                cx, trim_y, col_w, line_h, trim_title, font="font12",
                textColor="FFFFD060"))
            column = []
            for row in range(TRIM_ROWS_TOTAL):
                y = trim_y + section_block + row * line_h
                label = xbmcgui.ControlLabel(cx, y, col_w, line_h, "",
                                             font="font12",
                                             textColor="FFFFFFFF")
                column.append(label)
                controls.append(label)
            self.trim_labels.append(column)

        self.footer = xbmcgui.ControlLabel(
            mx + pad, my + ph - int(line_h * 1.3), pw - 2 * pad, line_h,
            "Back or re-run add-on: close   |   all other keys act on the video",
            font="font12", textColor="FF8090A0")
        controls.append(self.footer)

        self.addControls(controls)

    # ---------------------------------------------------------------- refresh
    def refresh(self):
        self.tick += 1
        self.title.setLabel("DV / HDR infolabels     tick %d     %s"
                            % (self.tick, player_state()))

        now = time.monotonic()
        for name_ctl, value_ctl, spec in self.rows:
            if spec is None:
                name_ctl.setLabel("")
                value_ctl.setLabel("")
                continue

            expression, shown_name = spec
            value = read(expression)
            shown = value if value != "" else "-"
            if self.previous.get(expression, value) != value:
                self.changed_at[expression] = now
            if now - self.changed_at.get(expression, -HIGHLIGHT_SECS) < HIGHLIGHT_SECS:
                shown = "[COLOR FF60FF60]%s[/COLOR]" % shown
            self.previous[expression] = value
            name_ctl.setLabel(shown_name)
            value_ctl.setLabel(shown)

        for (_, level), column in zip(TRIM_SECTIONS, self.trim_labels):
            composed = trim_rows(level)
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
