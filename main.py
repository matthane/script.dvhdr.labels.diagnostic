# -*- coding: utf-8 -*-
"""
script.dvhdr.labels.diagnostic -- on-screen overlay for verifying the Dolby Vision
and HDR infolabels registered by the CoreELEC label registry
(branch ce-label-registry).

2.0.x polled the mainline Kodi 22 upstream-PR surface (eleven undotted
Player.Process names). Those labels were reimplemented on the CoreELEC branch
as 23 dotted names (video.dovi.*, video.hdr.*) registered through
CEGUIInfoRegistry instead of the upstream label tables; the undotted names no
longer exist there, including Player.Process(hdrtype), for which the stock
VideoPlayer.HdrType row stands in. All 25 rows still fit on one screen as two
titled sections side by side; no paging.

Semantics of the new set (authoritative: xbmc/guilib/guiinfo/CEGUIInfoLabels.dox
and xbmc/cores/VideoPlayer/DVDCodecs/Video/AMLFrameMetadata.h):
  - one label per quantity, value in nits (cd/m2) -- no separate .pq/.nits
    pairs. Bare numbers, no unit suffix; values below 1 carry four decimals.
    The raw PQ codes exist only as GetInt companions, unreachable via
    getInfoLabel, so this overlay shows nits.
  - presence is signalled by emptiness: an absent level (no L1/L5/L6) resolves
    to "" (shown here as "-"). There are no has.* labels.
  - labels resolve empty on a build without the
    feature the label itself resolves empty.
  - video.dovi.flags is one space-separated token string out of
    converted / l5zeroed / rpuremoved / compressed (empty when none or no DV).

Read-only test tooling: no network, no settings, no filesystem writes.
"""

import os
import sys
import time
import traceback

import xbmc
import xbmcgui

ADDON_ID = "script.dvhdr.labels.diagnostic"
ADDON_VERSION = "2.1.0"  # keep in sync with addon.xml
LOG_PREFIX = "[script.dvhdr.labels.diagnostic] "

# Singleton guard: a property on the home window (10000) acts as a running
# flag. Addons.ExecuteAddon spawns a fresh Python invocation on every call,
# so without this each call stacks another WindowDialog over the last one --
# ghosted overlay text and an orphaned Python thread per extra call. Set on
# start, checked before building the window, cleared in run_overlay()'s
# finally block so a crash still releases it.
RUNNING_PROPERTY = ADDON_ID + ".running"

# Toggle support (2.1.0): a second invocation while RUNNING_PROPERTY is set no
# longer just refuses -- it sets this property, which the poll loop of the
# open overlay treats as a close request. So RunScript/ExecuteAddon acts as a
# show/hide toggle. Cleared on startup (a stale request from a crashed
# instance must not instantly close a fresh one) and in the finally block.
CLOSE_PROPERTY = ADDON_ID + ".closerequest"

# Poll interval in seconds. 0.05 = 20 Hz, effectively bounded by the GUI
# render rate; label reads are in-process and cheap.
POLL_INTERVAL = 0.05
# How long a changed value stays highlighted, decoupled from the poll rate.
HIGHLIGHT_SECS = 0.6

# ------------------------------------------------------------ label sections
# One page, two titled sections rendered as two columns. Each row is
# (infolabel expression passed verbatim to xbmc.getInfoLabel, short name shown
# on screen -- the dotted name minus its "video." prefix). The
# Player.Process(...) names are the shipped strings from the CoreELEC label
# registry map in xbmc/cores/VideoPlayer/DVDCodecs/Video/AMLFrameMetadata.h;
# the VideoPlayer.* names come from the `videoplayer` map in
# xbmc/GUIInfoManager.cpp.
SECTIONS = (
    ("DV identity (static)  +  source / L1 nits (per-frame)", (
        ("VideoPlayer.HdrType", "HdrType"),
        ("VideoPlayer.HdrDetail", "HdrDetail"),
        # valid is "1"/"0"; empty means a build without the feature.
        ("Player.Process(video.dovi.apiversion)", "dovi.apiversion"),
        ("Player.Process(video.dovi.profile)", "dovi.profile"),
        # el.type is FEL/MEL on profile 7 only, empty elsewhere.
        ("Player.Process(video.dovi.el.type)", "dovi.el.type"),
        # meta.version is "2.9" or "4.0".
        ("Player.Process(video.dovi.meta.version)", "dovi.meta.version"),
        # flags is one space-separated token string out of
        # converted / l5zeroed / rpuremoved / compressed.
        ("Player.Process(video.dovi.flags)", "dovi.flags"),
        # source min/max are stream-level nits; not sent on compressed frames
        # (the "compressed" flag), in which case they render empty.
        ("Player.Process(video.dovi.source.min.pq)", "dovi.source.min.pq"),
        ("Player.Process(video.dovi.source.min.nits)", "dovi.source.min.nits"),
        ("Player.Process(video.dovi.source.max.pq)", "dovi.source.max.pq"),
        ("Player.Process(video.dovi.source.max.nits)", "dovi.source.max.nits"),
        # The L1 trio is per-frame: it ticks while playing, freezes while
        # paused, and re-syncs after a seek. Values are bare nits numbers
        # ("834"; below 1 shows four decimals, e.g. "0.0001"). A clip whose
        # RPU carries constant L1 will sit still -- that is the file, not the
        # build.
        ("Player.Process(video.dovi.l1.min.pq)", "dovi.l1.min.pq"),
        ("Player.Process(video.dovi.l1.min.nits)", "dovi.l1.min.nits"),
        ("Player.Process(video.dovi.l1.max.pq)", "dovi.l1.max.pq"),
        ("Player.Process(video.dovi.l1.max.nits)", "dovi.l1.max.nits"),
        ("Player.Process(video.dovi.l1.avg.pq)", "dovi.l1.avg.pq"),
        ("Player.Process(video.dovi.l1.avg.nits)", "dovi.l1.avg.nits"),
    )),
    ("DV L5 offsets (per-frame)  +  L6 / HDR10 (static)", (
        # L5 is per-frame RPU metadata as well: the active-area offsets can
        # change shot to shot on content with varying letterboxing.
        ("Player.Process(video.dovi.l5.left.offset)", "dovi.l5.left.offset"),
        ("Player.Process(video.dovi.l5.right.offset)", "dovi.l5.right.offset"),
        ("Player.Process(video.dovi.l5.top.offset)", "dovi.l5.top.offset"),
        ("Player.Process(video.dovi.l5.bottom.offset)", "dovi.l5.bottom.offset"),
        # L6 is stream-level: MaxCLL/MaxFALL plus the mastering-display
        # min/max luminance, all in nits. Should not move during playback.
        ("Player.Process(video.dovi.l6.max.cll)", "dovi.l6.max.cll"),
        ("Player.Process(video.dovi.l6.max.fall)", "dovi.l6.max.fall"),
        ("Player.Process(video.dovi.l6.min.lum)", "dovi.l6.min.lum"),
        ("Player.Process(video.dovi.l6.max.lum)", "dovi.l6.max.lum"),
        # HDR10 static metadata from the stream hints, present on any HDR
        # stream (not DV-only).
        ("Player.Process(video.hdr.max.cll)", "hdr.max.cll"),
        ("Player.Process(video.hdr.max.fall)", "hdr.max.fall"),
        ("Player.Process(video.hdr.min.lum)", "hdr.min.lum"),
        ("Player.Process(video.hdr.max.lum)", "hdr.max.lum"),
    )),
)

ROWS_PER_SECTION = max(len(rows) for _, rows in SECTIONS)

# Action ids, from xbmc/input/actions/ActionIDs.h.
ACT_PREVIOUS_MENU = 10
ACT_NAV_BACK = 92

# 2.1.0: only Back / PreviousMenu close the overlay (or re-running the addon,
# see CLOSE_PROPERTY). Select no longer closes -- it opens the video OSD,
# exactly what OK does in fullscreen video without the overlay.
CLOSE = (ACT_PREVIOUS_MENU, ACT_NAV_BACK)

# ------------------------------------------------------- input pass-through
# Why some keys die with the overlay up: XBMCAddon WindowDialog is hard-modal
# (IsModalDialog() is `return true` in WindowDialog.h), so the keymap context
# for every key press is the <global> section of the keymaps, not
# <FullscreenVideo>. A remote/keyboard "left" therefore arrives here as
# ACTION_MOVE_LEFT (GUI focus navigation, which nothing handles globally)
# instead of StepBack, "select" arrives as ACTION_SELECT_ITEM instead of OSD,
# and so on. The fix is to re-issue, for each such context-hijacked id, the
# action the <FullscreenVideo> keymap section would have produced for the
# same physical key.
#
# Two delivery paths, chosen to match where Kodi actually handles the action
# (traced in the Kodi tree):
#
#   FORWARD_TO_APP -- xbmc.executebuiltin('Action(<name>)') with no window
#     argument. GUIBuiltins.cpp sends TMSG_GUI_ACTION with WINDOW_INVALID,
#     which GUIWindowManager.cpp routes to g_application.OnAction(): the same
#     global pipeline (CSeekHandler, playlist player, player core) that runs
#     without the overlay. CGUIWindowFullScreen::OnAction ignores the seek /
#     skip / number ids entirely, and a *targeted* Action(name,window) calls
#     only pWindow->OnAction() with no fallthrough, so targeting the window
#     would silently drop these.
#     Loop safety: the untargeted re-issue passes through the window manager,
#     which hands it to this dialog again (topmost modal), so onAction sees
#     an echo of every forward. That is safe by construction: no forwarded
#     action's translated id (21/20/97/98/14/15) is itself a FORWARD_TO_APP
#     key, so every echo lands in the ignored-silently default below.
#
#   FORWARD_TO_WINDOW -- xbmc.executebuiltin('Action(<name>,fullscreenvideo)').
#     Delivered straight to CGUIWindowFullScreen::OnAction, for actions only
#     that window handles (the OSD toggle). Targeted sends never re-enter
#     this dialog.
#
# Digits are deliberately NOT forwarded: CSeekHandler is a *global* action
# listener (registered via RegisterActionListener in Application.cpp), and
# its ChangeTimeCode consumes REMOTE_0..9 (58..67) and JUMP_SMS2..9
# (142..149) natively even with this modal dialog open -- a physical digit
# press already enters the seek timecode once. Forwarding it would enter it
# twice: pressing 1,5 would seek as 1155.

# incoming id in <global> context -> fullscreenvideo action name.
FORWARD_TO_APP = {
    1: "stepback",                  # MOVE_LEFT   -> left seek
    2: "stepforward",               # MOVE_RIGHT  -> right seek
    3: "chapterorbigstepforward",   # MOVE_UP     -> chapter/big step +
    4: "chapterorbigstepback",      # MOVE_DOWN   -> chapter/big step -
    5: "skipnext",                  # PAGE_UP     -> next item
    6: "skipprevious",              # PAGE_DOWN   -> previous item
}

FORWARD_TO_WINDOW = {
    7: "osd",     # SELECT_ITEM  -- OK opens the video OSD, as in fullscreen
    117: "osd",   # CONTEXT_MENU -- remote menu/title key, global context
    163: "osd",   # MENU         -- keyboard "m", global context
}

# DO NOT forward transport keys. v1.0.2 forwarded the pause and
# seek actions to PlayerControl(...) builtins, which double-toggled: one press
# paused and instantly resumed. Traced in the Kodi tree:
#
#   1. CApplication::OnAction offers the action to the window manager first
#      (Application.cpp).
#   2. XBMCAddon WindowDialog::IsModalDialog() is a hardcoded `return true`
#      (WindowDialog.h) even when opened with show(), so
#      CGUIWindowManager::HandleAction takes the modal branch.
#   3. Our OnAction chain bottoms out in CGUIWindow::OnAction, whose default
#      switch only knows NAV_BACK / PREVIOUS_MENU / SHOW_INFO / MENU, so it
#      returns false -- but Window::OnAction has already *queued* this Python
#      onAction callback (Window.cpp).
#   4. HandleAction therefore returns false and CApplication falls through to
#      its own global handling, which pauses via appPlayer->Pause() and handles
#      FF/RW; step/big-step seeks go to CSeekHandler, registered as an action
#      listener.
#   5. Milliseconds later the queued Python callback ran and toggled it back.
#
# So every transport key already reaches the player natively while the overlay
# is up; the forward was pure duplication. The Python return value is ignored by
# the C++ side, so doing nothing here cannot break that native path.
#
# That is why FORWARD_TO_APP above contains only the ids whose *keymap
# translation* changed because of the dialog's <global> context AND that
# nothing handles natively (navigation, page, select/menu). Ids that reach
# their handler anyway -- playpause, pause, play, stop, FF/RW, skip
# next/previous (keyboard . and ,), info, volume up/down, mute, and the
# digit timecode ids consumed by the CSeekHandler action listener -- fall
# through HandleAction to CApplication's global handlers before this Python
# callback even runs, and are left alone.

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
    """Borderless overlay; renders above fullscreen video without pausing it.

    Mechanically transparent to input since 2.1.0: only Back/PreviousMenu
    (or re-running the addon) closes it; every other key either falls
    through to the player natively or is re-issued as the action the
    FullscreenVideo keymap would have produced (see FORWARD_TO_APP /
    FORWARD_TO_WINDOW above).
    """

    # No super().__init__() on purpose: the C++ side is built in __new__ and
    # some Kodi builds warn when a Window subclass chains __init__.
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

        line_h = max(18, int(height / 24.0))
        mx, my = int(width * 0.03), int(height * 0.04)
        pad = int(width * 0.015)
        gap = int(width * 0.02)
        pw = width - 2 * mx

        title_block = int(line_h * 1.9)
        section_block = int(line_h * 1.35)
        footer_block = int(line_h * 1.8)
        # Size the panel to the content rather than the screen: this overlay
        # sits on top of the video the user is trying to look at.
        ph = (int(line_h * 0.35) + title_block + section_block
              + ROWS_PER_SECTION * line_h + footer_block)

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
        except Exception as exc:  # background is cosmetic; labels still work
            log("panel image unavailable: %s" % exc, xbmc.LOGWARNING)

        self.title = xbmcgui.ControlLabel(mx + pad, title_y, pw - 2 * pad,
                                          line_h, "", font="font13",
                                          textColor="FFFFD060")
        controls.append(self.title)

        # One column per section: a heading in the accent colour, then its rows.
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
                # Rows past the end of a short section stay blank.
                self.rows.append((name, value,
                                  section_rows[row] if row < len(section_rows)
                                  else None))
                controls.extend((name, value))

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
        # Every other id -- pause, play, FF/RW, stop, skips, digits, info,
        # volume, mute, and the echoes of our own untargeted forwards --
        # already reached its handler through CApplication's global
        # fallthrough before this callback ran (see the notes above);
        # forwarding here would double-fire. Unknown ids are ignored
        # silently.

    def onControl(self, control):
        pass


# ------------------------------------------------------------------- overlay

def run_overlay():
    home = xbmcgui.Window(10000)
    if home.getProperty(RUNNING_PROPERTY):
        # Toggle: ask the open overlay to close instead of stacking a second
        # WindowDialog over it. Its poll loop sees the property within one
        # POLL_INTERVAL and shuts down.
        log("already running (property %s set) -- requesting close (toggle)"
            % RUNNING_PROPERTY)
        home.setProperty(CLOSE_PROPERTY, "1")
        return
    # A stale close request (e.g. toggle raced a crashing instance) must not
    # instantly close this fresh one.
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
        # v1.0.4 had a headless "diag" mode; it scanned kodi.log for CoreELEC-only
        # tags (processDoviRpu counters, Amlogic dual-layer packet lines) that
        # mainline never emits, so it was removed rather than ported to nothing.
        log("ignoring unknown params %r (diag mode was removed in 2.0.0)"
            % (argv[1:],), xbmc.LOGWARNING)
    run_overlay()


if __name__ == "__main__":
    main()
