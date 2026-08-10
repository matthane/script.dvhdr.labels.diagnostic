# DV/HDR Labels (Diagnostic)

A small Kodi add-on that shows the live Dolby Vision and HDR infolabels as an overlay during playback. I use it for testing the CoreELEC label registry branch, and it may be useful for anyone else who wants to verify those labels on real content.

## Install

Download the zip from the latest release and install it in Kodi with Add-ons, Install from zip file.

## Use

Run the add-on from the add-ons list, or bind a key to `RunScript(script.dvhdr.labels.diagnostic)`. The overlay is transparent to input, so seeking and the OSD keep working while it is open. Back closes it, and running the add-on again will also close it. If you bind a key, bind it in the Global context so the same key can close the overlay too.

## Labels

The overlay polls the `Player.Process(video.dovi.*)` and `Player.Process(video.hdr.*)` labels from the CoreELEC label registry branch, plus the stock `VideoPlayer.HdrType` and `VideoPlayer.HdrDetail`. On builds without those labels the rows will just show empty values.

License GPL-2.0-or-later.
