# Split — Bobi.Studio's four-channel 2D DVE

*[Version française](README.md)*

A 2D DVE: four channels plus a background, composited live into a single output. Submodule of
[bobi.studio](https://github.com/bob-integration/bobistudio), mounted at `plugins/split/`.

## What it does

Five inputs, the background and four channels. Each channel has the usual settings (position,
crop, scale, rotation, opacity, rounded corners, softness, drop shadow, mirror, stacking order,
3D border).

Layouts are saved, recalled with an interpolated transition (nine easing curves, per-box offset),
exported and sent to another split. Every parameter is interpolated, not just the geometry.

## Composing

An editor similar to the multiviewer's lets you build your composition. Drag to move, pull a
corner to resize, pull an edge to crop; the keyboard arrows move by one pixel and bypass snapping.
Positions range from −1 to 2, for moves into and out of frame.

On top of the four channels there is a **Global channel**. It is not a multi-selection: it is a
separate object, composed over the settings of each box, `render(box i) = Global ∘ box_i`. The
whole composition moves out of frame as one block, each box keeping its relative position. It is
saved in the memories and interpolated like a box, at no cost.

## Background

Black, a video source, a flat colour, or a gradient of two to four stops, with an angle and a
**softness**. At zero softness the boundary is sharp: the background then works as a wipe,
animated by a memory recall with a duration.

## Overlay

An imported image (PNG, JPEG) sits above or below the channels, with its transparency if it has
any. Three key modes: **alpha** (the image's transparency channel), **luma** and **chroma**. An
image without an alpha channel stays opaque in alpha mode.

## Per-frame cost

Compositing runs on the `libbobi_mvk` C kernel — gather, border ribbon and blend in a single
memory pass, since these passes are bound by RAM, not by computation.

A word on the resources needed: the real CPU consumption comes from animations.

| 1080p, 4 boxes, 2 cores | static | animated |
|---|---|---|
| base | 7.0 ms | 6.4 ms |
| no rotation | 9.0 ms | 22.7 ms |
| rotation only | 10.3 ms | 19.8 ms |
| all effects | 18.4 ms | 58.3 ms |

Without any rotation, animation already costs twice the 50p budget: rotation is not the only
cause. And animated "all effects" is not the sum of its parts — the growth is superlinear.

## CPU or GPU

CPU rendering is the default and suits every static or lightly animated composition. GPU
rendering (`render=gpu`, NVIDIA card) is meant for compositions that are animated all the time: it
brings a rich animated frame under 8 ms, at the price of one card tied up for four to five splits.
It cannot be combined with slice mode, and in `auto` a fallback to CPU is published on `:8080`.
The two paths are not byte-identical: switching a composition changes its bytes, not its look.

## Slice mode

The output is published band by band, removing one frame of latency downstream. Global setting
under Settings → MXL.

## Control and diagnostics

Every continuous parameter can be driven by macros and triggers (`param_tree`, bounds published
live), and memories are recalled by action. `GET /state` returns the effective composition;
`:8080` publishes the cadence, the frame budget, the layers actually composited and the reason for
each channel that is not. That last counter matters: a split can hold its cadence while
compositing nothing, and `composed` is what tells you.

## Colours

Border, shadow, chroma key colour, overlay and background share a single conversion, set by the
format's declared colorimetry (601, 709, 2020); when it is missing, 709 is assumed and announced.
Versions before 0.24 applied a full-range 601 matrix whatever the format: the hues of a
composition from that time may have shifted slightly.

## Resources

Three cores, 2 GiB declared, pinned, plus an optional GPU. Measured on 2026-10-30 (dell-1, four
boxes, CPU rendering, 1080p50): 92 to 100 MiB.

## Licence

GNU GPL v3 or later — see `LICENSE`.
